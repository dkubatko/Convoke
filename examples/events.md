# End-to-end: a bot that schedules events from group conversation

This walkthrough takes you from an empty Convoke install to a Telegram group where the
assistant notices the group converging on an event ("dinner Tuesday at 7?") and creates
it in a calendar — after asking the chat for a ✅.

Time: ~20 minutes. Everything happens in the Convoke UI (`http://localhost:8080`) and
Telegram; no code.

---

## 0. Prerequisites

- The Convoke stack is running: `docker compose up -d`, UI reachable, sidebar shows
  **all systems live**.
- A Telegram account.
- For the models (this example uses one cloud + one local, but any OpenAI-compatible
  endpoints work):
  - An [OpenAI API key](https://platform.openai.com/api-keys) for the agent.
  - [Ollama](https://ollama.com) running on the host for the intent listener:
    `ollama pull gemma4` (the default `e4b` tag; use `gemma4:e2b` on smaller machines).

## 1. Point the model roles at endpoints

**Models** page:

| Role | Base URL | Model | API key |
| --- | --- | --- | --- |
| agent — the voice | `https://api.openai.com/v1` | `gpt-5.4-mini` | your OpenAI key |
| intent — the listener | `http://host.docker.internal:11434/v1` | `gemma4` | leave blank |

Save both. The split matters: the **intent** model is called continuously in small
windows (keep it cheap and local), while the **agent** model runs only when something
actually happens (a mention, or a workflow firing).

> `host.docker.internal` is how a container reaches services on your host — that's
> where Ollama listens (`:11434`).

## 2. Create the bot in Telegram

1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → pick a name and username.
2. **Critical:** run `/setprivacy`, select your bot, choose **Disable**.
   With privacy mode on (the default), a group bot only hears messages that mention it —
   Convoke's memory and intent detection would stay silently empty.
3. Copy the token BotFather prints.

## 3. Connect it to Convoke

**Bots** page → paste the token → **Connect bot**.

The bot appears with hearing **all messages**. If it shows **mentions only**, fix
step 2 and press **Re-check** — and if the bot was already in a group, remove and
re-add it there (Telegram only applies the privacy change on re-join).

## 4. Add it to a group and authorize

1. In Telegram, add the bot to your group.
2. The bot posts *"Convoke was added to this chat"* with an **Authorize Convoke** button.
3. A **group admin** taps it. Convoke verifies admin status with Telegram at the moment
   of the tap; non-admins get a polite refusal.
4. The **Chats** page now shows the group as **authorized** with a live lamp. From this
   moment every message is stored and becomes searchable memory.

Optional — backfill the past: bots cannot read history from before they joined
(Telegram platform rule). Open the chat in Convoke → **Import history** tab and follow
the instructions there (Telegram Desktop → Export chat history → JSON).

## 5. Give it calendar hands (MCP)

Any calendar MCP server works. Two options:

**A. Community server as a local service** — e.g.
[google-calendar-streamable-mcp-server](https://github.com/iceener/google-calendar-streamable-mcp-server)
(TypeScript, streamable HTTP). Run it on the host or add it to `docker-compose.yml` as
another service, complete its Google OAuth setup, and note its URL
(e.g. `http://host.docker.internal:3000/mcp`).

**B. OAuth servers, including Google's official remote Calendar MCP** — Convoke
supports the MCP OAuth flow natively: register the server with **Authentication:
OAuth sign-in**, and a browser tab opens for a one-time sign-in; Convoke stores and
refreshes the tokens from then on. For Google specifically
([guide](https://developers.google.com/workspace/calendar/api/guides/configure-mcp-server)):
Google doesn't support automatic client registration, so first create an OAuth client
in Google Cloud Console (Web application, redirect URI
`http://localhost:8080/api/mcp-oauth/callback`), then paste its client id/secret into
the register form along with the calendar scopes.

Then in Convoke:

1. **Tools** page → Register an MCP server → name `Calendar`, transport
   `Streamable HTTP`, the URL from above, bearer token if the server wants one.
2. **Chats** → your group → **Tools** tab → tick `Calendar`.

Tools are per-chat on purpose: your family chat's agent doesn't need your work chat's
ticket system.

## 6. Create the workflow

**Workflows** page → **New workflow**:

- **Name**: `Event scheduler`
- **Kind**: Intent
- **Trigger**: `The group agrees to schedule an event or meetup, with a specific date
  and time settled`
- **Information to wait for**:

  ```
  date: the agreed date and time, as specific as the group settles on
  title: what the event is (dinner, ski trip, standup…)
  ```

- **Ask in the chat before acting**: leave on. A wrongly created invite annoys real
  people; the confirmation turns that failure mode into one tap of ❌.
- **Action**: `Create the event via the calendar tools, then post a one-line
  confirmation with the event time.`
- Tick your group chat → **Create workflow**.

The card shows **generating example phrases** for a few seconds: Convoke asks the agent
model for ~25 realistic utterances ("does Tue 7pm work?", near-misses too), embeds them,
and calibrates the detector threshold. It then shows **calibrated** with the threshold.

## 7. Watch it work

Have a normal conversation in the group:

> **alice**: should we do a team dinner sometime?
> **bob**: yes! next week?
> **alice**: tuesday works for me
> **bob**: same, let's say 7pm at the usual place

What happens behind the scenes, stage by stage:

1. **Gates (free)** — messages accumulate; evaluation waits for a lull (~60s of
   silence) or a burst of 10+ messages, so a rapid exchange is judged once, as a whole.
2. **Prefilter (local embeddings)** — the window is compared against the generated
   example phrases. Off-topic chatter stops here at zero model cost.
3. **Classifier (gemma4)** — the window plus any previously gathered values go to the
   intent model, which extracts updates: `title: team dinner`, `date: Tuesday 7pm`.
   Values accumulate **across messages and across windows** — nothing needs to be said
   in one message. "Actually, Wednesday" later? The value is overwritten. The topic
   dies for a day and a half? The gathered state expires.
4. **Convergence** — once both `date` and `title` hold confident values, the workflow
   arms exactly once and goes into cooldown.
5. **Confirmation** — the bot posts *"Event scheduler is ready to act"* with the
   gathered values and ✅ / ❌ buttons.
6. **Action (gpt-5.4-mini)** — on ✅, the agent runs with the chat's memory and the
   Calendar tools, creates the event, and posts its one-line confirmation.

Check **Workflows → Activity** for the fire log and **Overview** for the run.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| Chat memory stays empty | Privacy mode still on, or the bot was not re-added after disabling it. Bots page shows this as a red **mentions only** pill. |
| Workflow never fires | Chat not ticked in the workflow; detector still **generating**; or no `intent`/`agent` model configured (check Overview → activity for errors). |
| Fires but "No agent model configured" | The agent role on the Models page is unset or the endpoint is down. |
| Detector shows **fallback** | No agent model was configured when the workflow was saved — set one, then edit and re-save the workflow to regenerate examples. |
| Calendar tool errors in the run log | The MCP server URL isn't reachable *from the worker container*, or auth is missing. `host.docker.internal`, not `localhost`, for host services. |
| Bot went offline >24h | Telegram discards undelivered updates after 24h. The chat's Memory tab shows the gap; re-import an export to fill it. |
