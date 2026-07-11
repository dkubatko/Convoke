# Adding a movie (OMDb) MCP to Convoke

Goal: the group is deciding what to watch — the agent looks up ratings, runtime, and plot so
it can weigh in ("Dune: Part Two is 2h46, 8.5 on IMDb, 92% RT"). Unlike the calendar and
Splitwise examples, this needs **no custom code** — a movie lookup is a stateless, public,
read-only query with a single shared API key, so an off-the-shelf community server is exactly
right. (That's the dividing line: stateless public tools → community servers; stateful,
credential-per-identity tools → a first-party shim. See the [examples index](README.md).)

Backed by [OMDb](https://www.omdbapi.com/), the Open Movie Database. Time: ~10 minutes.

## 1. Get an OMDb API key

1. Go to [omdbapi.com/apikey.aspx](https://www.omdbapi.com/apikey.aspx) → pick the **FREE**
   tier (1,000 requests/day) → enter your email.
2. OMDb emails you a key with an **activation link you must click** before it works. (Delivery
   to Outlook/Hotmail/Yahoo can be slow — if it doesn't arrive within an hour, try another
   address.)

A raw OMDb request looks like `https://www.omdbapi.com/?apikey=KEY&t=Inception`; the MCP
server wraps this so the agent never sees the key.

## 2. Run the server

Uses [`tyrell/omdb-mcp-server`](https://github.com/tyrell/omdb-mcp-server) — OMDb-native and
serves MCP over HTTP directly, so there's nothing to bridge. A prebuilt container exists, so
you don't need a JDK:

```bash
docker run -p 8081:8081 -e OMDB_API_KEY=your-key ghcr.io/tyrell/omdb-mcp-server:latest
```

It serves the MCP endpoint at `http://<host>:8081/mcp`. Tools it exposes: `search_movies`
(by title/year), `get_movie_details` (rating, plot, runtime, genre, Rotten Tomatoes &
Metacritic), and `get_movie_by_imdb_id`.

Confirm it's alive:

```bash
curl -X POST http://localhost:8081/mcp -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":"1","method":"tools/call",
       "params":{"name":"search_movies","arguments":{"title":"The Matrix","year":"1999"}}}'
```

To keep it running, fold it into `docker-compose.yml` as a sidecar:

```yaml
  omdb-mcp:
    image: ghcr.io/tyrell/omdb-mcp-server:latest
    restart: unless-stopped
    environment:
      OMDB_API_KEY: ${OMDB_API_KEY}
```

…then register it as `http://omdb-mcp:8081/mcp` (service name, in-network).

## 3. Register it in Convoke

**Tools** page → *Register an MCP server*:

| Field | Value |
| --- | --- |
| Name | `Movies` |
| Transport | Streamable HTTP |
| URL | `http://host.docker.internal:8081/mcp` (host run) or `http://omdb-mcp:8081/mcp` (sidecar) |
| Authentication | **None** — the OMDb key lives on the server |

Press **Test connection**, then **Register server**. `host.docker.internal`, not `localhost`
— the worker runs in a container.

> **If the connection test fails:** this server is built on Spring AI's MCP stack, which is
> slightly older than the current Streamable HTTP spec. Most clients POST to `/mcp` fine (the
> curl above proves it answers), but if Convoke's test won't negotiate, run a stdio movie
> server bridged to HTTP instead — e.g.
> [`movie-metadata-mcp`](https://github.com/stevenaubertin/movie-metadata-mcp) via
> `npx -y supergateway --stdio "node dist/index.js" --port 8000 --baseUrl http://127.0.0.1:8000`
> (the [weather example](weather-mcp.md#option-c--accuweather-free-api-token) uses the same
> supergateway pattern). Note that one also wants a TMDB key.

Then **Chats → your group → Tools tab → tick `Movies`**.

## 4. Try it

In the group, mention it or ask directly:

> **alice**: movie night friday? thinking Dune or Oppenheimer
> **bob**: @yourbot which is better and how long are they?

The agent calls `get_movie_details` for each and answers with ratings and runtimes. No
workflow needed — this is a reply-on-mention tool, not an intent trigger (though you could
pair it with the calendar example: agree on a film and time → the event scheduler books
movie night). The run and its tool calls appear under the chat's **Agent runs** tab.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Connection test fails to `:8081/mcp` | Server not running/reachable from the worker (`host.docker.internal`, not `localhost`), or the transport mismatch above — use the supergateway fallback. |
| Tool calls return an OMDb auth error | `OMDB_API_KEY` not set in the server's environment, or the key was never activated via the email link. |
| "Movie not found" for a real film | OMDb matches on exact-ish titles; add a `year`, or have the agent `search_movies` first to get the right title/IMDb id. |
| Daily lookups stop working | Free tier is 1,000 requests/day — it resets at midnight, or upgrade the OMDb key. |
| Tools don't appear in a run | Server registered but not **enabled** (Tools page), or not ticked for that chat (chat → Tools tab). |
