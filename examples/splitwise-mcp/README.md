# Group expense tracking with a custom Splitwise MCP

Goal: when a group talks about money — "I covered the $120 Airbnb deposit", "dinner was
$90, split four ways" — the agent logs it in that group's [Splitwise](https://splitwise.com)
group, and can answer "who owes whom?" without anyone opening the app. One bot account serves
**any number of groups** it belongs to, targeted **by name**.

This guide **starts from nothing** and ships a ~190-line MCP server ([`server.py`](server.py))
that talks to the Splitwise API directly. It exposes five tools — `list_groups`,
`list_members`, `record_expense`, `who_owes_whom`, `record_payment` — shaped for a chat agent
(it passes group and people *names*; the server resolves them and balances the split).

Time: ~25 minutes.

## Why a custom server (and not a community one)?

Logging a group's expenses is essentially one API call (`POST /create_expense`). The
community Splitwise MCP servers are single-user hobby projects that expose 20+ generic tools
and, worse, ask you to hand them a Splitwise API key — which is an **unscoped, full-account
token**. The 110 lines here keep that token on a server *you* run and give the agent exactly
the four tools it needs.

## Why a dedicated bot account — and why it's *not* in the split

Two facts about the Splitwise API drive the design:

1. **An API key acts as the account that created it, and can only touch groups that account
   belongs to.** So the bot gets its own Splitwise account, joins each group, and uses *its*
   key. Nobody's personal account is exposed.
