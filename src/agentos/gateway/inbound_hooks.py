"""Inbound webhooks: ``POST /api/v1/hooks/agent``, ``/wake`` and ``/<name>``.

External automation -- CI, a repository's webhooks, a no-code workflow -- can
reach the agent without a chat channel:

* ``/api/v1/hooks/agent`` ``{message, agent?, session_key?}`` enqueues a user
  message into a session, exactly as the console's send would.
* ``/api/v1/hooks/wake`` ``{reason?}`` asks the heartbeat to run now.
* ``/api/v1/hooks/<name>`` takes any JSON body and turns it into a message for
  the agent and session ``[hooks.routes.<name>]`` names, through that route's
  ``template`` (``{{ payload.pull_request.title }}``).

They authenticate with their own bearer, ``[hooks] token`` (``Authorization:
Bearer`` or ``X-AgentOS-Hook-Token``), never the gateway token -- presenting
that one is refused, so a leaked webhook secret is not a Control credential
and vice versa. With no hooks token configured the routes answer ``404``.

Guard rails, per hook name: ``rate_limit_per_minute`` (default 60) answered
with ``429``; ``idempotency_key`` (body) or an ``Idempotency-Key`` header
replays the first response for five minutes; and a fan-out cap,
``max_sessions_per_hook`` (default 1), on the distinct sessions a hook may
deliver into. A caller-chosen ``session_key`` must stay in the hook namespace
``agent:<agent>:hook:...``; only a route's own ``session_key`` (written by the
operator) can point a hook at an existing conversation.
"""

from __future__ import annotations

import hmac
import json
import re
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import structlog
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from agentos.gateway.config import GatewayConfig

log = structlog.get_logger(__name__)

HOOKS_PREFIX = "/api/v1/hooks"
IDEMPOTENCY_TTL_SECONDS = 300.0
IDEMPOTENCY_MAX_ENTRIES = 2048
RATE_WINDOW_SECONDS = 60.0
MAX_MESSAGE_CHARS = 32_000
MAX_BODY_BYTES = 256 * 1024
_TEMPLATE_FIELD = re.compile(r"\{\{\s*([A-Za-z0-9_.\-]+)\s*\}\}")
_HOOK_SEGMENT = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


def _json_error(status: int, code: str, message: str, **headers: str) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "error": message, "code": code},
        status_code=status,
        headers=headers or None,
    )


def _not_found() -> JSONResponse:
    return _json_error(404, "NOT_FOUND", "Not found")


def render_template(template: str, payload: Any) -> str:
    """Fill ``{{ payload.a.b }}`` / ``{{ a.b }}`` placeholders from ``payload``.

    No expressions, filters or code: a placeholder is a dotted path, list
    indexes allowed (``commits.0.message``). A missing path renders empty; a
    nested object renders as compact JSON.
    """

    def lookup(match: re.Match[str]) -> str:
        parts = match.group(1).split(".")
        if parts and parts[0] == "payload":
            parts = parts[1:]
        node: Any = payload
        for part in parts:
            if isinstance(node, dict):
                node = node.get(part)
            elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
                node = node[int(part)]
            else:
                return ""
            if node is None:
                return ""
        if isinstance(node, str):
            return node
        return json.dumps(node, ensure_ascii=False, separators=(",", ":"))

    return _TEMPLATE_FIELD.sub(lookup, template)


@dataclass
class _Replay:
    created_at: float
    status: int
    body: dict[str, Any]


