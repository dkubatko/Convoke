# Convoke

Self-hosted orchestration for Telegram chat assistants. Connect a bot you own to any
group chat; the chat's history becomes the assistant's persistent semantic memory, and
**workflows** let it act on its own — on a schedule, or when it detects an intent
converging in the conversation ("they've agreed on dinner Tuesday 7pm → create the
calendar event via MCP").

## Features

- **Memory cortex** — every message is stored, chunked into conversation segments and
  embedded (local `multilingual-e5-small`, pgvector). The agent searches the full chat
  history semantically, keeps distilled notes (`remember`/`recall`), and always sees
  recent messages verbatim.
- **History import** — bots can't read messages from before they joined (Telegram
  platform limitation), so Convoke ingests a Telegram Desktop JSON export, validated
  against live history before it's trusted.
- **Agent on mention/reply** — @mention the bot or reply to it; it answers with full
  memory and the chat's MCP tools. Powered by [Pydantic AI](https://ai.pydantic.dev).
- **Intent workflows** — plain-text triggers evaluated continuously and cheaply:
  free gates → embedding prefilter (anchored in generated example utterances) →
  cheap-LLM classifier → slot-filling convergence state machine → optional in-chat
  ✅/❌ confirmation before acting.
- **Scheduled workflows** — cron-triggered agent actions per chat.
- **MCP connections** — register streamable-HTTP or stdio MCP servers, enable them
  per chat; the agent gets their tools per run. OAuth-protected servers are supported
  with a one-time browser sign-in (discovery, dynamic client registration, PKCE,
  automatic token refresh).
- **BYO models** — any OpenAI-compatible endpoints (Ollama, LM Studio, OpenRouter,
  OpenAI…), split by role: cheap `intent` classifier, strong `agent` model.

## Quick start

```bash
cp .env.example .env   # then fill in the three secrets (see comments inside)
docker compose up -d
open http://localhost:8080
```

Sign in with `CONVOKE_OPERATOR_PASSWORD`, then:

1. **Models** — point the `agent` (and optionally `intent`) role at an
   OpenAI-compatible endpoint. From Docker, a service on your host is
   `http://host.docker.internal:11434/v1` (Ollama example).
2. **Bots** — create a bot with [@BotFather](https://t.me/BotFather), paste its token.
   **Critical:** in BotFather run `/setprivacy` → **Disable**, or the bot only sees
   mentions and its memory stays empty. If the bot is already in a group, remove and
   re-add it after changing privacy mode (Telegram requirement). Convoke shows a
   warning until this is right.
3. Add the bot to a group. It posts an **"Authorize Convoke"** button — a chat admin
   taps it (verified at click time). From that moment messages are ingested.
4. *(Optional)* **Import history**: ask an admin for a Telegram Desktop export of the
   chat (⋯ → Export chat history → Format: **JSON**) and upload the `result.json` in
   the chat panel. Uploads are validated (chat id, title, overlap with live history)
   and each import is surgically deletable.
5. Create **workflows** and assign them to chats.

## Architecture (short version)

Four containers: `frontend` (nginx + React), `backend` (FastAPI, singleton),
`worker` (polling + all loops, singleton), `db` (Postgres 17 + pgvector). Postgres is
the only datastore — including the work queues.

The load-bearing pattern is a **transactional inbox**: each bot's `getUpdates`
long-poll loop does exactly one thing — persist raw updates, commit, then advance the
Telegram offset. Everything downstream (message ingestion, embedding, intent
evaluation, agent runs) is a DB-driven consumer, so a crash never loses data;
Telegram's offset-ack is the only redelivery mechanism there is.

Things Telegram will not tell you (and how Convoke copes):

| Platform reality | Convoke's answer |
| --- | --- |
| Bots can't fetch history, ever | Export upload + validation scorecard |
| Privacy mode hides group messages | `getMe` check + hard warning in UI |
| Updates kept only 24h server-side | Downtime gaps recorded, shown in UI and to the agent |
| Bots never see other bots' messages (incl. own) | Outbound replies persisted at send time |
| Deletions are never delivered | Operator "forget" tooling per sender/range/chat |
| 1 msg/s per chat, 20/min per group | Central token-bucket limiter on all sends |

## Development

```bash
cd backend && uv sync && uv run pytest          # 52 tests
cd frontend && npm install && npm run dev       # Vite dev server, proxies /api to :8000
```

Migrations: `alembic revision` in `backend/`; the backend container runs
`alembic upgrade head` on start.

## Not in the MVP (by design)

Langfuse observability, Temporal workflows, MTProto-assisted history pull (QR login →
pull one chat → destroy session), webhook mode, hierarchical summaries, multi-user
accounts. The data model is keyed by bot/chat so these bolt on without rework.
