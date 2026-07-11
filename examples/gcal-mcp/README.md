# Shared group calendars with a custom Google Calendar MCP

Goal: when a group settles on a plan ("ski trip Feb 14, leave at 8"), the agent creates,
edits, or cancels the event on **a calendar everyone in that group sees** — not on somebody's
private calendar. One service account serves **any number of groups**, each with its own
calendar, and the agent picks the right one **by name**.

This is an end-to-end guide that **starts from nothing**: no Google Cloud project, no
calendar. By the end you'll have a ~220-line MCP server ([`server.py`](server.py)) running
next to Convoke, exposing event tools (`create_event`, `find_events`, `update_event`,
`delete_event`) plus calendar-management tools (`list_calendars`, `add_calendar`,
`create_calendar`).

Time: ~30 minutes.

## Why a custom server, and why a service account?

Calendar MCP servers on GitHub are built for a *single person* managing *their own* calendar
through Claude Desktop — driving one as an unattended group bot inherits a browser OAuth flow
and a refresh token that **expires every 7 days**, so the bot goes silent a week after setup.
The server here sidesteps that by using the identity Google designed for programs: a **service
account**. It *is* the bot's identity — no OAuth consent screen, no browser sign-in, no token
expiry. You download a key file and share calendars with an email address.

(The one thing a Gmail identity would add is a friendlier "created by" name; a service account
shows as `…@…iam.gserviceaccount.com`. If that matters, see [Alternative](#alternative-a-bot-gmail-account).)

> One real limitation: a service account on consumer Gmail **can't email calendar invitations
> to attendees** (that needs Google Workspace). It doesn't need to here — members subscribe to
> the shared calendar, so events just appear. Don't add attendee emails to events.

## How one account serves many calendars

The service account is shared onto each group's calendar, and the event tools take a
`calendar` argument (a name or id). There's **one Google quirk** the server works around, and
it's worth understanding:

> Sharing a calendar with a service account grants it access **by id**, but does **not** add
> the calendar to the account's list — so the bot can't *discover* it by name until you
> **subscribe** it once. `add_calendar(id)` does that subscribe. Calendars the bot makes
> itself with `create_calendar` are owned by it and show up automatically.

So per group you do a **one-time** setup (share + `add_calendar`, or `create_calendar`), then
the agent targets that calendar by name forever — no env vars, no redeploys, no second server.

---

## 1. Create a Google Cloud project and enable the Calendar API

1. [Google Cloud Console](https://console.cloud.google.com/) → project picker → **New
   Project** → name it (e.g. `Group Calendar Bot`) → **Create**.
2. **APIs & Services → Library** → search **Google Calendar API** → **Enable**.

## 2. Create the service account and its key

1. **APIs & Services → Credentials → Create credentials → Service account**. Name it
   (e.g. `calendar-bot`) → **Create and continue** → skip the optional roles → **Done**.
2. Open it → **Keys** tab → **Add key → Create new key → JSON** → **Create**. A `*.json` file
   downloads — the bot's credential, keep it secret.
3. Copy the service account's **email** (`calendar-bot@your-project.iam.gserviceaccount.com`).
   You'll share calendars with it.

## 3. Run the MCP server

The server needs only the key file — every event tool takes the target calendar as an
argument, so there's nothing else to configure. On your host:

```bash
cd examples/gcal-mcp
pip install -r requirements.txt
export GOOGLE_SERVICE_ACCOUNT_FILE=/path/to/calendar-bot-key.json
python server.py
```

It serves one streamable-HTTP MCP endpoint (it prints the URL — by default
`http://0.0.0.0:8000/mcp/`). The Google credentials live only on this server; Convoke never
sees them. To keep it running, add it to `docker-compose.yml` as a sidecar (mount the key
read-only):

```yaml
  gcal-mcp:
    build: ./examples/gcal-mcp
    restart: unless-stopped
    environment:
      GOOGLE_SERVICE_ACCOUNT_FILE: /creds/sa.json
    volumes:
      - ./examples/gcal-mcp/sa.json:/creds/sa.json:ro
```

## 4. Register it in Convoke

**Tools** page → *Register an MCP server*:

| Field | Value |
| --- | --- |
| Name | `Calendar` |
| Transport | Streamable HTTP |
| URL | `http://host.docker.internal:8000/mcp/` (host run) or `http://gcal-mcp:8000/mcp/` (sidecar) |
| Authentication | **None** — credentials live on the server, not in Convoke |

Press **Test connection** (required before *Register server* enables), then **Register
server**. Use `host.docker.internal`, not `localhost` — the worker runs in a container. Then
enable it for each group chat that should get it: **Chats → the chat → Tools tab → tick
`Calendar`**.

## 5. Give the bot a calendar for each group

Two ways, both done **once per group** — and both can be driven by just talking to the bot in
that group (the tools run through the agent), or by calling the server directly.

**A. Reuse an existing shared calendar** (you already have one for the group):

1. In [Google Calendar](https://calendar.google.com/), open that calendar's **Settings and
   sharing → Share with specific people** → add the **service-account email** at **Make changes
   to events**. Copy its **Calendar ID** from **Integrate calendar** (`…@group.calendar.google.com`).
2. Tell the bot, in the group chat: *"@bot add the calendar
   `…@group.calendar.google.com` and use it as this group's calendar."* It calls `add_calendar`
   (subscribing the calendar so it's findable by name) and — because Convoke remembers the chat
   — notes that this group's events belong on that calendar. You set it **once**; the agent
   recalls it on later runs.

**B. Let the bot create one** (new group, zero id-wrangling): in the group chat,
*"@bot create a calendar called `Ski Trip Crew`, share it with alice@…, bob@…, and use it as
this group's calendar."* → the agent calls `create_calendar` (bot-owned, instantly
discoverable), shares it, and remembers it for this chat. Members add it to their Google
Calendar from the invite/link once.

Check what the bot can see anytime with *"@bot what calendars do you have?"* (`list_calendars`),
and change a group's calendar just by telling it to use a different one.

## 6. The workflow (per group)

**Workflows → New workflow**:

- **Name**: `Event scheduler`
- **Kind**: Intent
- **Trigger**: `The group agrees to schedule, move, or cancel an event, with a specific date
  and time settled`
- **Information to wait for**:

  ```
  action: create, update, or cancel
  date: the agreed date and time, as specific as the group settles on
  title: what the event is (dinner, ski trip, standup…)
  ```

- **Ask in the chat before acting**: leave **on** — a wrong or cancelled invite pings real
  people; the confirmation makes that one tap of ❌.
- **Action**: `Use the calendar tools on this group's calendar (the one set up for this chat)
  to create, update, or cancel the event — find it first with find_events when changing or
  cancelling — then post a one-line confirmation with the event time.`
  The agent recalls which calendar this chat uses from memory (step 5), so the action text is
  the same for every group. If you'd rather be explicit, name the calendar in the action.
- Tick this group's chat → **Create workflow**, wait for **calibrated**. Repeat per group — the
  same action works everywhere, since each chat remembers its own calendar.

## 7. Watch it work

```
alice: should we do a team dinner sometime?
bob:   tuesday works, say 7pm at the usual place
```

Once `title` and `date` are confident, the bot posts *"Event scheduler is ready to act"* with
✅/❌. On ✅ the agent calls `create_event` on the calendar it remembers for this chat, and it
appears for everyone in that group. The run and its tool calls show under the chat's **Agent runs** tab. Later —
*"push dinner to Wednesday"* — the same workflow fires with `action: update`, and the agent
`find_events` → `update_event`.

## Multiple groups

One `Calendar` server, enabled in several group chats. Each group is told once which calendar
it uses (step 5), and Convoke **remembers that per chat** — so the same workflow, with the same
action text, drives every group onto its own calendar. The agent uses `list_calendars` to
resolve the name and can self-correct if it's ambiguous. No per-group env vars, servers, or
redeploys — and no server-side default: the calendar lives in the chat's memory, not the shim.

## Alternative: a bot Gmail account

If you specifically want events to show a friendly **"Group Bot"** as the creator, use a
dedicated Gmail account with OAuth instead. The community server
[`@cocal/google-calendar-mcp`](https://github.com/nspady/google-calendar-mcp) does this — but
you set up an OAuth consent screen and client, sign in as the bot, and **publish the app to
Production** (Console → OAuth consent screen → Publish) or its refresh token expires every
7 days. More moving parts for a nicer name; the service-account path above is what most groups
should use.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Calendar tool errors with a connection failure | The URL isn't reachable *from the worker container*. Use `host.docker.internal` (host service) or the compose service name (sidecar), never `localhost`. |
| `No calendar named 'X'` | The bot isn't subscribed to it. Share the service-account email onto it and run `add_calendar` with its id (step 5A), or `create_calendar` it. `list_calendars` shows what's currently known. |
| `list_calendars` is empty despite sharing | Expected — sharing grants access by id but doesn't add the calendar to the bot's list. Run `add_calendar(id)` once. |
| `create_event` succeeds but nobody sees it | Members weren't shared in or didn't add the calendar. Re-check step 5 and have them accept/add it. |
| Server won't start: `KeyError: 'GOOGLE_SERVICE_ACCOUNT_FILE'` | That env var isn't set in the server's environment. |
| Invites to attendees don't send | Expected — a consumer service account can't email invitations. Put people on the calendar via sharing, not as event attendees. |
| Tools don't appear in a run | Server registered but not **enabled** (Tools page), or not ticked for that chat (chat → Tools tab). |
