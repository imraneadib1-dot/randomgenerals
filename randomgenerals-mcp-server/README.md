# randomgenerals-mcp-server

An MCP server for [randomgenerals.com](https://randomgenerals.com) — lets any
MCP-compatible client (Claude Desktop, Cursor, etc.) list your account's
available models and send chat messages through your own model routing
(pip 1.0 → OpenRouter → local Ollama), the same failover chain the website
itself uses.

## What's exposed (and what isn't)

randomgenerals.com's backend (`app.py`) has dozens of routes, but only two
are authenticated with an API key rather than a browser session cookie:

| Route | Tool |
| --- | --- |
| `GET /v1/models` | `randomgenerals_list_models` |
| `POST /v1/chat/completions` | `randomgenerals_chat` |

Everything else — image generation, web search, code execution, video
generation, diagrams, threads, memory, billing — is session-cookie only
today, so it isn't reachable from an external MCP client and isn't exposed
here. If you add API-key auth to any of those routes in `app.py` (mirroring
the `_api_user()` check on `/v1/*`), the corresponding tool can be added to
this server later.

## Setup

1. Generate an API key at <https://randomgenerals.com> → Settings → API
   (this calls `POST /api/account/api-key`; the key is shown once).
2. Build this server:

   ```bash
   npm install
   npm run build
   ```

3. Point your MCP client at it, e.g. in Claude Desktop's
   `claude_desktop_config.json`:

   ```json
   {
     "mcpServers": {
       "randomgenerals": {
         "command": "node",
         "args": ["/absolute/path/to/randomgenerals-mcp-server/dist/index.js"],
         "env": {
           "RANDOMGENERALS_API_KEY": "sk-..."
         }
       }
     }
   }
   ```

### Environment variables

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `RANDOMGENERALS_API_KEY` | yes | — | Bearer key from Settings → API. |
| `RANDOMGENERALS_BASE_URL` | no | `https://randomgenerals.com/v1` | Point at `http://127.0.0.1:5000/v1` for a local dev server instead. |
| `TRANSPORT` | no | `stdio` | Set to `http` to run as a Streamable HTTP server instead of stdio. |
| `PORT` | no | `3000` | Only used when `TRANSPORT=http`. |

## Tools

### `randomgenerals_list_models`

Read-only. No arguments. Returns the model ids currently reachable from
your account (your own Ollama models if one is online, otherwise a
placeholder id standing in for the hosted channels).

### `randomgenerals_chat`

Sends a full conversation (`messages: [{role, content}, ...]`) and returns
one assistant reply, non-streaming. Optional `model`, `temperature`,
`max_tokens`. Each call is metered against the account's credit balance,
exactly like a normal API call from any other OpenAI-compatible client.

Errors are translated into one clear sentence for the specific cases the
backend can return: invalid/missing key (401), out of credits (402), a
Pro-only model on a free account (403), and no channel available (503).

## Development

```bash
npm run dev     # tsx watch, runs src/index.ts directly
npm run build   # compile to dist/
npm start       # run the compiled server
```

Manual smoke test over stdio (no MCP client needed):

```bash
RANDOMGENERALS_API_KEY=sk-... node dist/index.js <<'EOF'
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0.0"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"randomgenerals_list_models","arguments":{}}}
EOF
```

You can also test with the official inspector:

```bash
npx @modelcontextprotocol/inspector node dist/index.js
```

## Why no eval suite

The mcp-builder process calls for ten realistic, read-only, *stable*
evaluation questions with single verifiable answers. That fits a server
sitting on a large, mostly-static dataset (a CRM, a ticket tracker, a
document store). This server has exactly one meaningfully variable
read — the model list — and its answer depends on which channel happens
to be up at query time (your local Ollama, if online; otherwise a
placeholder), so it fails the "stable answer" requirement by design: the
correct answer changes with server state, not with the question.

`evals/manual-checks.md` has a short, honest substitute: a handful of
scenarios to run by hand against a live account (valid key, revoked key,
Pro-only model on a free account, out-of-credits) with the exact
behavior each should produce, since those failure modes *are* stable and
worth checking after any change to `client.ts`.
