# Convoke examples

Hands-on walkthroughs for giving your agents tools and putting them to work. New here? Start
with [`getting-started.md`](getting-started.md) to get Convoke running with a bot listening in
a group — every tool guide below assumes that baseline. Each tool then follows the same arc:
**get an MCP endpoint the worker can reach over HTTP → register its URL on the Tools page →
enable it per chat → (optionally) wire a workflow that fires it.**

## Walkthroughs

| Guide | What it builds | Custom server? |
| --- | --- | --- |
| [`getting-started.md`](getting-started.md) | Start here: Convoke running → a bot listening in a Telegram group | — |
| [`weather-mcp.md`](weather-mcp.md) | A weather tool, three ways (hosted, self-hosted, API-key) | No |
| [`omdb-mcp.md`](omdb-mcp.md) | Movie ratings/plot/runtime lookups for "what should we watch?" | No |
| [`gcal-mcp/`](gcal-mcp/) | Shared **group calendars** the agent can CRUD (one service account, many groups) | Yes (~220 lines, included) |
| [`splitwise-mcp/`](splitwise-mcp/) | **Group expense tracking** — who owes whom (one bot account, many groups) | Yes (~190 lines, included) |

## When you need a custom server (and when you don't)

The choice isn't arbitrary — it follows from what the tool touches:

- **Stateless, public, read-only tools with one shared key** (weather, movies) → use an
  existing **community or hosted** server. There's no identity to manage and no secret worth
  isolating, so writing code would be pure ceremony.
- **Stateful, credential-bearing, identity-coupled tools** (a calendar you write to, an
  expense ledger) → use a small **first-party shim** you run yourself. Community servers for
  these are built for a single user managing their own account; bending one into an unattended
  group bot inherits fragile OAuth and hands your full-access token to someone else's code.
  The two shims here are ~100 lines each and keep your credentials on a server you control.

This mirrors how Convoke itself is built: the agent's *action surface* is always MCP, but
capabilities intrinsic to Convoke's own data (memory, reading the conversation) are native
tools, not servers. MCP is the seam for *doing things in the world* — not for everything.

## The shared mechanics

- Register servers on the **Tools** page as **Streamable HTTP**; secrets stay server-side, so
  most register with **Authentication: None**. Non-OAuth servers require a successful **Test
  connection** before *Register* enables.
- Tools are **per-chat**: register once globally, then tick which chats may use each server
  (chat → **Tools** tab). Your family chat's agent doesn't need your work chat's tools.
- Reach host services at **`http://host.docker.internal:PORT/...`** and compose sidecars at
  **`http://<service-name>:PORT/...`** — never `localhost`, which is the worker container
  itself.
