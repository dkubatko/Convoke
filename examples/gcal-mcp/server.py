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
                                      each takes a required `calendar` (name or id)

The gotcha this design works around: Google Calendar's CalendarList API is
UNRELIABLE for service accounts — `calendarList.insert` returns a full success
and the entry silently never lands (measured live: insert → list in the same
process returned zero entries, even for a calendar the account OWNS). So names
can never depend on Google's list. Instead, `add_calendar`/`create_calendar`
record the name→id mapping in a small JSON file on a volume, and name
resolution consults that registry first; the live Google list is merged in as
a bonus wherever it happens to work.

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
from pathlib import Path

from fastmcp import FastMCP
from google.oauth2 import service_account
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar"]

_creds = service_account.Credentials.from_service_account_file(
    os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"], scopes=SCOPES
)
_calendar = build("calendar", "v3", credentials=_creds, cache_discovery=False)

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
        resp = _calendar.calendarList().list(pageToken=page).execute()
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
    return {
        "id": ev["id"],
        "title": ev.get("summary", ""),
        "start": start.get("dateTime") or start.get("date"),
        "end": end.get("dateTime") or end.get("date"),
        "location": ev.get("location", ""),
        "link": ev.get("htmlLink", ""),
    }


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
    c = _calendar.calendars().get(calendarId=calendar_id).execute()
    name = c.get("summary", calendar_id)
    try:
        _calendar.calendarList().insert(body={"id": calendar_id}).execute()
    except Exception:  # noqa: BLE001 — registry below is the real registration
        pass
    _save_alias(name, calendar_id)
    return {"id": calendar_id, "name": name, "access": "registered"}


@mcp.tool
def create_calendar(name: str, member_emails: list[str] = []) -> dict:
    """Create a new calendar owned by the bot and share it (view access) with the
    given member emails. It's immediately usable by name — no add_calendar needed."""
    cal = _calendar.calendars().insert(body={"summary": name}).execute()
    for email in member_emails:
        _calendar.acl().insert(
            calendarId=cal["id"],
            body={"role": "reader", "scope": {"type": "user", "value": email}},
        ).execute()
    _save_alias(cal.get("summary", name), cal["id"])
    return {"id": cal["id"], "name": cal.get("summary", name)}


# --- events -------------------------------------------------------------------


@mcp.tool
def create_event(
    title: str,
    start: str,
    end: str,
    calendar: str,
    description: str = "",
    location: str = "",
    timezone: str = "UTC",
) -> dict:
    """Create an event on `calendar` (its name or id — required).

    `start` and `end` are ISO-8601 datetimes, e.g. "2026-07-14T19:00:00". Returns
    the created event (id + link) so it can be referenced for later edits.
    """
    body = {
        "summary": title,
        "description": description,
        "location": location,
        "start": {"dateTime": start, "timeZone": timezone},
        "end": {"dateTime": end, "timeZone": timezone},
    }
    ev = _calendar.events().insert(
        calendarId=_resolve_calendar(calendar), body=body
    ).execute()
    return _summarize(ev)


@mcp.tool
def find_events(
    time_min: str, time_max: str, calendar: str, query: str = ""
) -> list[dict]:
    """List events on `calendar` (name or id) between two ISO-8601 datetimes. Use
    this to locate an event's id before updating or deleting it. `query`
    full-text-filters."""
    resp = (
        _calendar.events()
        .list(
            calendarId=_resolve_calendar(calendar),
            timeMin=time_min,
            timeMax=time_max,
            q=query or None,
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )
    return [_summarize(e) for e in resp.get("items", [])]


@mcp.tool
def update_event(
    event_id: str,
    calendar: str,
    title: str | None = None,
    start: str | None = None,
    end: str | None = None,
    description: str | None = None,
    location: str | None = None,
    timezone: str = "UTC",
) -> dict:
    """Change fields on an event on `calendar` (name or id). Only the fields you
    pass are touched."""
    patch: dict = {}
    if title is not None:
        patch["summary"] = title
    if description is not None:
        patch["description"] = description
    if location is not None:
        patch["location"] = location
    if start is not None:
        patch["start"] = {"dateTime": start, "timeZone": timezone}
    if end is not None:
        patch["end"] = {"dateTime": end, "timeZone": timezone}
    ev = (
        _calendar.events()
        .patch(calendarId=_resolve_calendar(calendar), eventId=event_id, body=patch)
        .execute()
    )
    return _summarize(ev)


@mcp.tool
def delete_event(event_id: str, calendar: str) -> dict:
    """Cancel and remove an event from `calendar` (name or id)."""
    _calendar.events().delete(
        calendarId=_resolve_calendar(calendar), eventId=event_id
    ).execute()
    return {"deleted": event_id}


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
