# Getting started: Convoke up and a bot listening

This is the one guide that starts from an empty install. It takes you from
`docker compose up` to a Telegram group whose every message is stored and searchable —
the baseline every other example assumes. Then you add tools and workflows on top.

Time: ~15 minutes. Everything happens in the Convoke UI (`http://localhost:8080`) and
Telegram; no code.

---

## 0. Prerequisites

- The Convoke stack is running: `docker compose up -d` (see the [main README](../README.md)
  for install), UI reachable, sidebar shows **all systems live**.
- A Telegram account.
- For the models — any OpenAI-compatible endpoints work; this example uses one cloud + one
  local:
  - An [OpenAI API key](https://platform.openai.com/api-keys) for the agent.
  - [Ollama](https://ollama.com) on the host for the intent listener:
    `ollama pull gemma4` (the default `e4b` tag; `gemma4:e2b` on smaller machines).

## 1. Point the model roles at endpoints

**Models** page:

| Role | Base URL | Model | API key |
| --- | --- | --- | --- |
| agent — the voice | `https://api.openai.com/v1` | `gpt-5.4-mini` | your OpenAI key |
| intent — the listener | `http://host.docker.internal:11434/v1` | `gemma4` | leave blank |

Save both. The split matters: the **intent** model is called continuously in small windows
(keep it cheap and local), while the **agent** model runs only when something actually
happens (a mention, or a workflow firing).

> `host.docker.internal` is how a container reaches services on your host — that's where
> Ollama listens (`:11434`).

## 2. Create the bot in Telegram

1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → pick a name and username.
2. **Critical:** run `/setprivacy`, select your bot, choose **Disable**. With privacy mode on
   (the default), a group bot only hears messages that mention it — Convoke's memory and
   intent detection would stay silently empty.
3. Copy the token BotFather prints.

## 3. Connect it to Convoke

**Bots** page → paste the token → **Connect bot**.

The bot appears hearing **all messages**. If it shows **mentions only**, fix step 2 and press
**Re-check** — and if the bot was already in a group, remove and re-add it there (Telegram
only applies the privacy change on re-join).

## 4. Add it to a group and authorize

1. In Telegram, add the bot to your group.
2. The bot posts *"Convoke was added to this chat"* with an **Authorize Convoke** button.
3. A **group admin** taps it. Convoke verifies admin status with Telegram at the moment of the
   tap; non-admins get a polite refusal.
4. The **Chats** page now shows the group as **authorized** with a live lamp. From this moment
   every message is stored and becomes searchable memory.

Optional — backfill the past: bots cannot read history from before they joined (Telegram
platform rule). Open the chat in Convoke → **Import history** tab and follow the instructions
there (Telegram Desktop → Export chat history → JSON).

## 5. Next: give it hands

A connected bot already listens, remembers, and answers on @mention or reply. To let it
*act*, add tools and workflows — that's what the rest of [`examples/`](README.md) covers:

- [`weather-mcp.md`](weather-mcp.md) / [`omdb-mcp.md`](omdb-mcp.md) — quick, no-code tools to
  get a feel for registering an MCP server and enabling it per chat.
- [`gcal-mcp/`](gcal-mcp/) — a shared **group calendar** the agent CRUDs when the group
  settles on a plan (an Intent workflow with a ✅ confirmation).
- [`splitwise-mcp/`](splitwise-mcp/) — **expense tracking**: the agent logs shared costs and
  answers "who owes whom?"

Each tool guide walks its own workflow. When you create an Intent workflow, the card shows
**generating example phrases** for a few seconds (Convoke asks the agent model for ~25
realistic utterances, embeds them, and calibrates the detector), then **calibrated** — after
which a normal conversation converging on the trigger fires it, gathering values across
messages until it has enough to act.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| Chat memory stays empty | Privacy mode still on, or the bot was not re-added after disabling it. Bots page shows this as a red **mentions only** pill. |
| Bot answers on mention but workflows never fire | No `intent` model configured, or the chat isn't ticked in the workflow. Check **Overview → activity** for errors. |
| Fires but "No agent model configured" | The agent role on the Models page is unset or the endpoint is down. |
| Bot went offline >24h | Telegram discards undelivered updates after 24h. The chat's Memory tab shows the gap; re-import an export to fill it. |
