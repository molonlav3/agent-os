# HTTP API

The AgentOS gateway is a [Starlette](https://www.starlette.io/) ASGI
application that exposes a **REST HTTP API** alongside its streaming WebSocket.
Every AgentOS surface — CLI, Web UI, channels, and external clients — talks to
this same gateway, so the HTTP endpoints below are a stable integration point
for your own applications and automation.

Use this page when you want to call AgentOS from an external app, script, or
service instead of the CLI.

## Base URL and Auth

The gateway binds to loopback by default:

```text
http://127.0.0.1:18791
```

**On the default loopback bind, no token is required.** The gateway ships with
`auth.mode = "none"`, and a loopback peer is admitted as a Control connection,
so `curl http://127.0.0.1:18791/api/...` works with no credentials.

A token is only enforced when `auth.mode = "token"`. A public address
(`0.0.0.0` / LAN) needs that mode or `trusted-proxy`: the startup guard
refuses to serve an unauthenticated public bind, and a token is
auto-generated when unset. See [`gateway.md`](gateway.md) for bind safety and
[`remote-access.md`](remote-access.md) for tunnels and reverse proxies.

When auth is enabled, send the token via HTTP headers (checked in order):

```text
Authorization: Bearer <token>
X-Agentos-Token: <token>
```

> **Note:** Query-string tokens (`?token=<token>`) are rejected (`401 Unauthorized`). Passing tokens in URL query parameters is not supported to prevent token leaks in access logs, browser history, and HTTP referrers.

The gateway implements exactly three auth modes — `none`, `token`, and
`trusted-proxy`. Any other value (including the never-implemented `"password"`,
or a typo like `"tokenn"`) is refused when the config loads rather than silently
admitting every request; the startup error names the supported modes.

`trusted-proxy` admits a request only when the transport peer is listed in
`auth.trusted_proxy` (IP literals or CIDR blocks such as `10.0.0.0/8`,
comma-separated). `X-Forwarded-For` is read only from such a peer and walked
right to left, skipping trusted hops; an address naming the gateway host
itself is ignored as spoofed.

### Failed-auth rate limiting

A *wrong* token counts against a budget per client address, shared by HTTP
and the WebSocket: ten failures within 60 seconds lock that address out for
five minutes, whatever `Origin` header its attempts carry. A locked-out
client gets `429` with `Retry-After` (seconds) and

```json
{"error": "Too many failed authentication attempts", "code": "RATE_LIMITED",
 "details": {"code": "AUTH_RATE_LIMITED", "retry_after_ms": 299000}}
```

even when it presents the right token. A request with no token is a plain
`401` and never counts. A loopback address is never locked out (each failure
is answered after a delay growing from 250 ms to 5 s), but browser requests
(those carrying `Origin`) are also counted in a second budget per address,
shared by every `Origin` value and never exempt, so a page in the operator's
browser cannot guess the token from loopback, and rotating `Origin` gains
nothing. Behind a proxy on the same host every client looks like loopback:
list the proxy in `auth.trusted_proxy` (its `X-Forwarded-For` then names the
client) or set `exempt_loopback = false`. Settings:

```toml
[auth.rate_limit]
enabled = true
max_failures = 10
window_seconds = 60
lockout_seconds = 300
exempt_loopback = true
```

`/health`, `/ready` and `/startupz` never require a token. Cross-origin browser requests are
governed by CORS configuration regardless of auth mode.

## Liveness and Readiness

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health`, `/healthz` | Liveness probe — `{"ok": true, "status": "live"}`. |
| `GET` | `/ready`, `/readyz` | Readiness probe — `{ready, state, failing, startup, safe_mode, drain_budget_ms, uptime_ms}`; `state` is `starting`, `ready`, `draining` or `safe_mode`. `503` while starting or draining, `200` when ready and in safe mode. `failing` also lists down channels as `channel:<name>` without making the gateway unready. The older `status` field mirrors `state`. |
| `GET` | `/startupz` | Startup probe — `200` once every boot sidecar settled (channels started, router preloaded), `503` with the `pending` list before. |

These require no auth and are safe for load balancers and container probes.
The full supervisor contract (WebSocket refusal during boot, the `shutdown`
event, exit code `78`) is in [`gateway.md`](gateway.md#supervisor-contract).

## Core Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/config` | Effective gateway configuration. |
| `GET` | `/api/system/status` | Version, uptime, active provider, auth mode, plus `circuitBreaker` (the active provider's breaker) and `circuitBreakers` (every tracked provider). |
| `GET` | `/api/sessions` | List sessions. |
| `POST` | `/api/chat` | Send a chat turn. Body: `message` (required), `sessionKey` (optional). |
| `GET` | `/api/chat/history?sessionKey=<key>` | Fetch a session transcript. |
| `GET` | `/api/agents` | List durable agents. |
| `GET` | `/api/cron` | List scheduled jobs. |
| `GET` | `/api/usage` | Token usage and cost breakdown. |
| `GET` | `/metrics` | Prometheus metrics exposition (`observability.metrics_path`). |
| `POST` | `/api/v1/rpc` | Call an allowlisted RPC method: `{id?, method, params?}` → `{id, ok, payload \| error}`. See [RPC over HTTP](#rpc-over-http). |

## Channels

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/channels/status` | Channel connection status. |
| `POST` | `/api/channels/logout` | Log a channel out. |

## Approvals and Permissions

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/approvals` | Pending approvals + current mode/patterns. |
| `POST` | `/api/approvals/settings` | Set mode (`prompt` / `auto-approve` / `auto-deny`). |
| `POST` | `/api/approvals/resolve` | Approve or deny a pending item (optional `payload` object carries ACP form answers). |
| `POST` | `/api/elevated-mode` | Set per-session elevated mode (admitted Control connection only). |

`GET /api/approvals` serializes every pending command and its arguments, so it
is rate limited per client IP like the rest of `/api/*` — just in its own
bucket, because the Web UI polls it every 1.5s. The cap is
`AGENTOS_RATE_APPROVALS_MAX_REQUESTS` (default 300 per
`AGENTOS_RATE_WINDOW_SECONDS`, i.e. 300/min), which clears several open tabs.
Raise it only if you run more consoles than that against one gateway.

## Files and Media

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/v1/files/upload` | Upload a file; returns an opaque id for `chat.send`. |
| `GET` | `/api/v1/attachments/{sha256}` | Fetch an attachment by content hash. |
| `GET` | `/api/v1/artifacts/{artifact_id}` | Fetch a generated artifact. |
| `POST` | `/api/audio/transcribe` | Transcribe audio to text. |

## RPC over HTTP

`POST /api/v1/rpc` calls one gateway RPC method without a WebSocket, behind the
same bearer auth, origin guard and rate limits as the other `/api/*` routes:

```sh
curl -X POST -H "Authorization: Bearer $AGENTOS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"id": "1", "method": "sessions.list", "params": {"limit": 5}}' \
  http://127.0.0.1:18791/api/v1/rpc
```

The body is `{id?, method, params?}` (`id` a string or integer, echoed back;
`params` an object). The answer is the WebSocket `res` frame without its
`type`: `{"id", "ok": true, "payload"}` with `200`, or `{"id", "ok": false,
"error": {code, message, details, retryable, retry_after_ms}}` with a status
from the error's category:

| Category | Status |
| --- | --- |
| `INVALID_REQUEST` | `400` |
| `UNAUTHORIZED`, `FORBIDDEN` | `403` |
| `NOT_FOUND`, `METHOD_NOT_FOUND` | `404` |
| `RATE_LIMITED` | `429` |
| `UNAVAILABLE` | `503` |
| `INTERNAL_ERROR` and anything else | `500` |

Only an allowlist is reachable: reads (`health`, `status`, `config.get`,
`sessions.list`/`get`/`preview`/`resolve`, `chat.history`, `models.list`,
`usage.*`, `agents.list`, `projects.list`/`get`, `skills.list`, `cron.list`,
`channels.status`, `doctor.status`, `acp.*` status, ...) and mutations that
create, rename or send without deleting anything (`chat.send`,
`sessions.send`, `sessions.create`, `sessions.rename`,
`projects.create`/`update`, `*.abort`, `plan.mode.set`). `chat.send` and
`sessions.send` start an agent turn with the same options as over the
WebSocket, including an elevated execution mode the request asks for, so the
token must be treated as full operator access either way. Configuration
writes, secrets and environment variables, trading and wallets, restarts,
deletes and approval decisions stay on the WebSocket. Any other method answers `403` with `error.code = "FORBIDDEN"` and
`details.code = "METHOD_NOT_ALLOWED_OVER_HTTP"`. The full list is
`HTTP_RPC_METHODS` in
[`src/agentos/gateway/http_rpc.py`](../src/agentos/gateway/http_rpc.py).

## Streaming (WebSocket)

For streaming turns and live events, connect to the WebSocket route:

```text
ws://127.0.0.1:18791/ws
```

The WebSocket carries the same JSON-RPC methods the HTTP endpoints dispatch to,
plus server-pushed events. It is one route on the same app — the REST surface
above sits alongside it. See [`mcp-server.md`](mcp-server.md) for a bridge that
uses this transport.

### Handshake

1. The server sends `connect.challenge` with `{nonce, ts}` (`ts` in ms since
   the epoch).
2. The client answers with a `req` whose `method` is `connect` and whose
   `params` are `{minProtocol, maxProtocol, clientKind, client, auth?}`. The
   current protocol is `3`; send `minProtocol = maxProtocol = 3`. `client` is
   informational, `{id, version, platform, mode}`, every field optional.
   `auth.token` carries the gateway token.
   While the gateway is still booting, `connect` is answered with a retryable
   `UNAVAILABLE` (`details.code: "STARTUP_SIDECARS"`, `retry_after_ms`) and the
   socket closes `1013`; retry after `retry_after_ms`.
3. The server answers with a bare `hello-ok` frame (not a `res`) carrying
   `features.methods`, `features.events`, `features.capabilities` (additive
   feature names such as `typed-errors`, `challenge-ts`, `auth-rate-limit`,
   `rpc-http`, `supervisor-contract`; treat an unknown one as absent), `policy` and
   `auth.nonce` (the challenge nonce, so a client can check it answered its
   own challenge).

Messages before `hello-ok` are capped at 64 KiB: a larger one (plain,
fragmented or compressed) closes the socket with `1009` before it is read.
After it the cap is `policy.max_payload` (25 MiB). One address may hold at
most 128 sockets in the handshake; the next upgrade is refused with HTTP 403. Malformed
frames answer `INVALID_REQUEST` with `details.code` `INVALID_FRAME` or
`INVALID_CONNECT_PARAMS` and an `errors` list. A protocol range the server
cannot meet answers `INVALID_REQUEST` with `details {code:
"PROTOCOL_MISMATCH", client_min, client_max, server_min, server_max,
reload_required}`; `reload_required` is true when the client is the older
side (a stale web console bundle).

### Errors

`error.code` is one of `INVALID_REQUEST`, `UNAUTHORIZED`, `FORBIDDEN`,
`NOT_FOUND`, `METHOD_NOT_FOUND`, `UNAVAILABLE`, `RATE_LIMITED`,
`INTERNAL_ERROR`, a long-standing code kept for existing clients
(`QUEUE_FULL`, `GATEWAY_RESTARTING`, ...), or a namespaced domain code
(`project.conflict`, `trading.operator_required`). The specific reason of a
new error is in `details.code` (`PROTOCOL_MISMATCH`, `AUTH_RATE_LIMITED`,
`REQUEST_START_TIMEOUT`, `QUEUE_FULL`, ...). `retryable: true` says trying
again later can succeed.

### Concurrency and backpressure

Up to four requests per connection run at once. Requests naming the same
session (`sessionKey`, or `key` on `sessions.*`) run in order, and a request
naming no session waits for every earlier one, so a client can still rely on
`chat.send` before `sessions.abort` or `subscribe` before a history read.
Session-less reads (`sessions.list`, `status`, `config.get`, `models.list`,
`usage.*`, ...) wait only for earlier session-less writes, so a poll is not
held back by another session's slow history. A request that cannot start
within 30 seconds answers `UNAVAILABLE` with
`details.code = "REQUEST_START_TIMEOUT"` (retryable).

Event frames carry a per-connection `seq`. When more than
`policy.max_buffered_bytes` (50 MiB) is waiting for a client that is not
reading, periodic refreshes (`tick`, `presence`, `health`) are dropped and
their `seq` still consumed, so the client sees a gap and resynchronizes;
anything else closes the socket with `1008` `slow consumer`.

Projects (session groups with shared knowledge) have no REST route of their
own: `projects.create` / `projects.list` / `projects.get` / `projects.update`
are RPC methods (WebSocket, or `POST /api/v1/rpc`), `projects.delete` is
WebSocket-only (params use `projectId`, `agentId`, `name`, `knowledge`;
delete responds with `sessionsCleared` — sessions are detached, not deleted).
`projects.update` writes only the fields you pass and accepts an optional
`expectedUpdatedAt` (the `updatedAt` you last read): when the row changed in
between, the call fails with code `project.conflict` instead of silently
overwriting the other writer's edit.
`sessions.create` accepts an optional `projectId`, `sessions.patch` moves a
session with `projectId` (explicit `null` detaches), and `sessions.list`
accepts a `projectId` filter and emits `project_id`/`projectId` on every row.
`sessions.patch` and `sessions.rename` on an ephemeral WebChat key
(`agent:<id>:webchat:<suffix>`) that has no row yet create it first — the WebUI
mints that key client-side and only sends it on the first message; every other
key shape still fails with `NOT_FOUND`.
Project CRUD broadcasts a `projects.changed` event to connected clients.

### Environment variables (`env.*`)

Control-surface only. Values never appear in a listing.

| Method | Purpose |
| --- | --- |
| `env.list` | Every known variable: name, set/unset, source, description, owner, and a **masked** value. |
| `env.set` | Write one variable. Returns its new state without echoing the value. |
| `env.unset` | Remove one variable from `~/.agentos/.env`. |
| `env.reveal` | Return one real value. Rate limited to 5 per 30s and written to the audit log. |

Writes are refused for names that steer subprocess execution or AgentOS runtime
posture; see
[configuration.md](configuration.md#what-cannot-be-written-through-agentos).
`env.set` and `env.unset` report `restartRequired` per variable — provider
clients are built at boot with the key they had then, while other variables are
picked up by the next process AgentOS spawns.

## Example

On the default loopback bind these work as-is, no token needed:

```sh
# Liveness
curl http://127.0.0.1:18791/health

# System status
curl http://127.0.0.1:18791/api/system/status

# Send a chat turn (message is required; sessionKey is optional)
curl -X POST \
  -H "Content-Type: application/json" \
  -d '{"sessionKey": "agent:main:webchat:default", "message": "hello"}' \
  http://127.0.0.1:18791/api/chat
```

When the gateway runs with `auth.mode = "token"` (any public bind), add the
token to each `/api/*` call:

```sh
curl -H "Authorization: Bearer $AGENTOS_TOKEN" \
  https://gateway.example.com/api/system/status
```

## Source

The full route table is defined in `create_gateway_app`:
[`src/agentos/gateway/app.py`](../src/agentos/gateway/app.py) (the `routes`
list). File-and-media routes are registered just below it via
`register_upload_routes`, `register_attachment_routes`,
`register_artifact_routes`, and `register_audio_transcription_routes`.

Read next:

- [`gateway.md`](gateway.md)
- [`mcp-server.md`](mcp-server.md)
- [`web-ui.md`](web-ui.md)

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/use-agent-os/agent-os/issues/new?template=docs_report.yml)
