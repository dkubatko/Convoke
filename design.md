# Convoke - In-chat Assistant Orchestration Platform

## Brief

Convoke allows to orchestrate chat agent behaviors based on intent, schedule and chat memory.

In Convoke UI, user can connect a Telegram bot they own and invite it to any group chat.
Convoke detects the bot being added as a participant, and allows to sends an authentication request 
to the group chat admin. When granted, entire chat context is loaded into a semantic memory cortex 
and is persistent across invocations within the chat. After loading, the bot will be able to interact
with the chat via direct requests (@bot), replies to the bot, or **workflows**.

## Workflows
Workflows allow to define either scheduled, or intent-based triggers for the bot in the UI.

Intent-based triggers are prompted in plain text, and are evaluated continuously within the chat.

For example, an intent workflow can be defined as:
- Trigger: When there is an intent to schedule an event, with convergence on the specific date & time
- Action: Create the event via the Calendar MCP

## Features
- **Convoke UI**: connecting bots, MCPs, managing chats, ingesting message history, 
creating workflows, assigning workflows, etc. with fine-grained control per-bot and per-chat
- **MCP connections**: allows to connect MCPs via Convoke UI to allow bot/agent to use the tools
- **Proactive invocation**: Intent-based workflows trigger automatically by continuously monitoring chat
- **Memory context**: Full access to chat context via tool calls + additive hydration on new messages
- **Shared agentic memory**: all invocations of the agent within the chat share memory. 
- **BYO models**: support for locally and externally hosted models

## Requirements
**Free/open-source tooling only**
Suggestions/preferences below (not exhaustive):
- **Frontend/UI**: React, Typescript, Vite
- **Backend/service**: Python, FastAPI, SQLAlchemy, Alembic
- **Harness**: Pydantic AI
- **Memory**: ? -> Embeddings, Semantic Context, MCP (Python SDK?)
- **Deployment**: Docker, local friendly
- **Models**: Lightweight model for intent 
NOT PART OF MVP:
- **Observability**: Langfuse
- **Workflows**: Temporal
- **Semantic persistent agent memory**: xmemory