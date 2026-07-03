from sqlalchemy import select

from app.agents.mcp import _safe_prefix, toolsets_for_chat
from app.models import Bot, Chat, ChatMcpServer, McpServer


async def _setup(db_sessionmaker):
    async with db_sessionmaker() as s:
        bot = Bot(tg_bot_id=1, username="b", name="b", token_encrypted="x",
                  can_read_all_group_messages=True)
        s.add(bot)
        await s.flush()
        chat = Chat(bot_id=bot.id, tg_chat_id=-100, type="supergroup", status="authorized")
        s.add(chat)
        await s.flush()
        servers = [
            McpServer(name="calendar", transport="http", url="http://cal:8000/mcp"),
            McpServer(name="files", transport="stdio", command="mcp-files", args=["--root", "/x"]),
            McpServer(name="disabled one", transport="http", url="http://z:1/mcp", enabled=False),
            McpServer(name="unassigned", transport="http", url="http://u:1/mcp"),
        ]
        s.add_all(servers)
        await s.flush()
        for srv in servers[:3]:
            s.add(ChatMcpServer(chat_id=chat.id, mcp_server_id=srv.id))
        await s.commit()
        return chat.id


async def test_toolsets_only_for_enabled_assigned_servers(db_sessionmaker):
    chat_id = await _setup(db_sessionmaker)
    async with db_sessionmaker() as s:
        toolsets = await toolsets_for_chat(s, chat_id)
    # calendar + files assigned and enabled; disabled + unassigned excluded
    assert len(toolsets) == 2


def test_safe_prefix():
    assert _safe_prefix("My Calendar!") == "my_calendar_"
    assert _safe_prefix("...") == "___"


def test_sanitize_tool_name():
    from app.agents.mcp import sanitize_tool_name

    # Smithery namespaces tools as 'server:tool' — OpenAI rejects ':' and '/'
    assert sanitize_tool_name("smithery-ai/national-weather-service:get_current_weather") == (
        "smithery-ai_national-weather-service_get_current_weather"
    )
    assert sanitize_tool_name("ok_name-123") == "ok_name-123"
    assert sanitize_tool_name("") == "tool"


async def test_agent_calls_tool_with_illegal_name(db_sessionmaker, monkeypatch):
    """Regression: a Smithery-style 'server:tool' name must be sanitized for
    the model AND still route back to the original tool on call."""
    from fastmcp import FastMCP
    from pydantic_ai.mcp import MCPToolset
    from pydantic_ai.models.test import TestModel

    import app.agents.runtime as runtime
    from app.agents.mcp import SanitizedToolset
    from app.agents.runtime import execute_run
    from app.memory.embeddings import FakeEmbedder
    from app.models import AgentRun, ModelProvider
    from app.telegram.limiter import SendLimiter
    from tests.test_agent import AgentFakeBot

    calls: list[str] = []
    demo = FastMCP("aggregator")

    @demo.tool(name="weather-service:get_current_weather")
    def get_current_weather(location: str) -> str:
        """Current weather for a location."""
        calls.append(location)
        return "sunny"

    seen_tool_names: list[str] = []
    original_test_model = TestModel

    class RecordingModel(original_test_model):
        async def request(self, messages, model_settings, model_request_parameters):
            seen_tool_names.extend(
                t.name for t in model_request_parameters.tool_defs.values()
            ) if hasattr(model_request_parameters.tool_defs, "values") else None
            return await super().request(messages, model_settings, model_request_parameters)

    monkeypatch.setattr(runtime, "build_model", lambda provider: RecordingModel())

    async with db_sessionmaker() as s:
        bot = Bot(tg_bot_id=999, username="cb", name="cb", token_encrypted="x",
                  can_read_all_group_messages=True)
        s.add(bot)
        await s.flush()
        chat = Chat(bot_id=bot.id, tg_chat_id=-100, type="supergroup", status="authorized")
        s.add(chat)
        s.add(ModelProvider(role="agent", base_url="http://unused", model_name="t"))
        await s.flush()
        run = AgentRun(chat_id=chat.id, trigger="mention", request_text="weather?")
        s.add(run)
        await s.commit()
        run_id = run.id

    toolset = SanitizedToolset(MCPToolset(demo)).prefixed("weather")
    await execute_run(
        db_sessionmaker, FakeEmbedder(), SendLimiter(), AgentFakeBot(), run_id,
        extra_toolsets=[toolset],
    )

    async with db_sessionmaker() as s:
        run = (await s.execute(select(AgentRun).where(AgentRun.id == run_id))).scalar_one()
        assert run.status == "done", run.error
    assert calls, "the MCP tool was never called through the sanitized route"
    import re
    mcp_names = [n for n in seen_tool_names if "weather" in n]
    assert mcp_names and all(re.fullmatch(r"[a-zA-Z0-9_-]+", n) for n in mcp_names), seen_tool_names


async def test_agent_run_calls_mcp_tool(db_sessionmaker, monkeypatch):
    """End-to-end: in-process FastMCP server attached as a toolset; TestModel
    calls every available tool, so the MCP tool must be reachable."""
    from fastmcp import FastMCP
    from pydantic_ai.mcp import MCPToolset
    from pydantic_ai.models.test import TestModel

    import app.agents.runtime as runtime
    from app.agents.runtime import execute_run
    from app.memory.embeddings import FakeEmbedder
    from app.models import AgentRun, ModelProvider
    from app.telegram.limiter import SendLimiter
    from tests.test_agent import AgentFakeBot

    calls: list[str] = []
    demo = FastMCP("demo-calendar")

    @demo.tool
    def create_event(title: str) -> str:
        """Create a calendar event."""
        calls.append(title)
        return f"created {title}"

    monkeypatch.setattr(runtime, "build_model", lambda provider: TestModel())

    async with db_sessionmaker() as s:
        bot = Bot(tg_bot_id=999, username="cb", name="cb", token_encrypted="x",
                  can_read_all_group_messages=True)
        s.add(bot)
        await s.flush()
        chat = Chat(bot_id=bot.id, tg_chat_id=-100, type="supergroup", status="authorized")
        s.add(chat)
        s.add(ModelProvider(role="agent", base_url="http://unused", model_name="t"))
        await s.flush()
        run = AgentRun(chat_id=chat.id, trigger="mention", request_text="make an event")
        s.add(run)
        await s.commit()
        run_id = run.id

    fake = AgentFakeBot()
    await execute_run(
        db_sessionmaker,
        FakeEmbedder(),
        SendLimiter(),
        fake,
        run_id,
        extra_toolsets=[MCPToolset(demo)],
    )

    async with db_sessionmaker() as s:
        run = (await s.execute(select(AgentRun))).scalar_one()
        assert run.status == "done", run.error
    assert calls, "MCP tool was never called"
