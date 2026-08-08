"""Agent media attachments + reply targeting: tools, delivery, persistence.

Delivery contract under test: text parts first, then ONE media send (single
photo/video or one album, never per-item), media never carries a reply link,
history re-sends are persisted already-described (no second vision call), and
media failure degrades the run instead of failing it.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message as TgMessage
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from sqlalchemy import select

from app.agents.deps import AgentDeps
from app.agents.runtime import execute_run
from app.agents.tools import (
    MAX_RUN_ATTACHMENTS,
    attach_media,
    attach_media_url,
    set_reply_target,
)
from app.core.crypto import encrypt
from app.memory.embeddings import FakeEmbedder
from app.models import AgentRun, Bot, ChatThread, Message, MessageAttachment
from app.telegram.limiter import SendLimiter
from app.telegram.media import OutgoingMedia

from tests.test_agent import AgentFakeBot, add_agent_model, authorize_chat
from tests.test_handlers import BOT_USER, message_update, run_update


@pytest.fixture
async def bot_row(db_sessionmaker):
    async with db_sessionmaker() as s:
        row = Bot(
            tg_bot_id=999,
            username="convoke_bot",
            name="ConvokeBot",
            token_encrypted=encrypt("123:fake"),
            can_read_all_group_messages=True,
        )
        s.add(row)
        await s.commit()
        return row


class MediaFakeBot(AgentFakeBot):
    """Adds media sends and a unified event log so tests can assert ordering
    across text and media calls."""

    def __init__(self, member_status: str = "administrator"):
        super().__init__(member_status)
        self.events: list[tuple] = []  # ("text"|"photo"|"video"|"group", payload, kwargs)
        self.fail_media: Exception | None = None  # raised by every media send
        self.fail_media_once = False
        self.fail_text: Exception | None = None  # raised by every text send

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        if self.fail_text is not None:
            raise self.fail_text
        msg = await super().send_message(chat_id, text, reply_markup, **kwargs)
        self.events.append(("text", text, kwargs))
        return msg

    def _maybe_fail(self):
        if self.fail_media is not None:
            exc = self.fail_media
            if self.fail_media_once:
                self.fail_media = None
            raise exc

    def _media_msg(self, chat_id, kind: str, group_id: str | None = None) -> TgMessage:
        self._next_id += 1
        payload = {
            "message_id": self._next_id,
            "date": int(datetime.now(timezone.utc).timestamp()),
            "chat": {"id": chat_id, "type": "supergroup", "title": "Test Group"},
            "from": BOT_USER,
        }
        if group_id:
            payload["media_group_id"] = group_id
        if kind == "video":
            payload["video"] = {
                "file_id": f"sentV{self._next_id}", "file_unique_id": f"su{self._next_id}",
                "width": 100, "height": 100, "duration": 5,
            }
        else:
            payload["photo"] = [{
                "file_id": f"sentF{self._next_id}", "file_unique_id": f"su{self._next_id}",
                "width": 100, "height": 100,
            }]
        return TgMessage.model_validate(payload)

    async def send_photo(self, chat_id, photo, **kwargs):
        self._maybe_fail()
        self.events.append(("photo", photo, kwargs))
        return self._media_msg(chat_id, "photo")

    async def send_video(self, chat_id, video, **kwargs):
        self._maybe_fail()
        self.events.append(("video", video, kwargs))
        return self._media_msg(chat_id, "video")

    async def send_media_group(self, chat_id, media, **kwargs):
        self._maybe_fail()
        self.events.append(("group", [m.media for m in media], kwargs))
        return [
            self._media_msg(chat_id, "video" if m.type == "video" else "photo", group_id="g1")
            for m in media
        ]


def bad_request() -> TelegramBadRequest:
    return TelegramBadRequest(method=None, message="Bad Request: test failure")


def media_model(tool_calls: list[tuple[str, dict]], text: str = "here you go"):
    """FunctionModel that makes the given tool calls one per turn, then
    replies with `text` — args stay exact, unlike TestModel's synthesized
    ones (attach_media must hit the seeded message ids)."""

    def fn(messages, info):
        n = sum(1 for m in messages if isinstance(m, ModelResponse))
        if n < len(tool_calls):
            tool, args = tool_calls[n]
            return ModelResponse(
                parts=[ToolCallPart(tool_name=tool, args=args, tool_call_id=f"c{n}")]
            )
        return ModelResponse(parts=[TextPart(text)])

    return FunctionModel(fn)


async def seed_media_messages(db_sessionmaker, chat_id: int):
    """#2 described photo, #3 imported photo (no file_id, source=import), #4 video note,
    #5 pending photo, #6 described video, #7 photo in unmonitored thread 55,
    #8 described photo (spare for cap tests), #9 animation (GIF), #10 plain
    message in monitored forum thread 77."""
    t0 = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    async with db_sessionmaker() as s:
        rows = {}
        for tg in (1, 2, 3, 4, 5, 6, 8, 9):
            m = Message(chat_id=chat_id, tg_message_id=tg, sender_name="Alice",
                        text="", sent_at=t0,
                        source="import" if tg == 3 else "live")
            s.add(m)
            rows[tg] = m
        m7 = Message(chat_id=chat_id, tg_message_id=7, thread_id=55, sender_name="Alice",
                     text="", sent_at=t0)
        s.add(m7)
        rows[7] = m7
        s.add(ChatThread(chat_id=chat_id, thread_key=55, monitored=False))
        await s.flush()
        att = dict(chat_id=chat_id)
        s.add(MessageAttachment(message_id=rows[2].id, tg_message_id=2, kind="photo",
                                file_id="F2", file_unique_id="u2", status="described",
                                description="sunset over the bridge", **att))
        s.add(MessageAttachment(message_id=rows[3].id, tg_message_id=3, kind="photo",
                                file_id=None, file_unique_id="u3", status="described",
                                description="imported picnic", **att))
        s.add(MessageAttachment(message_id=rows[4].id, tg_message_id=4, kind="video_note",
                                file_id="F4", file_unique_id="u4", status="described",
                                description="a circle video", **att))
        s.add(MessageAttachment(message_id=rows[5].id, tg_message_id=5, kind="photo",
                                file_id="F5", file_unique_id="u5", status="pending", **att))
        s.add(MessageAttachment(message_id=rows[6].id, tg_message_id=6, kind="video",
                                file_id="F6", file_unique_id="u6", status="described",
                                description="a dog chasing waves", **att))
        s.add(MessageAttachment(message_id=rows[7].id, tg_message_id=7, kind="photo",
                                file_id="F7", file_unique_id="u7", status="described",
                                description="SECRET", **att))
        s.add(MessageAttachment(message_id=rows[8].id, tg_message_id=8, kind="photo",
                                file_id="F8", file_unique_id="u8", status="described",
                                description="spare shot", **att))
        s.add(MessageAttachment(message_id=rows[9].id, tg_message_id=9, kind="animation",
                                file_id="F9", file_unique_id="u9", status="described",
                                description="a dancing cat gif", **att))
        s.add(Message(chat_id=chat_id, tg_message_id=10, thread_id=77, sender_name="Alice",
                      text="topic anchor", sent_at=t0))
        await s.commit()


async def test_attach_media_tool_validation(db_sessionmaker, bot_row):
    fake = MediaFakeBot()
    chat = await authorize_chat(db_sessionmaker, fake, bot_row)
    await seed_media_messages(db_sessionmaker, chat.id)
    deps = AgentDeps(sessionmaker=db_sessionmaker, embedder=FakeEmbedder(),
                     chat_id=chat.id, run_id=1)
    ctx = SimpleNamespace(deps=deps)

    # happy path: described photo → spec carries file_id + copied description
    out = await attach_media(ctx, 2)
    assert "Attached the photo from #2" in out
    assert deps.media == [OutgoingMedia(
        kind="photo", source="history", file_id="F2", src_tg_message_id=2,
        described=True, description="sunset over the bridge", transcript=None,
    )]

    # video works; pending photo attaches with nothing copied (described later
    # by the normal pipeline)
    assert "Attached the video from #6" in await attach_media(ctx, 6)
    await attach_media(ctx, 5)
    assert deps.media[2].description is None and deps.media[2].file_id == "F5"

    # rejections, each with an actionable reason
    assert "no media" in (await attach_media(ctx, 1))
    assert "history import" in (await attach_media(ctx, 3))
    assert "video note" in (await attach_media(ctx, 4))
    # a GIF's Animation-typed file_id can't be re-sent via sendVideo/albums
    assert "GIF/animation" in (await attach_media(ctx, 9))
    assert "not in Convoke's stored history" in (await attach_media(ctx, 99))
    # unmonitored thread reads exactly like a missing id (no existence leak)
    assert "not in Convoke's stored history" in (await attach_media(ctx, 7))
    # duplicate is idempotent
    assert "already attached" in (await attach_media(ctx, 2))
    assert len(deps.media) == 3

    # cap: fill to the limit, the next attach is refused
    deps.media.extend(
        OutgoingMedia(kind="photo", source="url", url=f"https://x.test/{i}.jpg")
        for i in range(MAX_RUN_ATTACHMENTS - len(deps.media))
    )
    assert "Attachment limit reached" in (await attach_media(ctx, 8))
    assert len(deps.media) == MAX_RUN_ATTACHMENTS


async def test_attach_media_url_validation(db_sessionmaker, bot_row):
    fake = MediaFakeBot()
    chat = await authorize_chat(db_sessionmaker, fake, bot_row)
    deps = AgentDeps(sessionmaker=db_sessionmaker, embedder=FakeEmbedder(),
                     chat_id=chat.id, run_id=1)
    ctx = SimpleNamespace(deps=deps)

    assert "Attached the image URL" in (await attach_media_url(ctx, " https://pics.test/eiffel.jpg "))
    assert deps.media == [OutgoingMedia(kind="photo", source="url",
                                        url="https://pics.test/eiffel.jpg")]
    assert "already attached" in (await attach_media_url(ctx, "https://pics.test/eiffel.jpg"))
    assert "http(s)" in (await attach_media_url(ctx, "ftp://pics.test/x.jpg"))
    assert "http(s)" in (await attach_media_url(ctx, "data:image/png;base64,xxxx"))
    assert "http(s)" in (await attach_media_url(ctx, ""))
    assert "too long" in (await attach_media_url(ctx, "https://x.test/" + "a" * 2048))
    assert len(deps.media) == 1


async def test_set_reply_target_tool(db_sessionmaker, bot_row):
    fake = MediaFakeBot()
    chat = await authorize_chat(db_sessionmaker, fake, bot_row)
    await seed_media_messages(db_sessionmaker, chat.id)
    async with db_sessionmaker() as s:
        # Pre-migration import: exports carry the basic-group era with
        # negative synthetic ids nothing in Telegram can resolve.
        s.add(Message(chat_id=chat.id, tg_message_id=-11, sender_name="Alice",
                      text="from before the migration", source="import",
                      sent_at=datetime(2025, 5, 1, 12, 0, tzinfo=timezone.utc)))
        await s.commit()
    deps = AgentDeps(sessionmaker=db_sessionmaker, embedder=FakeEmbedder(),
                     chat_id=chat.id, run_id=1)
    ctx = SimpleNamespace(deps=deps)

    assert "reply to #2" in (await set_reply_target(ctx, 2))
    assert deps.reply_target == (2, None)
    # last call wins
    await set_reply_target(ctx, 6)
    assert deps.reply_target == (6, None)
    # post-migration imported ids are the chat's real message ids — targetable
    assert "reply to #3" in (await set_reply_target(ctx, 3))
    assert deps.reply_target == (3, None)
    # unknown id, unmonitored thread, and pre-migration (negative-id) imports
    # leave the target unchanged
    assert "not in Convoke's stored history" in (await set_reply_target(ctx, 99))
    assert "not in Convoke's stored history" in (await set_reply_target(ctx, 7))
    assert "migration" in (await set_reply_target(ctx, -11))
    assert deps.reply_target == (3, None)


async def run_agent_with(db_sessionmaker, bot_row, fake, monkeypatch, tool_calls,
                         request="@convoke_bot send it", arm=None):
    """`arm` runs right before execute_run — for failure toggles that must
    not affect chat setup (the auth prompt is a send too)."""
    import app.agents.runtime as runtime

    model = media_model(tool_calls)
    monkeypatch.setattr(runtime, "build_model", lambda provider: model)
    chat = await authorize_chat(db_sessionmaker, fake, bot_row)
    await seed_media_messages(db_sessionmaker, chat.id)
    await run_update(db_sessionmaker, fake, bot_row, message_update(3, 20, request))
    async with db_sessionmaker() as s:
        await add_agent_model(s)
        await s.commit()
        run_id = (await s.execute(select(AgentRun.id))).scalar_one()
    if arm is not None:
        arm()
    await execute_run(db_sessionmaker, FakeEmbedder(), SendLimiter(), fake, run_id)
    return chat, run_id


async def test_execute_run_delivers_single_photo(db_sessionmaker, bot_row, monkeypatch):
    fake = MediaFakeBot()
    chat, run_id = await run_agent_with(
        db_sessionmaker, bot_row, fake, monkeypatch, [("attach_media", {"message_id": 2})]
    )

    async with db_sessionmaker() as s:
        run = await s.get(AgentRun, run_id)
        assert run.status == "done", run.error
        assert run.error is None
        assert run.media == [{"kind": "photo", "source": "history", "ok": True}]

    # text first, then exactly one media send, re-using the stored file_id;
    # media carries no reply link (the text does the pointing)
    media_events = [e for e in fake.events if e[0] != "text"]
    assert [e[0] for e in fake.events[-2:]] == ["text", "photo"]
    assert media_events == [("photo", "F2", {"message_thread_id": None})]

    # the sent photo is persisted as self + attachment, already described with
    # the copied text — invisible to MediaLoop's pending scan
    async with db_sessionmaker() as s:
        att = (
            await s.execute(
                select(MessageAttachment)
                .join(Message, Message.id == MessageAttachment.message_id)
                .where(Message.source == "self", MessageAttachment.kind == "photo")
            )
        ).scalar_one()
        assert att.status == "described"
        assert att.description == "sunset over the bridge"
        assert att.file_id.startswith("sentF")


async def test_execute_run_delivers_album_in_order(db_sessionmaker, bot_row, monkeypatch):
    fake = MediaFakeBot()
    chat, run_id = await run_agent_with(
        db_sessionmaker, bot_row, fake, monkeypatch,
        [("attach_media", {"message_id": 6}), ("attach_media", {"message_id": 2})],
    )

    async with db_sessionmaker() as s:
        run = await s.get(AgentRun, run_id)
        assert run.status == "done", run.error
        assert run.media == [
            {"kind": "video", "source": "history", "ok": True},
            {"kind": "photo", "source": "history", "ok": True},
        ]
    # ONE group send, attach order preserved (video first)
    media_events = [e for e in fake.events if e[0] != "text"]
    assert media_events == [("group", ["F6", "F2"], {"message_thread_id": None})]
    # album members persist individually, sharing the media_group_id
    async with db_sessionmaker() as s:
        atts = (
            (
                await s.execute(
                    select(MessageAttachment)
                    .join(Message, Message.id == MessageAttachment.message_id)
                    .where(Message.source == "self")
                    .order_by(MessageAttachment.tg_message_id)
                )
            )
            .scalars()
            .all()
        )
        assert [a.kind for a in atts] == ["video", "photo"]
        assert {a.media_group_id for a in atts} == {"g1"}
        assert atts[0].description == "a dog chasing waves"


async def test_media_failure_drops_url_items_keeps_album(db_sessionmaker, bot_row, monkeypatch):
    """Mixed album fails once (URL member can poison a group send) → the
    file_id-only album is retried as the sole fallback; URL item is reported
    failed; the run stays done with a degradation note. Never per-item."""
    fake = MediaFakeBot()
    fake.fail_media = bad_request()
    fake.fail_media_once = True
    chat, run_id = await run_agent_with(
        db_sessionmaker, bot_row, fake, monkeypatch,
        [
            ("attach_media", {"message_id": 2}),
            ("attach_media_url", {"url": "https://pics.test/broken.jpg"}),
        ],
    )

    async with db_sessionmaker() as s:
        run = await s.get(AgentRun, run_id)
        assert run.status == "done", run.error
        assert run.media == [
            {"kind": "photo", "source": "history", "ok": True},
            {"kind": "photo", "source": "url", "ok": False},
        ]
        assert "some media failed to send" in run.error
    # first attempt was the mixed group (failed before recording), fallback is
    # the single history photo — the URL item is never sent on its own
    media_events = [e for e in fake.events if e[0] != "text"]
    assert media_events == [("photo", "F2", {"message_thread_id": None})]
    assert not any("broken.jpg" in str(e) for e in fake.events)


async def test_media_total_failure_posts_notice(db_sessionmaker, bot_row, monkeypatch):
    fake = MediaFakeBot()
    fake.fail_media = bad_request()  # every media send fails deterministically
    chat, run_id = await run_agent_with(
        db_sessionmaker, bot_row, fake, monkeypatch, [("attach_media", {"message_id": 2})]
    )

    async with db_sessionmaker() as s:
        run = await s.get(AgentRun, run_id)
        assert run.status == "done", run.error  # degradation, not failure
        assert run.media == [{"kind": "photo", "source": "history", "ok": False}]
        assert "some media failed to send" in run.error
        # no self attachment row was persisted for the failed send
        n = (
            await s.execute(
                select(MessageAttachment)
                .join(Message, Message.id == MessageAttachment.message_id)
                .where(Message.source == "self")
            )
        ).scalars().all()
        assert n == []
    assert fake.sent[-1].text == "Media attachment failed."


async def test_reply_target_overrides_text_not_media(db_sessionmaker, bot_row, monkeypatch):
    fake = MediaFakeBot()
    chat, run_id = await run_agent_with(
        db_sessionmaker, bot_row, fake, monkeypatch,
        [
            ("set_reply_target", {"message_id": 2}),
            ("attach_media", {"message_id": 6}),
        ],
    )

    async with db_sessionmaker() as s:
        run = await s.get(AgentRun, run_id)
        assert run.status == "done", run.error

    text_events = [e for e in fake.events if e[0] == "text"]
    # the reply text anchors to the chosen target (not the trigger #20)
    assert text_events[-1][2].get("reply_to_message_id") == 2
    # media follows the thread but never carries the reply link
    media_events = [e for e in fake.events if e[0] != "text"]
    assert media_events == [("video", "F6", {"message_thread_id": None})]


async def test_reply_target_routes_into_target_thread(db_sessionmaker, bot_row, monkeypatch):
    """A target in a forum topic pulls the whole reply — text AND media —
    into that topic's thread; only the text carries the reply link."""
    fake = MediaFakeBot()
    chat, run_id = await run_agent_with(
        db_sessionmaker, bot_row, fake, monkeypatch,
        [
            ("set_reply_target", {"message_id": 10}),  # lives in thread 77
            ("attach_media", {"message_id": 2}),
        ],
    )

    async with db_sessionmaker() as s:
        run = await s.get(AgentRun, run_id)
        assert run.status == "done", run.error

    text_events = [e for e in fake.events if e[0] == "text"]
    assert text_events[-1][2].get("reply_to_message_id") == 10
    assert text_events[-1][2].get("message_thread_id") == 77
    media_events = [e for e in fake.events if e[0] != "text"]
    assert media_events == [("photo", "F2", {"message_thread_id": 77})]


