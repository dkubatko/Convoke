# Adding a weather MCP to Convoke

Three ways to give your agents weather tools, fastest first: a fully hosted server
(nothing to install — register, get a key, paste a URL), a free self-hosted one, and
an AccuWeather-backed one. In every case the pattern is the same: **get an MCP
endpoint the worker can reach over HTTP, register its URL on the Tools page, then
enable it per chat.**

---

## Option A — Hosted on Smithery (zero install)

[Smithery](https://smithery.ai) hosts thousands of MCP servers behind HTTPS endpoints,
authenticated with one Smithery API key. No local installs, no processes to keep
running. Their UI pushes the CLI/SDK path, but plain HTTP endpoints exist — the
documented one is the **namespace endpoint**, which bundles every server you've added
behind a single URL.

**1. Get your key:** sign up at [smithery.ai](https://smithery.ai) → the server page's
**Integrate** tab → **Create API key** (or account settings → API keys).

**2. Add a weather server to your toolbox/namespace:** on the
[United States Weather](https://smithery.ai/servers/smithery-ai/national-weather-service)
page press **Add to toolbox** (free US forecast data via weather.gov — no upstream
key). This creates a connection inside your namespace; note the namespace name (or
create one under **Manage Connections**).

**3. Register it in Convoke** — Tools page → *Register an MCP server*:

| Field | Value |
| --- | --- |
| Name | `Weather` |
| Transport | Streamable HTTP |
| URL | `https://mcp.smithery.run/YOUR_NAMESPACE` |
| Authentication | **Bearer token** → your Smithery API key |

The key is sent as an `Authorization: Bearer` header and stored encrypted.

**4. Enable it per chat** — Chats → your chat → **Tools** tab → tick `Weather` — and
ask the bot about the weather.

Notes:
- Everything in the namespace arrives as one toolset, so keep a dedicated namespace
  per concern if you want per-chat granularity in Convoke.
- A per-server endpoint also exists (`https://server.smithery.ai/@smithery-ai/national-weather-service/mcp`,
  answers 401 without credentials) but isn't surfaced in the current UI; the namespace
  endpoint is the documented, stable path.
- Servers needing their own upstream API key (OpenWeatherMap-style) work the same way —
  Smithery collects that config when you add the connection.

---

## Option B — Self-hosted, Open-Meteo (no API key)

> Why not stdio? Convoke supports stdio MCP servers, but the command must exist inside
> the backend container image. Running weather as an HTTP service on your host keeps
> the image untouched and the server independently restartable.

Uses [mcp_weather_server](https://mcpservers.org/servers/isdaniel/mcp_weather_server),
which wraps the free [Open-Meteo](https://open-meteo.com) API. Tools include current
weather, forecasts by date range, air quality, and timezone helpers.

**1. Run it on your host** (any machine the Docker network can reach):

```bash
pip install mcp_weather_server
python -m mcp_weather_server --mode streamable-http --host 0.0.0.0 --port 3005
```

It serves a single MCP endpoint at `http://<host>:3005/mcp`.

**2. Register it in Convoke** — Tools page → *Register an MCP server*:

| Field | Value |
| --- | --- |
| Name | `Weather` |
| Transport | Streamable HTTP |
| URL | `http://host.docker.internal:3005/mcp` |
| Bearer token | *(leave blank)* |

`host.docker.internal` is how containers reach services on your machine — don't use
`localhost`, that's the container itself.

**3. Enable it for a chat** — Chats → your chat → **Tools** tab → tick `Weather`.

**4. Try it** — in the group: `@yourbot what's the weather in Lisbon this weekend?`
The run appears under the chat's *Agent runs* tab with the tool calls it made.

To keep it running permanently, either add a systemd/launchd service for the command
above, or fold it into `docker-compose.yml` as a sidecar:

```yaml
  weather-mcp:
    image: python:3.12-slim
    restart: unless-stopped
    command: sh -c "pip install mcp_weather_server && python -m mcp_weather_server --mode streamable-http --host 0.0.0.0 --port 3005"
```

…then register it as `http://weather-mcp:3005/mcp` (service name, in-network).

---

## Option C — AccuWeather (free API token)

Uses [@timlukahorstmann/mcp-weather](https://github.com/timlukahorstmann/mcp-weather)
(Node ≥18): hourly/daily forecasts backed by AccuWeather.

**1. Get the token:**

1. Sign up at [developer.accuweather.com](https://developer.accuweather.com/) (free).
2. **My Apps** → **Add a new App** — any name; pick the *Core Weather Limited Trial*
   (free tier, 50 calls/day).
3. Copy the **API Key** shown on the app card.

**2. Run the server.** It speaks stdio natively; bridge it to HTTP with supergateway:

```bash
export ACCUWEATHER_API_KEY=your_key_here
npx -y supergateway --stdio "npx -y @timlukahorstmann/mcp-weather" \
  --port 4004 --baseUrl http://127.0.0.1:4004
```

**3. Register in Convoke** — same pattern, with URL
`http://host.docker.internal:4004/mcp` (check supergateway's startup log for the exact
path it prints). The API key stays on the server side — Convoke never needs it.

**4. Enable per chat and test**, as above.

---

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Agent run errors with a connection failure to the tool | The URL isn't reachable *from the worker container*. Use `host.docker.internal` (host service) or the compose service name (sidecar), never `localhost`. |
| Tools don't appear in a run | The server is registered but not **enabled** (Tools page toggle), or not ticked for that chat (chat → Tools tab). |
| AccuWeather calls fail with 401/403 | Key not exported in the server's environment, or the free-tier daily quota (50 calls) is exhausted. |
