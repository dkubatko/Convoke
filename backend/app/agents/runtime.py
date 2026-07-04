"""Agent run execution: context assembly → pydantic-ai run → reply."""

import asyncio
import html
import logging
from contextlib import AsyncExitStack
from datetime import datetime, timezone

from aiogram import Bot as AiogramBot
from aiogram.exceptions import TelegramRetryAfter
from pydantic_ai import Agent
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.context import assemble_context
from app.agents.deps import AgentDeps
from app.agents.mcp import toolsets_for_chat
from app.agents.models import ProviderNotConfigured, build_model, get_provider
from app.agents.tools import AGENT_TOOLS
from app.intent.episodes import finish_run_episode
from app.memory.embeddings import Embedder
from app.models import AgentRun, Bot, Chat
from app.telegram.limiter import SendLimiter
from app.telegram.sender import send_and_persist

log = logging.getLogger("convoke.agent")

TELEGRAM_MESSAGE_LIMIT = 4096
MAX_REPLY_PARTS = 3

INSTRUCTIONS_TEMPLATE = """\
You are {bot_name} (@{bot_username}), an assistant participating in the Telegram \
group chat "{chat_title}". {invocation_line}

- Answer directly and conversationally; this is a group chat, keep it brief.
- Write plain text only: no markdown, no HTML tags, no code fences.
- The chat's full history is your memory: use search_chat_history for anything \
older than the recent messages shown, recall for stored notes, and remember to \
persist durable facts (decisions, preferences, recurring context) worth keeping.
- When a tool needs structured arguments, fill them from your own knowledge when \
you are confident: a city becomes its coordinates, a place its timezone, a date \
phrase a concrete date. Don't ask the user for technical values you can derive.
- If you genuinely can't help, say so briefly rather than guessing at facts you \
don't know.
"""


def build_agent(model, instructions: str, extra_toolsets=None) -> Agent:
    return Agent(
        model,
        instructions=instructions,
        deps_type=AgentDeps,
        tools=list(AGENT_TOOLS),
        toolsets=list(extra_toolsets or []),
        retries=1,
    )


def split_reply(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT - 96) -> list[str]:
    parts: list[str] = []
    remaining = text.strip()
    while remaining and len(parts) < MAX_REPLY_PARTS:
        if len(remaining) <= limit:
            parts.append(remaining)
            break
        cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    return parts


async def execute_run(
    sessionmaker: async_sessionmaker[AsyncSession],
    embedder: Embedder,
    limiter: SendLimiter,
    bot: AiogramBot,
    run_id: int,
    extra_toolsets=None,
) -> None:
    async with sessionmaker() as session:
        run = await session.get(AgentRun, run_id)
        if run is None or run.status != "pending":
            return
        chat = await session.get(Chat, run.chat_id)
        bot_row = await session.get(Bot, chat.bot_id)
        run.status = "running"
        run.started_at = datetime.now(timezone.utc)
        await session.commit()

        try:
            provider = await get_provider(session, "agent")
        except ProviderNotConfigured:
            await _fail(
                session,
                run,
                "No agent model configured",
                bot,
                limiter,
                bot_row,
                chat,
                notify="I'm not fully set up yet — the operator needs to configure "
                "an agent model in Convoke.",
            )
            return

    is_workflow = run.trigger == "workflow"
    thread_id = run.thread_id
    trigger_message_id = run.trigger_tg_message_id
    request_text = run.request_text

    # Everything past the 'running' commit is guarded: a failure in context
    # assembly, MCP setup, the model call, OR the reply send must mark the run
    # error and (best-effort) notify — otherwise it shows 'running' forever.
    try:
        async with sessionmaker() as session:
            # chat/bot_row from the first block are detached but readable;
            # re-attach chat for the queries these helpers run.
            chat = await session.get(Chat, chat.id)
            prompt_context = await assemble_context(
                session, embedder, chat, request_text, thread_id=thread_id
            )
            mcp_toolsets = await toolsets_for_chat(session, chat.id)

        instructions = INSTRUCTIONS_TEMPLATE.format(
            bot_name=bot_row.name,
            bot_username=bot_row.username,
            chat_title=chat.title or "this chat",
            invocation_line=(
                "You were triggered by an automated workflow; carry out the task given "
                "at the end of the prompt, using tools as needed, then post a short "
                "summary of what you did."
                if is_workflow
                else "You were invoked by a member's message (shown last in the recent messages)."
            ),
        )
        user_prompt = prompt_context
        if is_workflow:
            user_prompt = f"{prompt_context}\n\n## Task\n{request_text}"
        agent = build_agent(
            build_model(provider), instructions, mcp_toolsets + list(extra_toolsets or [])
        )
        deps = AgentDeps(
            sessionmaker=sessionmaker, embedder=embedder, chat_id=chat.id, run_id=run_id
        )

        try:
            await bot.send_chat_action(chat.tg_chat_id, "typing", message_thread_id=thread_id)
        except Exception:  # noqa: BLE001 — cosmetic
            pass

        # MCP connections open for exactly the duration of the run.
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(agent)
            result = await agent.run(user_prompt, deps=deps)
        reply_text = (result.output or "").strip() or "(no reply)"

        async with sessionmaker() as session:
            run = await session.get(AgentRun, run_id)
            chat = await session.get(Chat, run.chat_id)
            for part in split_reply(reply_text):
                await limiter.acquire(chat.bot_id, chat.tg_chat_id)
                await _send(
                    session, bot, chat, part,
                    reply_to=trigger_message_id, thread_id=thread_id,
                )
            run.status = "done"
            run.response_text = reply_text
            run.finished_at = datetime.now(timezone.utc)
            # Feedback loop: the episode that fired this run becomes
            # `satisfied`, carrying what was done.
            await finish_run_episode(session, run_id, reply_text, run.finished_at)
            await session.commit()
    except Exception as e:  # noqa: BLE001 — any failure ends the run cleanly
        log.exception("agent run %s failed", run_id)
        async with sessionmaker() as session:
            run = await session.get(AgentRun, run_id)
            if run is not None and run.status == "running":
                chat = await session.get(Chat, run.chat_id)
                await _fail(
                    session, run, f"{type(e).__name__}: {e}", bot, limiter, bot_row, chat,
                    # Silence reads as a crash — always leave a trace in the chat.
                    notify="Something went wrong and I couldn't finish that. "
                    "The details are in Convoke's run log.",
                )


async def _send(session, bot, chat, text: str, reply_to: int | None = None, thread_id: int | None = None):
    # Agent output is untrusted for HTML parse mode — escape it (renders as
    # the original characters, but can never break parsing).
    try:
        return await send_and_persist(
            session, bot, chat, html.escape(text), reply_to_message_id=reply_to, thread_id=thread_id
        )
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after)
        return await send_and_persist(
            session, bot, chat, html.escape(text), reply_to_message_id=reply_to, thread_id=thread_id
        )


async def _fail(session, run, error: str, bot, limiter, bot_row, chat, notify: str | None = None):
    run.status = "error"
    run.error = error
    run.finished_at = datetime.now(timezone.utc)
    # A failed run didn't handle the topic — the episode reverts to tracking.
    await finish_run_episode(session, run.id, None, run.finished_at)
    if notify:
        try:
            await limiter.acquire(chat.bot_id, chat.tg_chat_id)
            await _send(
                session, bot, chat, notify,
                reply_to=run.trigger_tg_message_id, thread_id=run.thread_id,
            )
        except Exception:  # noqa: BLE001 — recording the failure matters more
            log.warning("could not send failure notice to chat %s", chat.tg_chat_id)
    await session.commit()
