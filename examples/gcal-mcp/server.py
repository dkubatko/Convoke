"""A Google Calendar MCP server for Convoke — one service account, many calendars.

Share the bot's service-account email onto each group's calendar (or let it
create them), and the agent targets calendars **by name or id** on every call.
One shim, one service account, any number of groups.

There is no server-side default calendar: which calendar a group uses is the
agent's job to remember (in Convoke's chat memory) and pass on each call.

Tools:
  list_calendars()                    calendars the bot can see (its subscribed list)
  add_calendar(calendar_id)           subscribe a calendar shared by id → makes it
                                      discoverable by name (a one-time setup step)
  create_calendar(name, members)      create a new bot-owned calendar and share it
  create_event / find_events / update_event / delete_event
                                      each takes a required `calendar` (name or id);
                                      events may be all-day and/or recurring

The gotcha this design works around: Google Calendar's CalendarList API is
UNRELIABLE for service accounts — `calendarList.insert` returns a full success
and the entry silently never lands (measured live: insert → list in the same
process returned zero entries, even for a calendar the account OWNS). So names
can never depend on Google's list. Instead, `add_calendar`/`create_calendar`
record the name→id mapping in a small JSON file on a volume, and name
resolution consults that registry first; the live Google list is merged in as
a bonus wherever it happens to work.

Concurrency: FastMCP runs these sync tools on worker threads, and the agent
fires tool calls in parallel. httplib2 connections are not thread-safe — one
shared client corrupted its SSL stream under eight parallel create_event
calls (measured live: "SSL record layer failure", then read timeouts). Every
request therefore executes on its own authorized connection, with the client
library's retries; creates carry a client-chosen event id so a retried insert
that already landed comes back as the existing event, never a duplicate.

Environment:
  GOOGLE_SERVICE_ACCOUNT_FILE  path to the service-account JSON key
  CALENDAR_ALIASES_FILE        persistent name→id registry (default
                               /data/calendars.json; created on first start —
                               put it on a volume or registrations die with
                               the container)
  PORT                         HTTP port to serve on (default 8000)
"""

import json
import os
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

import google_auth_httplib2
from fastmcp import FastMCP
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import build_http

SCOPES = ["https://www.googleapis.com/auth/calendar"]

_creds = service_account.Credentials.from_service_account_file(
    os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"], scopes=SCOPES
)
_calendar = build("calendar", "v3", credentials=_creds, cache_discovery=False)
# Transient failures (5xx, 429, dropped connections) retried with backoff.
RETRIES = 3


def _run(request):
    """Execute on a fresh connection — never the shared one (see Concurrency)."""
    http = google_auth_httplib2.AuthorizedHttp(_creds, http=build_http())
    return request.execute(http=http, num_retries=RETRIES)

mcp = FastMCP("Calendar")

_ALIASES = Path(os.environ.get("CALENDAR_ALIASES_FILE", "/data/calendars.json"))
_ALIASES.parent.mkdir(parents=True, exist_ok=True)
if not _ALIASES.exists():
    _ALIASES.write_text("{}")


def _load_aliases() -> dict[str, str]:
    """name (as registered) → calendar id."""
    try:
        return json.loads(_ALIASES.read_text())
    except (OSError, ValueError):
        return {}


def _save_alias(name: str, calendar_id: str) -> None:
    aliases = _load_aliases()
    aliases[name] = calendar_id
    _ALIASES.write_text(json.dumps(aliases, ensure_ascii=False, indent=1))


def _calendar_list() -> list[dict]:
    """Every calendar in the bot's list (subscribed + owned), across pages."""
    items, page = [], None
    while True:
        resp = _run(_calendar.calendarList().list(pageToken=page))
        items += resp.get("items", [])
        page = resp.get("nextPageToken")
        if not page:
            return items


def _resolve_calendar(calendar: str) -> str:
    """Turn a calendar name or id into a calendar id: the persistent registry
    first (the only durable source with a service account), then whatever the
    live Google list happens to contain."""
    if "@" in calendar or calendar == "primary":
        return calendar  # already an id
    aliases = _load_aliases()
    for name, cal_id in aliases.items():
        if name.lower() == calendar.lower():
            return cal_id
    for c in _calendar_list():
        if c.get("summary", "").lower() == calendar.lower():
            return c["id"]
    known = ", ".join(sorted(aliases)) or "(none registered yet)"
    raise ValueError(
        f"No calendar named {calendar!r}. Known: {known}. If it was shared by id, "
        "run add_calendar with that id first."
    )


