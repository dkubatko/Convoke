"""MediaLoop: pending attachments become descriptions/transcripts; chunks go
stale so memory re-embeds; missing models skip (and requeue later); failures
back off and cap out."""

import io
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.crypto import encrypt
from app.media.loop import MediaLoop
from app.models import (
    Bot,
    Chat,
    Chunk,
    ConnectedModel,
    Message,
    MessageAttachment,
    ModelRoleAssignment,
)
from app.telegram.client import BotCache

T0 = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


class FakeTgBot:
    def __init__(self):
        self.downloads: list[str] = []

    async def download(self, file_id):
        self.downloads.append(file_id)
        return io.BytesIO(b"bytes:" + file_id.encode())

    class session:  # noqa: N801 — aiogram interface shim for BotCache.aclose
        @staticmethod
        async def close():
            pass


class FakeDescriber:
    def __init__(self, fail_times: int = 0, sampled: list[bytes] | None = None):
        self.fail_times = fail_times
        self.sampled = sampled or []  # what ffmpeg "extracts"
        self.calls: list[tuple] = []

    async def describe_image(self, provider, data, mime, caption=""):
        self.calls.append(("image", data, mime, caption))
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("model exploded")
        return f"described({data.decode()})"

    async def describe_frames(self, provider, frames, caption="", transcript=None):
        self.calls.append(("frames", frames, caption, transcript))
        return f"video-desc({len(frames)} frames, audio={transcript is not None})"

    async def describe_video_native(self, provider, data, mime, caption=""):
        self.calls.append(("native", data, mime, caption))
        return "native-video-desc"

    async def sample_frames(self, data, count, duration_s):
        return self.sampled

    async def transcribe(self, provider, data, filename, mime):
        self.calls.append(("audio", data, filename, mime))
        return f"transcribed({data.decode()})"


async def seed(db_sessionmaker, *, kind="photo", roles=("vision", "transcription"), **att_extra):
    """Bot + chat + one media message (+ covering chunk) + assigned models."""
    async with db_sessionmaker() as s:
        bot = Bot(tg_bot_id=999, username="b", name="b", token_encrypted=encrypt("123:fake"),
                  can_read_all_group_messages=True)
        s.add(bot)
        await s.flush()
        chat = Chat(bot_id=bot.id, tg_chat_id=-100, type="supergroup", status="authorized")
        s.add(chat)
        await s.flush()
        msg = Message(chat_id=chat.id, tg_message_id=20, sender_name="Alice",
                      text=att_extra.pop("caption", ""), sent_at=T0)
        att_fields = dict(chat_id=chat.id, tg_message_id=20, kind=kind, file_id="f-1",
                          file_unique_id="u-1", status="pending")
        msg.attachment = MessageAttachment(**{**att_fields, **att_extra})
        s.add(msg)
        s.add(Chunk(chat_id=chat.id, thread_id=None, msg_tg_id_start=10, msg_tg_id_end=30,
                    text="old render", stale=False, content_version=0))
        for role in roles:
            m = ConnectedModel(name=f"{role}-m", base_url="http://unused", model_name="m",
                               capabilities={role: True})
            s.add(m)
            await s.flush()
            s.add(ModelRoleAssignment(role=role, model_id=m.id))
        await s.commit()
        return bot.id, msg.id


def make_loop(db_sessionmaker, bot_id, describer) -> tuple[MediaLoop, FakeTgBot]:
    cache = BotCache()
    fake = FakeTgBot()
    cache.put(bot_id, fake)
    return MediaLoop(db_sessionmaker, bots=cache, describer=describer), fake


async def get_att(db_sessionmaker) -> MessageAttachment:
    async with db_sessionmaker() as s:
        return (await s.execute(select(MessageAttachment))).scalar_one()


async def test_photo_described_and_chunk_marked_stale(db_sessionmaker):
    bot_id, _ = await seed(db_sessionmaker, caption="movie night?")
    loop, fake = make_loop(db_sessionmaker, bot_id, FakeDescriber())
    await loop._tick()

    att = await get_att(db_sessionmaker)
    assert att.status == "described"
    assert att.description == "described(bytes:f-1)"
    assert att.described_at is not None
    assert fake.downloads == ["f-1"]
    async with db_sessionmaker() as s:
        chunk = (await s.execute(select(Chunk))).scalar_one()
        assert chunk.stale is True
        assert chunk.content_version == 1


async def test_voice_transcribed(db_sessionmaker):
    bot_id, _ = await seed(db_sessionmaker, kind="voice", mime="audio/ogg", duration_s=12)
    describer = FakeDescriber()
    loop, _ = make_loop(db_sessionmaker, bot_id, describer)
    await loop._tick()

    att = await get_att(db_sessionmaker)
    assert att.status == "described"
    assert att.transcript == "transcribed(bytes:f-1)"
    assert describer.calls[0][:1] == ("audio",)
    assert describer.calls[0][2] == "voice.ogg"


