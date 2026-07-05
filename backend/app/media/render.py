"""Textual rendering of media attachments.

message_body() is the single seam every renderer uses instead of Message.text
— memory chunks, the intent prefilter and transcripts, and agent context all
route through it — so a media message reads identically everywhere:

    [photo: two friends on a picnic blanket, food spread out] optional caption
    [voice 0:12: "okay let's meet at seven then"]
    [video 1:03 — description pending]
"""

from app.models.telegram import Message, MessageAttachment

_KIND_LABELS = {
    "photo": "photo",
    "video": "video",
    "voice": "voice",
    "video_note": "video note",
    "sticker": "sticker",
    "image_document": "document",
    "audio": "audio",
}


def _duration(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}"


def attachment_label(att: MessageAttachment) -> str:
    label = _KIND_LABELS.get(att.kind, att.kind)
    if att.kind == "sticker" and att.sticker_emoji:
        label = f"{label} {att.sticker_emoji}"
    if att.duration_s:
        label = f"{label} {_duration(att.duration_s)}"
    return label


def attachment_annotation(att: MessageAttachment) -> str:
    label = attachment_label(att)
    if att.status == "described":
        if att.description and att.transcript:
            return f'[{label}: {att.description} — audio: "{att.transcript}"]'
        if att.transcript:
            return f'[{label}: "{att.transcript}"]'
        return f"[{label}: {att.description or ''}]"
    if att.status == "failed":
        return f"[{label} — could not be analyzed]"
    if att.status == "skipped":
        return f"[{label} — {att.error or 'not analyzed'}]"
    return f"[{label} — description pending]"


def message_body(m: Message) -> str:
    """The message's content as text: the attachment annotation (if any)
    followed by the text/caption. Use this everywhere instead of m.text."""
    att = m.attachment
    if att is None:
        return m.text
    annotation = attachment_annotation(att)
    return f"{annotation} {m.text}" if m.text else annotation