def _summarize(ev: dict) -> dict:
    """Trim a Google event to what the agent needs to reason about and act on."""
    start, end = ev.get("start", {}), ev.get("end", {})
    all_day = "date" in start
    out = {
        "id": ev["id"],
        "title": ev.get("summary", ""),
        "start": start.get("dateTime") or start.get("date"),
        # All-day: the last day, as the tools take it — not Google's exclusive end.
        "end": _shift(end["date"], -1) if all_day else end.get("dateTime"),
        "all_day": all_day,
        "location": ev.get("location", ""),
        "link": ev.get("htmlLink", ""),
    }
    if ev.get("recurrence"):
        out["recurrence"] = ev["recurrence"]
    if ev.get("recurringEventId"):
        # An occurrence of a series: edit/delete the series via this id.
        out["series_id"] = ev["recurringEventId"]
    return out


def _day(value: str) -> str:
    """The date part of an ISO date or datetime ("2027-01-26T00:00:00" → "2027-01-26")."""
    return date.fromisoformat(value[:10]).isoformat()


def _shift(day: str, days: int) -> str:
    return (date.fromisoformat(day) + timedelta(days=days)).isoformat()


def _times(start: str, end: str | None, all_day: bool, timezone: str) -> tuple[dict, dict]:
    """Google's start/end objects. For all-day events `end` is the LAST day
    (omitted: a single day); Google's end date is exclusive, hence the +1."""
    if all_day:
        first = _day(start)
        last = max(_day(end), first) if end else first
        return {"date": first}, {"date": _shift(last, 1)}
    if not end:
        raise ValueError("A timed event needs an end; pass all_day=true for a whole-day event.")
    return {"dateTime": start, "timeZone": timezone}, {"dateTime": end, "timeZone": timezone}


def _check_recurrence(recurrence: list[str]) -> list[str]:
    rules = [r.strip() for r in recurrence if r.strip()]
    bad = [r for r in rules if not r.upper().startswith(("RRULE:", "EXDATE", "RDATE"))]
    if bad:
        raise ValueError(
            f"Recurrence lines must be RFC 5545 rules like 'RRULE:FREQ=YEARLY'; got {bad}."
        )
    return rules


# --- calendar management (one-time setup, usually operator-triggered) ----------


@mcp.tool
def list_calendars() -> list[dict]:
    """List the calendars this bot can use, as {id, name, access}. Pass a name as
    the `calendar` argument to the event tools to target one."""
    out = {
        cal_id: {"id": cal_id, "name": name, "access": "registered"}
        for name, cal_id in _load_aliases().items()
    }
    for c in _calendar_list():  # merge the live list where Google provides one
        out[c["id"]] = {"id": c["id"], "name": c.get("summary", ""), "access": c.get("accessRole")}
    return list(out.values())


@mcp.tool
def add_calendar(calendar_id: str) -> dict:
    """Make a calendar that was shared with the bot **by id** durably addressable
    by name. Run once per calendar, after sharing the bot's service-account email
    onto it."""
    # calendars().get is the access check AND the name source of truth; the
    # CalendarList insert is attempted as a bonus but its result is untrusted
    # (service-account CalendarLists drop entries silently).
    c = _run(_calendar.calendars().get(calendarId=calendar_id))
    name = c.get("summary", calendar_id)
    try:
        _run(_calendar.calendarList().insert(body={"id": calendar_id}))
    except Exception:  # noqa: BLE001 — registry below is the real registration
        pass
    _save_alias(name, calendar_id)
    return {"id": calendar_id, "name": name, "access": "registered"}


@mcp.tool
def create_calendar(name: str, member_emails: list[str] = []) -> dict:
    """Create a new calendar owned by the bot and share it (view access) with the
    given member emails. It's immediately usable by name — no add_calendar needed."""
    cal = _run(_calendar.calendars().insert(body={"summary": name}))
    for email in member_emails:
        _run(_calendar.acl().insert(
            calendarId=cal["id"],
            body={"role": "reader", "scope": {"type": "user", "value": email}},
        ))
    _save_alias(cal.get("summary", name), cal["id"])
    return {"id": cal["id"], "name": cal.get("summary", name)}


# --- events -------------------------------------------------------------------