async def test_network_error_is_terminal_no_retry_no_notice(db_sessionmaker, bot_row, monkeypatch):
    """Only BadRequest walks the URL-drop ladder. On a network flake the album
    may have landed with the response lost — retrying could double-post and a
    failure notice could contradict what the chat sees, so the failure is
    terminal and silent (dashboard chip + run.error only)."""
    fake = MediaFakeBot()
    fake.fail_media = RuntimeError("connection reset")
    chat, run_id = await run_agent_with(
        db_sessionmaker, bot_row, fake, monkeypatch,
        [
            ("attach_media", {"message_id": 2}),
            ("attach_media_url", {"url": "https://pics.test/x.jpg"}),
        ],
    )

    async with db_sessionmaker() as s:
        run = await s.get(AgentRun, run_id)
        assert run.status == "done", run.error
        assert run.media == [
            {"kind": "photo", "source": "history", "ok": False},
            {"kind": "photo", "source": "url", "ok": False},
        ]
        assert "some media failed to send" in run.error
    # no fallback batch was attempted and no notice was posted
    media_events = [e for e in fake.events if e[0] != "text"]
    assert media_events == []
    assert "Media attachment failed." not in [m.text for m in fake.sent]


async def test_text_failure_skips_media(db_sessionmaker, bot_row, monkeypatch):
    """Media is only attempted when ALL text parts landed — a half-delivered
    reply must not be decorated with the album it promised."""
    fake = MediaFakeBot()
    chat, run_id = await run_agent_with(
        db_sessionmaker, bot_row, fake, monkeypatch, [("attach_media", {"message_id": 2})],
        arm=lambda: setattr(fake, "fail_text", bad_request()),
    )

    async with db_sessionmaker() as s:
        run = await s.get(AgentRun, run_id)
        assert run.status == "done", run.error  # outcome committed pre-delivery
        assert "delivery failed after 0 part(s)" in run.error
        assert "attached media skipped" in run.error
        assert run.media is None  # media never attempted, not "attempted and failed"
    assert [e for e in fake.events if e[0] != "text"] == []


