"""Textual rendering of media attachments.

message_body() is the single seam every renderer uses instead of Message.text
— memory chunks, the intent prefilter and transcripts, and agent context all
route through it — so a media message reads identically everywhere:

    [photo: two friends on a picnic blanket, food spread out] optional caption
    [voice 0:12: "okay let's meet at seven then"]
    [video 1:03 — description pending]
    [poll «Dinner?», multiple answers: Fri / Sat]
    [location 55.75124, 37.61842]
"""

from sqlalchemy import inspect as sa_inspect

from app.models.telegram import Message, MessageAttachment, Poll

_KIND_LABELS = {
    "photo": "photo",
    "video": "video",
    "voice": "voice",
    "video_note": "video note",
    "sticker": "sticker",
    "image_document": "document",
    "audio": "audio",
    "file": "file",
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


def _size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{max(1, n // 1024)} KB"


def attachment_annotation(att: MessageAttachment) -> str:
    if att.kind == "file":
        # Not described by the pipeline — the name is the content's index entry.
        meta = att.file_name or "unnamed"
        if att.size_bytes:
            meta += f", {_size(att.size_bytes)}"
        return f"[file {meta}]"
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


def _coords(c: dict) -> str:
    return f"{c['latitude']:.5f}, {c['longitude']:.5f}"


def poll_annotation(p: Poll) -> str:
    """Question and options — what doesn't change. Vote counts stay out (they
    move constantly; get_poll reads them live) except on a closed poll, whose
    tally is final."""
    head = f"poll «{p.question}»"
    if p.description:
        head += f" ({p.description})"
    flags = [
        f for f, on in (
            ("quiz", p.type == "quiz"),
            ("multiple answers", p.multiple_answers),
            ("closed", p.closed),
        ) if on
    ]
    if flags:
        head += ", " + ", ".join(flags)
    final = p.closed and p.counts_at is not None
    opts = " / ".join(
        o["text"] + (f" ({o['votes']})" if final and o.get("votes") is not None else "")
        for o in p.options
    )
    return f"[{head}: {opts}]"


def content_annotation(c: dict) -> str:
    t = c.get("type")
    if t == "location":
        label = "live location" if c.get("live_period") else "location"
        return f"[{label} {_coords(c)}]"
    if t == "venue":
        where = ", ".join(x for x in (c.get("title"), c.get("address")) if x)
        if c.get("latitude") is not None:
            where += f" ({_coords(c)})"
        return f"[venue: {where}]"
    if t == "contact":
        name = " ".join(x for x in (c.get("first_name"), c.get("last_name")) if x)
        return f"[contact: {name}, {c.get('phone_number') or 'no phone'}]"
    if t == "dice":
        return f"[{c.get('emoji')} dice roll: {c.get('value')}]"
    if t == "checklist":
        def task(x: dict) -> str:
            if x.get("done") is None:  # imported: completion state unknown
                return x["text"]
            mark = "✓" if x["done"] else "☐"
            by = f" ({x['done_by']})" if x.get("done") and x.get("done_by") else ""
            return f"{mark} {x['text']}{by}"
        tasks = " / ".join(task(x) for x in c.get("tasks", []))
        return f"[checklist «{c.get('title') or ''}»: {tasks}]"
    if t == "story":
        return f"[forwarded story from {c.get('from') or 'unknown'}]"
    if t == "game":
        return f"[game: {c.get('title') or ''}]"
    if t == "invoice":
        return f"[invoice: {c.get('title') or ''}]"
    if t == "giveaway":
        prize = f": {c['prize']}" if c.get("prize") else ""
        return f"[giveaway{prize}]"
    if t == "paid_media":
        return "[paid media]"
    return f"[{t or 'unknown content'}]"


def _poll_of(m: Message) -> Poll | None:
    """m.poll without a lazy load: queried rows have it eagerly loaded, and a
    row built in code that never had one assigned has no poll — reading the
    unloaded attribute would only crash (async) or hit the DB to learn that."""
    state = sa_inspect(m, raiseerr=False)
    if state is not None and "poll" in state.unloaded:
        return None
    return m.poll


def message_body(m: Message) -> str:
    """The message's content as text: annotations for its media, poll, or
    structured content (if any), followed by the text/caption. Use this
    everywhere instead of m.text."""
    parts = []
    if m.attachment is not None:
        parts.append(attachment_annotation(m.attachment))
    poll = _poll_of(m)
    if poll is not None:
        parts.append(poll_annotation(poll))
    if m.content:
        parts.append(content_annotation(m.content))
    if m.text:
        parts.append(m.text)
    return " ".join(parts)