class HookGuards:
    """Per-hook rate windows, idempotency replays and fan-out bookkeeping."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._windows: dict[str, deque[float]] = {}
        self._replays: OrderedDict[tuple[str, str], _Replay] = OrderedDict()
        self._sessions: dict[str, set[str]] = {}

    def throttled(self, hook: str, limit: int) -> float | None:
        """Seconds until ``hook`` may be called again, or None to admit (and count) it."""
        now = self._clock()
        window = self._windows.setdefault(hook, deque())
        while window and now - window[0] >= RATE_WINDOW_SECONDS:
            window.popleft()
        if len(window) >= limit:
            return max(1.0, RATE_WINDOW_SECONDS - (now - window[0]))
        window.append(now)
        return None

    def replay(self, hook: str, key: str) -> _Replay | None:
        now = self._clock()
        for stale in [
            k for k, v in self._replays.items() if now - v.created_at >= IDEMPOTENCY_TTL_SECONDS
        ]:
            self._replays.pop(stale, None)
        return self._replays.get((hook, key))

    def remember(self, hook: str, key: str, status: int, body: dict[str, Any]) -> None:
        self._replays[(hook, key)] = _Replay(self._clock(), status, body)
        while len(self._replays) > IDEMPOTENCY_MAX_ENTRIES:
            self._replays.popitem(last=False)

    def admit_session(self, hook: str, session_key: str, cap: int) -> bool:
        seen = self._sessions.setdefault(hook, set())
        if session_key in seen:
            return True
        if len(seen) >= cap:
            return False
        seen.add(session_key)
        return True


def _presented_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if header.startswith("Bearer "):
        return header[7:].strip()
    return request.headers.get("x-agentos-hook-token", "").strip()


def _hooks_config(config: Any) -> Any:
    return getattr(config, "hooks", None)


def default_hook_session(agent_id: str, name: str) -> str:
    return f"agent:{agent_id}:hook:{name}"


def _in_hook_namespace(session_key: str, agent_id: str) -> bool:
    prefix = f"agent:{agent_id}:hook:"
    if not session_key.startswith(prefix):
        return False
    rest = session_key[len(prefix) :]
    return all(_HOOK_SEGMENT.fullmatch(part) for part in rest.split(":")) if rest else False


def register_inbound_hook_routes(
    app: Starlette,
    *,
    config: GatewayConfig,
    dispatch_message: Callable[[str, str, dict[str, Any]], Any],
    wake: Callable[[str], Any] | None = None,
    guards: HookGuards | None = None,
) -> None:
    """Register the hook routes.

    ``dispatch_message(session_key, message, provenance)`` enqueues a turn and
    returns its result dict (awaitable); ``wake(reason)`` nudges the heartbeat
    and returns whether one is wired.
    """
    from agentos.session.keys import normalize_agent_id

    state = guards or HookGuards()

    def authenticate(request: Request) -> JSONResponse | None:
        hooks = _hooks_config(config)
        expected = str(getattr(hooks, "token", "") or "")
        if not expected:
            return _not_found()
        gateway_token = str(getattr(getattr(config, "auth", None), "token", "") or "")
        if gateway_token and hmac.compare_digest(expected, gateway_token):
            log.error(
                "gateway.hooks_token_reuses_gateway_token",
                hint="set a [hooks] token different from the gateway token",
            )
            return _not_found()
        presented = _presented_token(request)
        if gateway_token and presented and hmac.compare_digest(presented, gateway_token):
            return _json_error(
                401, "UNAUTHORIZED", "The gateway token is not accepted here; use the hooks token"
            )
        if not presented or not hmac.compare_digest(presented, expected):
            return _json_error(401, "UNAUTHORIZED", "Unauthorized")
        return None

    async def read_body(request: Request) -> tuple[Any, JSONResponse | None]:
        raw = await request.body()
        if len(raw) > MAX_BODY_BYTES:
            return None, _json_error(413, "TOO_LARGE", f"body exceeds {MAX_BODY_BYTES} bytes")
        if not raw.strip():
            return {}, None
        try:
            return json.loads(raw), None
        except ValueError:
            return None, _json_error(400, "INVALID_REQUEST", "body must be JSON")

    def idempotency_key(request: Request, body: Any) -> str | None:
        key = body.get("idempotency_key") if isinstance(body, dict) else None
        key = key or request.headers.get("idempotency-key")
        if isinstance(key, str) and key.strip():
            return key.strip()[:200]
        return None

    async def deliver(
        hook: str, agent_id: str, session_key: str, message: str, provenance: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        if not message.strip():
            return 400, {"ok": False, "error": "message is empty", "code": "INVALID_REQUEST"}
        hooks = _hooks_config(config)
        cap = int(getattr(hooks, "max_sessions_per_hook", 1) or 1)
        if not state.admit_session(hook, session_key, cap):
            return 409, {
                "ok": False,
                "error": f"hook {hook!r} already delivers into {cap} session(s)",
                "code": "FANOUT_LIMIT",
            }
        if len(message) > MAX_MESSAGE_CHARS:
            message = message[:MAX_MESSAGE_CHARS]
        result = await dispatch_message(session_key, message, provenance)
        if isinstance(result, dict) and result.get("ok") is False:
            return 502, {
                "ok": False,
                "error": str(result.get("error") or "the turn could not be queued"),
                "code": str(result.get("code") or "UNAVAILABLE"),
            }
        log.info("gateway.hook_delivered", hook=hook, agent_id=agent_id, session_key=session_key)
        payload = {"ok": True, "hook": hook, "sessionKey": session_key}
        if isinstance(result, dict):
            for key in ("runId", "run_id", "taskId", "status"):
                if key in result:
                    payload[key] = result[key]
        return 202, payload

    async def guarded(
        request: Request,
        hook: str,
        handler: Callable[[Any], Any],
    ) -> JSONResponse:
        refusal = authenticate(request)
        if refusal is not None:
            return refusal
        hooks = _hooks_config(config)
        retry = state.throttled(hook, int(getattr(hooks, "rate_limit_per_minute", 60) or 60))
        if retry is not None:
            return _json_error(
                429,
                "RATE_LIMITED",
                "Too many requests for this hook",
                **{"Retry-After": str(int(retry))},
            )
        body, error = await read_body(request)
        if error is not None:
            return error
        key = idempotency_key(request, body)
        if key is not None:
            replay = state.replay(hook, key)
            if replay is not None:
                return JSONResponse({**replay.body, "duplicate": True}, status_code=replay.status)
        status, payload = await handler(body)
        if key is not None and status < 500:
            state.remember(hook, key, status, payload)
        return JSONResponse(payload, status_code=status)

    async def agent_hook(request: Request) -> JSONResponse:
        async def handle(body: Any) -> tuple[int, dict[str, Any]]:
            if not isinstance(body, dict):
                return 400, {
                    "ok": False,
                    "error": "body must be an object",
                    "code": "INVALID_REQUEST",
                }
            message = body.get("message")
            if not isinstance(message, str):
                return 400, {"ok": False, "error": "message is required", "code": "INVALID_REQUEST"}
            agent_id = normalize_agent_id(str(body.get("agent") or "main"))
            requested = body.get("session_key", body.get("sessionKey"))
            if requested is None:
                session_key = default_hook_session(agent_id, "agent")
            elif isinstance(requested, str) and _in_hook_namespace(requested.strip(), agent_id):
                session_key = requested.strip()
            else:
                return 403, {
                    "ok": False,
                    "error": f"session_key must be under agent:{agent_id}:hook:",
                    "code": "FORBIDDEN",
                }
            return await deliver(
                "agent",
                agent_id,
                session_key,
                message,
                {"kind": "external_user", "source": "webhook", "hook": "agent"},
            )

        return await guarded(request, "agent", handle)

    async def wake_hook(request: Request) -> JSONResponse:
        async def handle(body: Any) -> tuple[int, dict[str, Any]]:
            reason = body.get("reason") if isinstance(body, dict) else None
            reason = str(reason)[:200] if reason else "webhook"
            if wake is None:
                return 503, {"ok": False, "error": "no heartbeat to wake", "code": "UNAVAILABLE"}
            woke = wake(f"hook:{reason}")
            if not woke:
                return 503, {"ok": False, "error": "no heartbeat to wake", "code": "UNAVAILABLE"}
            log.info("gateway.hook_wake", reason=reason)
            return 202, {"ok": True, "hook": "wake", "reason": reason}

        return await guarded(request, "wake", handle)

    async def named_hook(request: Request) -> JSONResponse:
        name = str(request.path_params.get("name", ""))
        hooks = _hooks_config(config)
        routes = getattr(hooks, "routes", None) or {}
        route = routes.get(name)
        if route is None:
            # Authenticate first so an unauthenticated caller cannot map names.
            refusal = authenticate(request)
            return refusal if refusal is not None else _not_found()

        async def handle(body: Any) -> tuple[int, dict[str, Any]]:
            agent_id = normalize_agent_id(str(getattr(route, "agent", "main") or "main"))
            session_key = str(getattr(route, "session_key", "") or "") or default_hook_session(
                agent_id, name
            )
            template = str(getattr(route, "template", "") or "")
            if template:
                message = render_template(template, body)
            elif isinstance(body, dict) and isinstance(body.get("message"), str):
                message = body["message"]
            else:
                message = json.dumps(body, ensure_ascii=False)
            return await deliver(
                name,
                agent_id,
                session_key,
                message,
                {"kind": "external_user", "source": "webhook", "hook": name},
            )

        return await guarded(request, name, handle)

    app.router.routes.extend(
        [
            Route(f"{HOOKS_PREFIX}/agent", agent_hook, methods=["POST"]),
            Route(f"{HOOKS_PREFIX}/wake", wake_hook, methods=["POST"]),
            Route(f"{HOOKS_PREFIX}/{{name}}", named_hook, methods=["POST"]),
        ]
    )


__all__ = [
    "HOOKS_PREFIX",
    "HookGuards",
    "default_hook_session",
    "register_inbound_hook_routes",
    "render_template",
]