async def run_workflow_with(db_sessionmaker, bot_row, fake, monkeypatch, tool_calls,
                            text="done"):
    import app.agents.runtime as runtime
    from app.models import IntentEpisode, Workflow

    model = media_model(tool_calls, text=text)
    monkeypatch.setattr(runtime, "build_model", lambda provider: model)
    chat = await authorize_chat(db_sessionmaker, fake, bot_row)
    await seed_media_messages(db_sessionmaker, chat.id)
    async with db_sessionmaker() as s:
        await add_agent_model(s)
        wf = Workflow(name="recap", type="intent", action_prompt="recap the day",
                      trigger_prompt="t", required_slots=[], threshold=0.5)
        s.add(wf)
        await s.flush()
        run = AgentRun(chat_id=chat.id, trigger="workflow", workflow_id=wf.id,
                       request_text="recap the day")
        s.add(run)
        await s.flush()
        s.add(IntentEpisode(workflow_id=wf.id, chat_id=chat.id, status="fired",
                            summary="anniversary", agent_run_id=run.id,
                            fired_at=datetime.now(timezone.utc)))
        await s.commit()
        run_id = run.id
    await execute_run(db_sessionmaker, FakeEmbedder(), SendLimiter(), fake, run_id)
    return chat, run_id


async def test_no_action_discards_media(db_sessionmaker, bot_row, monkeypatch):
    from app.models import IntentEpisode

    fake = MediaFakeBot()
    chat, run_id = await run_workflow_with(
        db_sessionmaker, bot_row, fake, monkeypatch,
        [("attach_media", {"message_id": 2}), ("set_reply_target", {"message_id": 6})],
        text="NO_ACTION: recap already posted",
    )

    async with db_sessionmaker() as s:
        run = await s.get(AgentRun, run_id)
        assert run.status == "declined", run.error
        assert run.media is None
        ep = (await s.execute(select(IntentEpisode))).scalar_one()
        assert "[attaching" not in (ep.execution_summary or "")
    # nothing was posted by the run: no media, and the only text send is the
    # authorization prompt from chat setup
    assert [e for e in fake.events if e[0] != "text"] == []
    assert all("NO_ACTION" not in m.text for m in fake.sent)
    assert len(fake.sent) == 1  # the auth prompt only


