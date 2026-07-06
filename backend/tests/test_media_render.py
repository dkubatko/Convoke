"""message_body() is the seam every renderer (chunks, intent prompts, agent
context) uses instead of Message.text — these pin the annotation formats."""

from datetime import datetime, timezone

from app.media.render import message_body
from app.memory.chunker import render_message
from app.models import Message, MessageAttachment

T0 = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


def media_msg(text: str = "", **att_fields) -> Message:
    m = Message(
        chat_id=1,
        tg_message_id=1,
        sender_id=1,
        sender_name="Alice",
        text=text,
        sent_at=T0,
    )
    defaults = dict(
        chat_id=1, tg_message_id=1, kind="photo", file_id="f", file_unique_id="u", status="pending"
    )
    m.attachment = MessageAttachment(**{**defaults, **att_fields})
    return m


def test_plain_text_message_unchanged():
    m = Message(chat_id=1, tg_message_id=1, sender_name="Alice", text="hello", sent_at=T0)
    assert message_body(m) == "hello"


def test_pending_photo_placeholder():
    assert message_body(media_msg()) == "[photo — description pending]"


def test_described_photo_with_caption():
    m = media_msg(text="look!", status="described", description="movie tickets for Dune, Jul 12 7pm")
    assert message_body(m) == "[photo: movie tickets for Dune, Jul 12 7pm] look!"


def test_voice_transcript_with_duration():
    m = media_msg(kind="voice", status="described", duration_s=72, transcript="see you at seven")
    assert message_body(m) == '[voice 1:12: "see you at seven"]'


def test_video_with_description_and_transcript():
    m = media_msg(kind="video", status="described", duration_s=63, description="a dog chasing a ball", transcript="go get it!")
    assert message_body(m) == '[video 1:03: a dog chasing a ball — audio: "go get it!"]'


def test_sticker_includes_emoji():
    m = media_msg(kind="sticker", sticker_emoji="😂", status="described", description="a laughing cat")
    assert message_body(m) == "[sticker 😂: a laughing cat]"


def test_failed_and_skipped_states():
    assert message_body(media_msg(status="failed")) == "[photo — could not be analyzed]"
    m = media_msg(status="skipped", error="no vision model configured")
    assert message_body(m) == "[photo — no vision model configured]"


def test_render_message_carries_annotation():
    m = media_msg(status="described", description="two friends on a picnic blanket")
    assert render_message(m) == (
        "Alice [2026-07-01 12:00]: [photo: two friends on a picnic blanket]"
    )


def test_render_segment_annotates_media_and_quoted_media_reply():
    from app.memory.chunker import Segment, render_segment

    photo = media_msg(status="described", description="movie tickets for Dune")
    reply = Message(chat_id=1, tg_message_id=2, sender_name="Bob", text="I'm in!",
                    sent_at=T0, reply_to_tg_message_id=1)
    seg = Segment(thread_id=None, messages=[reply], tg_id_start=2, tg_id_end=2)
    out = render_segment(seg, reply_targets={1: photo})
    assert '↳ (replies to Alice: "[photo: movie tickets for Dune]")' in out

    seg_with_media = Segment(thread_id=None, messages=[photo, reply], tg_id_start=1, tg_id_end=2)
    out = render_segment(seg_with_media)
    assert "[photo: movie tickets for Dune]" in out.splitlines()[0]


def test_gate_texts_components():
    from app.intent.pipeline import gate_texts

    plain = Message(chat_id=1, tg_message_id=1, sender_name="A", text="hello", sent_at=T0)
    assert gate_texts(plain, None) == ["hello"]  # identical to pre-media behavior

    photo = media_msg(status="described", description="a long screenshot description")
    assert gate_texts(photo, None) == ["[photo: a long screenshot description]"]

    captioned = media_msg(text="wanna see", status="described", description="shrek card")
    assert gate_texts(captioned, None) == [
        "wanna see",
        "[photo: shrek card]",
        "[photo: shrek card] wanna see",
    ]

    # Reply composition is an ADDITIONAL candidate, not a replacement.
    reply = Message(chat_id=1, tg_message_id=2, sender_name="B", text="cool!",
                    sent_at=T0, reply_to_tg_message_id=1)
    texts = gate_texts(reply, captioned)
    assert texts[0] == "cool!"
    assert texts[-1] == "[photo: shrek card] wanna see\ncool!"