async def test_video_described_from_thumb_frames_and_transcript(db_sessionmaker):
    """Fallback path: thumbnail + sampled frames go to the vision model as one
    multi-image call, composed with the audio transcript."""
    bot_id, _ = await seed(db_sessionmaker, kind="video", mime="video/mp4",
                           thumb_file_id="thumb-1", size_bytes=1000, duration_s=30)
    describer = FakeDescriber(sampled=[b"fr1", b"fr2"])
    loop, fake = make_loop(db_sessionmaker, bot_id, describer)
    await loop._tick()

    att = await get_att(db_sessionmaker)
    assert att.status == "described"
    assert att.description == "video-desc(3 frames, audio=True)"  # thumb + 2 sampled
    assert att.transcript == "transcribed(bytes:f-1)"
    assert fake.downloads == ["f-1", "thumb-1"]
    frames_call = next(c for c in describer.calls if c[0] == "frames")
    assert frames_call[1][0] == b"bytes:thumb-1"  # thumbnail leads
    assert frames_call[3] == "transcribed(bytes:f-1)"  # transcript composed in


async def test_video_native_path_used_when_video_role_assigned(db_sessionmaker):
    bot_id, _ = await seed(db_sessionmaker, kind="video", mime="video/mp4",
                           thumb_file_id="thumb-1", size_bytes=1000,
                           roles=("vision", "transcription", "video"))
    describer = FakeDescriber(sampled=[b"fr1"])
    loop, _ = make_loop(db_sessionmaker, bot_id, describer)
    await loop._tick()

    att = await get_att(db_sessionmaker)
    assert att.description == "native-video-desc"
    assert att.transcript == "transcribed(bytes:f-1)"
    assert not any(c[0] == "frames" for c in describer.calls)  # fallback not used


async def test_oversized_video_described_from_thumbnail_only(db_sessionmaker):
    bot_id, _ = await seed(db_sessionmaker, kind="video", thumb_file_id="thumb-1",
                           size_bytes=50 * 2**20)
    loop, fake = make_loop(db_sessionmaker, bot_id, FakeDescriber(sampled=[b"never"]))
    await loop._tick()

    att = await get_att(db_sessionmaker)
    assert att.status == "described"
    assert att.description == "video-desc(1 frames, audio=False) (large video — described from thumbnail only)"
    assert att.transcript is None
    assert fake.downloads == ["thumb-1"]  # the 50MB file was never fetched


async def test_sample_frames_degrades_without_ffmpeg(monkeypatch):
    from app.media import describe as describe_mod
    from app.media.describe import Describer

    monkeypatch.setattr(describe_mod.shutil, "which", lambda _: None)
    assert await Describer().sample_frames(b"videobytes", 3, 30) == []


async def test_photo_without_vision_model_is_skipped(db_sessionmaker):
    bot_id, _ = await seed(db_sessionmaker, roles=())
    loop, fake = make_loop(db_sessionmaker, bot_id, FakeDescriber())
    await loop._tick()

    att = await get_att(db_sessionmaker)
    assert att.status == "skipped"
    assert att.error == "no vision model configured"
    assert fake.downloads == []


async def test_failure_backs_off_then_caps_at_failed(db_sessionmaker):
    bot_id, _ = await seed(db_sessionmaker)
    loop, _ = make_loop(db_sessionmaker, bot_id, FakeDescriber(fail_times=10))
    await loop._tick()

    att = await get_att(db_sessionmaker)
    assert att.status == "pending"
    assert att.attempts == 1
    assert "model exploded" in att.error

    # Within backoff: not retried.
    await loop._tick()
    assert (await get_att(db_sessionmaker)).attempts == 1

    # Force the backoff window to elapse for two more rounds → failed.
    for expected in (2, 3):
        async with db_sessionmaker() as s:
            a = (await s.execute(select(MessageAttachment))).scalar_one()
            a.last_attempt_at = datetime.now(timezone.utc) - timedelta(hours=1)
            await s.commit()
        await loop._tick()
        att = await get_att(db_sessionmaker)
        assert att.attempts == expected
    assert att.status == "failed"


async def test_backlog_drains_in_parallel(db_sessionmaker):
    """Model calls for a tick's batch run concurrently (media_describe_concurrency),
    so a burst of photos doesn't serialize on model latency."""
    import asyncio

    bot_id, _ = await seed(db_sessionmaker)  # tg 20
    async with db_sessionmaker() as s:
        chat_id = (await s.execute(select(Chat.id))).scalar_one()
        for tg in (21, 22):
            m = Message(chat_id=chat_id, tg_message_id=tg, sender_name="A", text="", sent_at=T0)
            m.attachment = MessageAttachment(
                chat_id=chat_id, tg_message_id=tg, kind="photo",
                file_id=f"f-{tg}", file_unique_id=f"u-{tg}", status="pending",
            )
            s.add(m)
        await s.commit()

    class SlowDescriber(FakeDescriber):
        def __init__(self):
            super().__init__()
            self.in_flight = 0
            self.peak = 0

        async def describe_image(self, provider, data, mime, caption=""):
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
            await asyncio.sleep(0.05)
            self.in_flight -= 1
            return await super().describe_image(provider, data, mime, caption)

    describer = SlowDescriber()
    loop, _ = make_loop(db_sessionmaker, bot_id, describer)
    await loop._tick()

    assert describer.peak == 3  # all three photos in flight together
    async with db_sessionmaker() as s:
        statuses = (await s.execute(select(MessageAttachment.status))).scalars().all()
        assert statuses == ["described"] * 3