2. **The API (unlike the app's UI) lets you create an expense whose participant list omits
   the caller.** So the bot records "Alice paid, Bob & Carol owe" **without inserting itself
   into the split** — no phantom member owing \$0 in every expense. The bot is a group member
   for permission only; it never appears in an expense.

(The Splitwise app itself hardcodes "you" into every bill, which is why this surprises people
— but it's a UI choice, not an API rule. The shipped `server.py` relies on the API behavior.
Worth one throwaway test expense to confirm on your account, since Splitwise doesn't publish
its full server-side validation.)

---

## 1. Create the bot's Splitwise account and add it to your group(s)

1. Sign up for a **new Splitwise account for the bot** (its own email).
2. Have a member **invite the bot to each group** it should manage (Group → *Add members* →
   the bot's email), or create groups from the bot account and invite everyone. Either way the
   bot must end up a **member** of every group. Accept the invites from the bot account.

## 2. Get the bot's API key

Signed in **as the bot**:

1. Go to [secure.splitwise.com/apps](https://secure.splitwise.com/apps) → **Register your
   application**. Any name/description; put a placeholder homepage/callback URL (unused here).
2. On the app's detail page, copy the **API key**. It's a personal access token that
   authenticates as the bot account — no OAuth flow needed. Treat it as a secret.

## 3. Run the MCP server

No group ID to hard-wire: the bot discovers the groups it belongs to via `list_groups`, and
every tool takes the target group as an argument, so there's nothing else to configure.

```bash
cd examples/splitwise-mcp
pip install -r requirements.txt
export SPLITWISE_API_KEY=your_bot_api_key
python server.py
```

It serves one streamable-HTTP MCP endpoint (it prints the URL — by default
`http://0.0.0.0:8000/mcp/`). The API key stays on this server; Convoke never sees it.

As a `docker-compose.yml` sidecar:

```yaml
  splitwise-mcp:
    build: ./examples/splitwise-mcp
    restart: unless-stopped
    environment:
      SPLITWISE_API_KEY: ${SPLITWISE_API_KEY}
```

## 4. Register it in Convoke

**Tools** page → *Register an MCP server*:

| Field | Value |
| --- | --- |
| Name | `Splitwise` |
| Transport | Streamable HTTP |
| URL | `http://host.docker.internal:8000/mcp/` (host run) or `http://splitwise-mcp:8000/mcp/` (sidecar) |
| Authentication | **None** — the API key lives on the server, not in Convoke |

Press **Test connection** (required before *Register server* enables), then **Register
server**. Use `host.docker.internal`, not `localhost` — the worker runs in a container.

Then enable it for each group chat that should get it: **Chats → the chat → Tools tab → tick
`Splitwise`**.

## 5. The workflow (per group)

**Workflows → New workflow**:

- **Name**: `Expense logger`
- **Kind**: Intent
- **Trigger**: `Someone mentions paying for a shared cost, or the group agrees to split a bill`
- **Information to wait for**:

  ```
  amount: the total cost, with currency if stated
  description: what it was for (dinner, Airbnb, groceries…)
  payer: who paid (default to the person who said they covered it)
  split_among: who shares the cost (default to everyone in the group)
  ```

- **Ask in the chat before acting**: leave **on** — money entries are worth a one-tap ✅.
- **Action**: `Use record_expense to log it in this chat's Splitwise group (the one set up for
  this chat): the payer paid the full amount, split equally among the named people (call
  list_members if you need to match names). Then post a one-line confirmation of who paid and
  who owes what.`
  The agent recalls which Splitwise group this chat uses from memory (see below), so the action
  is the same for every group. If you'd rather be explicit, name the group in the action.
- Tick this group's chat → **Create workflow**; wait for **calibrated**. Repeat per group — the
  same action works everywhere.

Tell the bot once, in each group chat, which Splitwise group it maps to:
*"@bot use the `Ski Trip Crew` Splitwise group for this chat."* Convoke remembers it per chat
(`list_groups` shows the exact names), so you set it once and the agent recalls it later.

## 6. Watch it work

```
alice: I put the whole Airbnb on my card, $600
bob:   nice, split between the four of us?
alice: yep, evenly
```

Once `amount`, `payer`, and `split_among` are confident, the bot posts *"Expense logger is
ready to act"* with ✅/❌. On ✅ the agent calls `record_expense("Airbnb", "600.00", "Alice",
["Alice","Bob","Carol","Dave"], "Ski Trip Crew")` — the last argument being the group it
remembers for this chat — and confirms: *"Alice paid \$600 for Airbnb — \$150 each for Bob,
Carol, and Dave."* Ask *"@bot who owes whom?"* any time and it calls `who_owes_whom`.

The server handles the fiddly bits so the agent doesn't have to: it splits to the penny
(remainder cents land on the first shares), keeps the bot out of the `users[]` list, and
checks the response's `errors` field because **Splitwise returns HTTP 200 even when it rejects
an expense**.

## Multiple groups

One `Splitwise` server, enabled in several group chats. The bot joins each Splitwise group
(step 1); each chat is told once which group it maps to (step 5), and Convoke **remembers that
per chat** — so the same workflow drives every chat onto its own Splitwise group, with
`list_groups` resolving the name. No per-group env vars, servers, or redeploys, and no
server-side default: the group lives in the chat's memory, not the shim. Unlike the calendar
case there's no discovery gotcha — `get_groups` returns every group the bot belongs to
immediately.

## Alternative backends

The agent-facing shape here (a `record_expense` tool + a `who_owes_whom` tool) is the useful
part; Splitwise is just the backend. If you'd rather not use Splitwise at all, the same four
tools could write rows to a **Google Sheet** via the *same service account* as the calendar
example — one credential for both, nothing third-party. You'd trade Splitwise's built-in
settle-up math and mobile app for full control. For most groups Splitwise's balance
computation and everyone-already-has-the-app convenience win, which is why it's the default
here.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Splitwise tool errors with a connection failure | URL unreachable *from the worker container*. Use `host.docker.internal` (host) or the compose service name (sidecar), never `localhost`. |
| `Splitwise rejected the expense: …` | The shares didn't balance, or a name resolved to someone not in the group. The server raises the API's own `errors` — read them; usually a name typo or a member who hasn't joined. |
| `No group member matches 'X'` | The agent used a name Splitwise doesn't have. Call `list_members` to see the exact names; nicknames won't match. |
| `No group named 'X'` | The bot isn't a member of that Splitwise group, or the name differs. `list_groups` shows exactly what it can post to. |
| 401 from Splitwise | `SPLITWISE_API_KEY` isn't set in the server's environment, or was regenerated (which invalidates the old key). |
| 403 / "you do not have permission" | The bot account isn't a member of the target group. Invite it (step 1). |
| Tools don't appear in a run | Server registered but not **enabled** (Tools page), or not ticked for that chat (chat → Tools tab). |