@mcp.tool
def create_event(
    title: str,
    start: str,
    calendar: str,
    end: str | None = None,
    all_day: bool = False,
    recurrence: list[str] | None = None,
    description: str = "",
    location: str = "",
    timezone: str = "UTC",
) -> dict:
    """Create an event on `calendar` (its name or id — required).

    Timed event: `start` and `end` are ISO-8601 datetimes, e.g.
    "2026-07-14T19:00:00", in `timezone`. Whole-day event (birthdays,
    holidays, trips): all_day=true with `start` as a date ("2027-01-26"); `end`
    is the last day of a multi-day span — omit it for a single day.
    Repeating event: `recurrence` takes RFC 5545 rules, e.g.
    ["RRULE:FREQ=YEARLY"] for every year, ["RRULE:FREQ=WEEKLY;BYDAY=TU;COUNT=10"].
    Returns the created event (id + link) for later edits.
    """
    start_obj, end_obj = _times(start, end, all_day, timezone)
    cal_id = _resolve_calendar(calendar)
    # Client-chosen id (base32hex: hex digits qualify) makes the insert
    # idempotent across retries — see Concurrency.
    event_id = uuid.uuid4().hex
    body = {
        "id": event_id,
        "summary": title,
        "description": description,
        "location": location,
        "start": start_obj,
        "end": end_obj,
    }
    if recurrence:
        body["recurrence"] = _check_recurrence(recurrence)
    try:
        ev = _run(_calendar.events().insert(calendarId=cal_id, body=body))
    except HttpError as e:
        if e.resp.status != 409:
            raise
        # A retry of an insert that had already landed: return that event.
        ev = _run(_calendar.events().get(calendarId=cal_id, eventId=event_id))
    return _summarize(ev)


@mcp.tool
def find_events(
    time_min: str, time_max: str, calendar: str, query: str = ""
) -> list[dict]:
    """List events on `calendar` (name or id) between two ISO-8601 dates or
    datetimes. Use this to locate an event's id before updating or deleting it.
    Repeating events are listed once per occurrence, each with `series_id` —
    pass that id to update_event/delete_event to change the whole series.
    `query` full-text-filters."""
    resp = _run(
        _calendar.events().list(
            calendarId=_resolve_calendar(calendar),
            timeMin=_as_bound(time_min),
            timeMax=_as_bound(time_max),
            q=query or None,
            singleEvents=True,
            orderBy="startTime",
        )
    )
    return [_summarize(e) for e in resp.get("items", [])]


def _as_bound(value: str) -> str:
    """Google wants RFC 3339 with an offset; accept a bare date or a naive
    datetime too (read as UTC) instead of failing the call."""
    if len(value) == 10:
        return f"{value}T00:00:00Z"
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value if dt.tzinfo else f"{value}Z"


@mcp.tool
def update_event(
    event_id: str,
    calendar: str,
    title: str | None = None,
    start: str | None = None,
    end: str | None = None,
    all_day: bool | None = None,
    recurrence: list[str] | None = None,
    description: str | None = None,
    location: str | None = None,
    timezone: str = "UTC",
) -> dict:
    """Change fields on an event on `calendar` (name or id). Only the fields you
    pass are touched. all_day=true turns a timed event into a whole-day one on
    its current date (or `start`); all_day=false needs `start` and `end`
    datetimes. `recurrence` replaces the repeat rules (e.g.
    ["RRULE:FREQ=YEARLY"]); pass [] to stop repeating. For a repeating event,
    pass its series id to change every occurrence."""
    cal_id = _resolve_calendar(calendar)
    # Read-modify-write (not patch): switching between timed and all-day
    # must drop the old dateTime/date key, which a merge-patch keeps.
    ev = _run(_calendar.events().get(calendarId=cal_id, eventId=event_id))
    if title is not None:
        ev["summary"] = title
    if description is not None:
        ev["description"] = description
    if location is not None:
        ev["location"] = location
    was_all_day = "date" in ev.get("start", {})
    whole_day = was_all_day if all_day is None else all_day
    if start is not None or end is not None or all_day is not None:
        cur_start = ev["start"].get("dateTime") or ev["start"].get("date")
        cur_end = ev["end"].get("dateTime") or ev["end"].get("date")
        if whole_day:
            new_start = start or cur_start
            if end is not None:
                new_end = end
            elif start is None and was_all_day:
                new_end = _shift(cur_end, -1)  # untouched span (stored exclusive)
            else:
                new_end = None  # one day
            ev["start"], ev["end"] = _times(new_start, new_end, True, timezone)
        else:
            if was_all_day and (start is None or end is None):
                raise ValueError("Turning a whole-day event into a timed one needs start and end datetimes.")
            tz = ev["start"].get("timeZone", timezone) if start is None and end is None else timezone
            ev["start"], ev["end"] = _times(start or cur_start, end or cur_end, False, tz)
    if recurrence is not None:
        rules = _check_recurrence(recurrence)
        if rules:
            ev["recurrence"] = rules
        else:
            ev.pop("recurrence", None)
    ev = _run(_calendar.events().update(calendarId=cal_id, eventId=event_id, body=ev))
    return _summarize(ev)


@mcp.tool
def delete_event(event_id: str, calendar: str) -> dict:
    """Cancel and remove an event from `calendar` (name or id). For a repeating
    event, its series id removes every occurrence; an occurrence id removes
    just that one."""
    _run(_calendar.events().delete(calendarId=_resolve_calendar(calendar), eventId=event_id))
    return {"deleted": event_id}


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