async def test_workflow_episode_annotated_with_attach_intent(db_sessionmaker, bot_row, monkeypatch):
    from app.models import IntentEpisode

    fake = MediaFakeBot()
    chat, run_id = await run_workflow_with(
        db_sessionmaker, bot_row, fake, monkeypatch,
        [("attach_media", {"message_id": 2})], text="posted the recap",
    )
    async with db_sessionmaker() as s:
        ep = (await s.execute(select(IntentEpisode))).scalar_one()
        assert ep.execution_summary == "posted the recap [attaching 1 photo]"


async def test_workflow_episode_corrected_on_media_failure(db_sessionmaker, bot_row, monkeypatch):
    """On delivery failure the pre-committed episode annotation is corrected
    so future dedup reasoning doesn't assume the media landed."""
    from app.models import IntentEpisode

    fake = MediaFakeBot()
    fake.fail_media = bad_request()
    chat, run_id = await run_workflow_with(
        db_sessionmaker, bot_row, fake, monkeypatch,
        [("attach_media", {"message_id": 2})], text="posted the recap",
    )
    async with db_sessionmaker() as s:
        ep = (await s.execute(select(IntentEpisode))).scalar_one()
        assert ep.execution_summary == (
            "posted the recap [attaching 1 photo] (some media failed to send)"
        )
