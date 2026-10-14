"""Telegram's non-file message content: polls, locations, contacts, checklists…

Each is stored structured — polls in their own table (they change through a
separate update stream keyed by poll id), everything else as Message.content —
and rendered at read time by app.media.render, so content that changes after
sending (live locations, checklist ticks, poll closes) never goes stale in a
transcript.
"""

from datetime import datetime, timezone

from aiogram.types import Message as TgMessage
from aiogram.types import Poll as TgPoll

from app.models import Poll


def _user_name(user) -> str | None:
    return user.full_name if user is not None else None


def checklist_tasks(tasks) -> list[dict]:
    return [
        {
            "id": t.id,
            "text": t.text,
            "done": bool(t.completion_date),
            "done_by": _user_name(t.completed_by_user)
            or (t.completed_by_chat.title if t.completed_by_chat else None),
        }
        for t in tasks
    ]


def extract_content(msg: TgMessage) -> dict | None:
    """The message's structured non-file content as Message.content, or None.
    Polls are not content — see poll_from_tg."""
    if msg.venue:  # checked before location: a venue message also carries one
        v = msg.venue
        return {
            "type": "venue",
            "title": v.title,
            "address": v.address,
            "latitude": v.location.latitude,
            "longitude": v.location.longitude,
        }
    if msg.location:
        loc = msg.location
        content = {"type": "location", "latitude": loc.latitude, "longitude": loc.longitude}
        if loc.live_period:
            content["live_period"] = loc.live_period
        return content
    if msg.contact:
        c = msg.contact
        return {
            "type": "contact",
            "first_name": c.first_name,
            "last_name": c.last_name,
            "phone_number": c.phone_number,
            "user_id": c.user_id,
        }
    if msg.dice:
        return {"type": "dice", "emoji": msg.dice.emoji, "value": msg.dice.value}
    if msg.checklist:
        return {
            "type": "checklist",
            "title": msg.checklist.title,
            "tasks": checklist_tasks(msg.checklist.tasks),
        }
    if msg.story:
        chat = msg.story.chat
        return {"type": "story", "from": chat.title or chat.full_name or chat.username}
    if msg.game:
        return {"type": "game", "title": msg.game.title}
    if msg.invoice:
        return {
            "type": "invoice",
            "title": msg.invoice.title,
            "amount": msg.invoice.total_amount,
            "currency": msg.invoice.currency,
        }
    if msg.giveaway:
        return {
            "type": "giveaway",
            "prize": msg.giveaway.prize_description,
            "winners": msg.giveaway.winner_count,
        }
    if msg.paid_media:
        return {"type": "paid_media", "stars": msg.paid_media.star_count}
    return None


def _options(tg_poll: TgPoll, with_counts: bool) -> list[dict]:
    return [
        {
            # persistent_id survives option adds/deletes; older payloads lack
            # it, and then the position is the only stable handle there is.
            "id": o.persistent_id if o.persistent_id is not None else str(i),
            "text": o.text,
            "votes": o.voter_count if with_counts else None,
        }
        for i, o in enumerate(tg_poll.options)
    ]


def poll_from_tg(tg_poll: TgPoll, *, sent_by_bot: bool, sent_at: datetime) -> Poll:
    """A Poll row for a poll seen in a message (chat/message ids and the
    message link are filled in by the caller). Counts are trusted only where
    Telegram reports them to bots: the bot's own polls (zero at sending) and
    polls already closed. A member's open poll gets counts_at NULL — votes
    unknown, never a misleading zero."""
    trusted = sent_by_bot or bool(tg_poll.is_closed)
    return Poll(
        tg_poll_id=tg_poll.id,
        question=tg_poll.question,
        description=tg_poll.description,
        options=_options(tg_poll, trusted),
        type=tg_poll.type or "regular",
        multiple_answers=tg_poll.allows_multiple_answers,
        anonymous=tg_poll.is_anonymous,
        sent_by_bot=sent_by_bot,
        closed=bool(tg_poll.is_closed),
        close_date=_as_dt(tg_poll.close_date),
        total_voters=tg_poll.total_voter_count if trusted else None,
        counts_at=sent_at if trusted else None,
    )


def apply_poll_snapshot(row: Poll, tg_poll: TgPoll, now: datetime) -> bool:
    """Overwrite a stored poll with a `poll` update — a full, authoritative
    snapshot. Returns True when the closed state flipped (the rendered line
    changes, so memory covering the message needs a refresh)."""
    was_closed = row.closed
    row.question = tg_poll.question
    if tg_poll.description is not None:
        row.description = tg_poll.description
    row.options = _options(tg_poll, True)
    row.total_voters = tg_poll.total_voter_count
    row.closed = bool(tg_poll.is_closed)
    row.close_date = _as_dt(tg_poll.close_date) or row.close_date
    row.counts_at = now
    return row.closed != was_closed


def _as_dt(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    return datetime.fromtimestamp(int(value), tz=timezone.utc)
