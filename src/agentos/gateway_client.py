"""Reusable WebSocket RPC client for AgentOS gateway integrations.

Besides :class:`GatewayRPCClient` this module holds the pieces every Python
client of the gateway shares (the CLI's ``GatewayClient`` imports them):

* the ``connect`` params: an honest ``minProtocol``/``maxProtocol`` and a
  ``client`` block ``{id, version, platform, mode}``;
* :class:`GatewayHandshakeError`, which says whether retrying can help — a
  wrong token or a protocol mismatch never heals by reconnecting;
* :class:`EventSeqTracker`: event frames carry a per-connection ``seq``; a
  gap means frames were lost, so the client closes with
  :data:`WS_CLOSE_SEQ_GAP` and reconnects, resubscribing each session from
  the last ``stream_seq`` it saw so the gateway replays what was missed;
* :func:`reconnect_delays`: 1 s, doubling, capped at 30 s.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from collections.abc import Iterator
from typing import Any, cast
from urllib.parse import urlparse, urlunparse

#: The protocol version these clients speak. ``tests`` pin it to the
#: gateway's ``protocol.PROTOCOL_VERSION``; importing that module here would
#: drag the whole gateway package into every CLI command.
CLIENT_PROTOCOL_VERSION = 3

#: Close code a client sends when it drops a connection over a seq gap.
WS_CLOSE_SEQ_GAP = 4000

#: Largest frame these clients accept. The websockets default (1 MiB) is
#: below what one ``chat.history`` page can carry; a refused frame would
#: drop the connection and send a reconnecting client round in circles.
CLIENT_MAX_FRAME_BYTES = 64 * 1024 * 1024

RECONNECT_INITIAL_DELAY_S = 1.0
RECONNECT_FACTOR = 2.0
RECONNECT_MAX_DELAY_S = 30.0


def reconnect_delays(
    initial: float = RECONNECT_INITIAL_DELAY_S,
    factor: float = RECONNECT_FACTOR,
    maximum: float = RECONNECT_MAX_DELAY_S,
) -> Iterator[float]:
    """Delays before each reconnect attempt: ``initial``, ×``factor``, ≤ ``maximum``."""
    delay = max(0.0, initial)
    while True:
        yield min(delay, maximum)
        delay = min(maximum, delay * factor) if delay > 0 else maximum


def client_block(client_id: str, mode: str) -> dict[str, str]:
    """The ``connect`` ``client`` block: who is connecting (informational)."""
    from agentos import __version__

    return {"id": client_id, "version": __version__, "platform": sys.platform, "mode": mode}


def build_connect_params(
    *, client_id: str, mode: str, auth: dict[str, str] | None = None
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "minProtocol": CLIENT_PROTOCOL_VERSION,
        "maxProtocol": CLIENT_PROTOCOL_VERSION,
        "clientKind": "control",
        "client": client_block(client_id, mode),
    }
    if auth:
        params["auth"] = dict(auth)
    return params


class GatewayHandshakeError(RuntimeError):
    """The gateway answered ``connect`` with an error.

    ``recoverable`` is false for the answers a retry cannot change: a wrong
    or rate-limited credential (``UNAUTHORIZED``) and a malformed or
    incompatible handshake (``INVALID_REQUEST``, including
    ``PROTOCOL_MISMATCH``). A reconnect loop stops on those.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.details = details or {}
        self.message = message
        #: The frame the gateway answered with, when there was one.
        self.frame: Any = None
        super().__init__(f"Gateway handshake failed: {code + ': ' if code else ''}{message}")

    @property
    def detail_code(self) -> str | None:
        value = self.details.get("code")
        return value if isinstance(value, str) else None

    @property
    def recoverable(self) -> bool:
        return self.code not in {"UNAUTHORIZED", "INVALID_REQUEST", "FORBIDDEN"}

    @classmethod
    def from_response(cls, frame: dict[str, Any]) -> GatewayHandshakeError:
        raw_error = frame.get("error")
        error: dict[str, Any] = raw_error if isinstance(raw_error, dict) else {}
        raw_details = error.get("details")
        raw_code = error.get("code")
        raw_message = error.get("message")
        error_obj = cls(
            raw_message if isinstance(raw_message, str) else "refused",
            code=raw_code if isinstance(raw_code, str) else None,
            details=raw_details if isinstance(raw_details, dict) else None,
        )
        error_obj.frame = frame
        return error_obj


class EventSeqTracker:
    """Checks that event ``seq`` numbers on one connection are contiguous."""

    def __init__(self) -> None:
        self.last = 0

    def reset(self) -> None:
        self.last = 0

    def observe(self, frame: dict[str, Any]) -> tuple[int, int] | None:
        """Record ``frame``'s seq; ``(expected, actual)`` on a gap, else ``None``."""
        seq = frame.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool):
            return None
        if self.last > 0 and seq != self.last + 1:
            return (self.last + 1, seq)
        self.last = seq
        return None


class SessionSubscriptions:
    """Sessions this client subscribed to, with the last ``stream_seq`` seen.

    Replayed on reconnect as ``sessions.messages.subscribe {key,
    since_stream_seq}`` so the gateway resends what the dropped socket missed.
    """

    def __init__(self) -> None:
        self._keys: dict[str, int | None] = {}
        self.session_list = False

    def note_call(self, method: str, params: Any, payload: Any) -> None:
        if method == "sessions.subscribe":
            self.session_list = True
        elif method == "sessions.unsubscribe":
            self.session_list = False
        if not isinstance(params, dict):
            return
        key = params.get("key")
        if not isinstance(key, str) or not key:
            return
        if method == "sessions.messages.subscribe":
            seq = payload.get("current_stream_seq") if isinstance(payload, dict) else None
            canonical = payload.get("key") if isinstance(payload, dict) else None
            current = seq if isinstance(seq, int) and not isinstance(seq, bool) else None
            self._keys[key] = self._max(self._keys.get(key), current)
            if isinstance(canonical, str) and canonical and canonical != key:
                self._keys[canonical] = self._max(self._keys.get(canonical), current)
        elif method == "sessions.messages.unsubscribe":
            self._keys.pop(key, None)

    def note_event(self, frame: dict[str, Any]) -> None:
        payload = frame.get("payload")
        if not isinstance(payload, dict):
            return
        key = payload.get("session_key")
        seq = payload.get("stream_seq")
        if (
            isinstance(key, str)
            and key in self._keys
            and isinstance(seq, int)
            and not isinstance(seq, bool)
        ):
            self._keys[key] = self._max(self._keys[key], seq)

    def resubscribe_calls(self) -> list[tuple[str, dict[str, Any]]]:
        calls: list[tuple[str, dict[str, Any]]] = []
        if self.session_list:
            calls.append(("sessions.subscribe", {}))
        for key, seq in self._keys.items():
            calls.append(("sessions.messages.subscribe", {"key": key, "since_stream_seq": seq}))
        return calls

    @staticmethod
    def _max(left: int | None, right: int | None) -> int | None:
        if left is None:
            return right
        if right is None:
            return left
        return max(left, right)


def open_websocket(url: str) -> Any:
    """``websockets.connect(url)`` with the frame ceiling above.

    Test doubles that only take ``url`` keep working.
    """
    import websockets

    try:
        return websockets.connect(url, max_size=CLIENT_MAX_FRAME_BYTES)
    except TypeError:
        return websockets.connect(url)


async def close_websocket(ws: Any, code: int | None = None, reason: str = "") -> None:
    """Close ``ws`` with ``code`` when the implementation takes one."""
    if ws is None:
        return
    try:
        if code is None:
            await ws.close()
        else:
            try:
                await ws.close(code=code, reason=reason)
            except TypeError:
                await ws.close()
    except Exception:  # noqa: BLE001 - closing a dead socket is best effort
        pass


class GatewayRPCError(Exception):
    """RPC failure returned by the AgentOS gateway."""

    def __init__(
        self,
        method: str,
        *,
        code: str | None = None,
        message: str = "RPC failed",
        data: dict[str, Any] | None = None,
    ) -> None:
        self.method = method
        self.code = code
        self.message = message
        self.data = data
        super().__init__(self.__str__())

    def __str__(self) -> str:
        code = f"{self.code}: " if self.code else ""
        return f"{self.method} failed: {code}{self.message}"


def normalize_gateway_url(url: str) -> str:
    """Normalize user-supplied gateway URLs to websocket endpoints."""

    stripped = url.strip()
    if "://" in stripped:
        parsed = urlparse(stripped)
        scheme = parsed.scheme
        netloc = parsed.netloc
        path = parsed.path
    else:
        parsed = urlparse(f"//{stripped}")
        scheme = "ws"
        netloc = parsed.netloc
        path = parsed.path
    params = parsed.params
    query = parsed.query
    fragment = parsed.fragment

    if scheme not in {"http", "https", "ws", "wss"}:
        raise ValueError(f"Unsupported gateway URL scheme: {scheme!r}")

    websocket_scheme = "wss" if scheme in {"https", "wss"} else "ws"
    websocket_path = "/ws" if path in ("", "/") else path
    return urlunparse((websocket_scheme, netloc, websocket_path, params, query, fragment))


class GatewayRPCClient:
    """Small async gateway client for non-CLI adapters.

    After a successful :meth:`connect` the client reconnects on its own when
    the socket drops or an event-sequence gap shows frames were lost
    (``auto_reconnect``): requests in flight fail with ``ConnectionError``,
    later :meth:`call`\\ s wait (up to ``request_timeout_s``) for the new
    connection, and every session subscription is renewed from its last
    ``stream_seq``. It gives up on a non-recoverable handshake answer (a
    wrong token, a protocol mismatch) and reports that error from then on.
    """

    def __init__(
        self,
        *,
        request_timeout_s: float | None = 30.0,
        auto_reconnect: bool = True,
        token: str | None = None,
        client_id: str = "agentos-python",
        client_mode: str = "backend",
        reconnect_initial_delay_s: float = RECONNECT_INITIAL_DELAY_S,
        reconnect_max_delay_s: float = RECONNECT_MAX_DELAY_S,
    ) -> None:
        self.request_timeout_s = request_timeout_s
        self.auto_reconnect = auto_reconnect
        self._token = token
        self._client_id = client_id
        self._client_mode = client_mode
        self._reconnect_initial_delay_s = reconnect_initial_delay_s
        self._reconnect_max_delay_s = reconnect_max_delay_s
        self._url: str | None = None
        self._ws: Any = None
        self._recv_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._listener_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._heartbeat_interval = 48.0
        self._connection_error: ConnectionError | None = None
        self._closing = False
        self._seq = EventSeqTracker()
        self._subscriptions = SessionSubscriptions()
        self._ready: asyncio.Event | None = None
        self.hello: dict[str, Any] | None = None
        #: Successful reconnects since :meth:`connect` (observability, tests).
        self.reconnects = 0

    async def connect(
        self, url: str = "ws://localhost:18791/ws", *, token: str | None = None
    ) -> None:
        if self._ws is not None or self._reconnect_task is not None:
            await self.close()
        self._closing = False
        self._connection_error = None
        if token is not None:
            self._token = token
        self._url = normalize_gateway_url(url)
        self._subscriptions = SessionSubscriptions()
        self.reconnects = 0
        await self._open()

    async def _open(self) -> None:
        """One connection attempt: socket, handshake, listener, heartbeat."""
        try:
            import websockets  # noqa: F401
        except ImportError as exc:  # pragma: no cover - dependency is part of base install.
            raise RuntimeError("websockets package is required") from exc

        assert self._url is not None
        ws = await open_websocket(self._url)
        try:
            raw = await ws.recv()
            challenge = json.loads(raw)
            if challenge.get("type") != "event" or challenge.get("event") != "connect.challenge":
                raise RuntimeError(f"Unexpected gateway handshake frame: {challenge}")

            req_id = str(uuid.uuid4())
            auth = {"token": self._token} if self._token else None
            params = build_connect_params(
                client_id=self._client_id, mode=self._client_mode, auth=auth
            )
            await ws.send(
                json.dumps({"type": "req", "id": req_id, "method": "connect", "params": params})
            )

            raw = await ws.recv()
            hello = json.loads(raw)
            if hello.get("type") == "res" and not hello.get("ok"):
                raise GatewayHandshakeError.from_response(hello)
            if hello.get("type") != "hello-ok":
                raise RuntimeError(f"Gateway handshake failed: {hello}")
        except BaseException:
            self._ws = None
            await close_websocket(ws)
            raise

        self._ws = ws
        self.hello = hello
        self._seq.reset()
        policy = hello.get("policy") if isinstance(hello.get("policy"), dict) else {}
        self._heartbeat_interval = _heartbeat_interval_from_policy(policy)
        self._listener_task = asyncio.create_task(self._listen(ws))
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(ws))
        self._ready_event().set()

    def _ready_event(self) -> asyncio.Event:
        if self._ready is None:
            self._ready = asyncio.Event()
        return self._ready

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if self._connection_error is not None:
            raise self._connection_error
        if self._ws is None:
            await self._await_reconnect(method)

        req_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[req_id] = fut
        try:
            await self._ws.send(
                json.dumps({"type": "req", "id": req_id, "method": method, "params": params})
            )
        except Exception as exc:
            self._pending.pop(req_id, None)
            err = self._connection_lost(exc)
            raise err from exc

        try:
            if self.request_timeout_s is None:
                res = await fut
            else:
                res = await asyncio.wait_for(fut, timeout=self.request_timeout_s)
        except TimeoutError as exc:
            self._pending.pop(req_id, None)
            raise TimeoutError(f"{method} timed out after {self.request_timeout_s:g}s") from exc
        if not res.get("ok"):
            err = res.get("error", {})
            details = err.get("details")
            if not isinstance(details, dict):
                details = err.get("data")
            raise GatewayRPCError(
                method,
                code=err.get("code"),
                message=err.get("message") or "RPC failed",
                data=details if isinstance(details, dict) else None,
            )
        payload = res.get("payload")
        self._subscriptions.note_call(method, params, payload)
        return {} if payload is None else payload

    async def _await_reconnect(self, method: str) -> None:
        task = self._reconnect_task
        if task is None or task.done():
            if self._connection_error is not None:
                raise self._connection_error
            raise ConnectionError("Gateway connection is not open")
        ready = self._ready_event()
        try:
            if self.request_timeout_s is None:
                await ready.wait()
            else:
                await asyncio.wait_for(ready.wait(), timeout=self.request_timeout_s)
        except TimeoutError as exc:
            raise TimeoutError(
                f"{method} timed out after {self.request_timeout_s:g}s waiting to reconnect"
            ) from exc
        if self._connection_error is not None:
            raise self._connection_error
        if self._ws is None:
            raise ConnectionError("Gateway connection is not open")

    async def list_sessions(self, limit: int = 50) -> dict[str, Any]:
        return cast(dict[str, Any], await self.call("sessions.list", {"limit": limit}))

    async def resolve_session(self, key: str) -> dict[str, Any]:
        return cast(dict[str, Any], await self.call("sessions.resolve", {"key": key}))

    async def session_history(self, session_key: str, limit: int = 1000) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await self.call("chat.history", {"sessionKey": session_key, "limit": limit}),
        )

    async def recv_event(self, timeout: float | None = None) -> dict[str, Any]:
        if timeout is None:
            return await self._recv_queue.get()
        return await asyncio.wait_for(self._recv_queue.get(), timeout=timeout)

    async def close(self) -> None:
        self._closing = True
        for task in (self._reconnect_task, self._heartbeat_task, self._listener_task):
            if task is None or task is asyncio.current_task():
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self._ws is not None:
            await self._ws.close()
        self._ws = None
        self._heartbeat_task = None
        self._listener_task = None
        self._reconnect_task = None

    async def _close_failed_connect(self) -> None:
        ws = self._ws
        self._ws = None
        if ws is not None:
            await ws.close()

    async def _listen(self, ws: Any = None) -> None:
        sock = self._ws if ws is None else ws
        lost: BaseException = ConnectionError("WebSocket connection closed")
        try:
            async for raw in sock:
                frame = json.loads(raw)
                frame_type = frame.get("type")
                if frame_type == "res":
                    fut = self._pending.pop(frame["id"], None)
                    if fut is not None and not fut.done():
                        fut.set_result(frame)
                elif frame_type == "event":
                    gap = self._seq.observe(frame)
                    if gap is not None:
                        lost = ConnectionError(
                            f"Event sequence gap: expected {gap[0]}, got {gap[1]}"
                        )
                        await close_websocket(sock, WS_CLOSE_SEQ_GAP, "seq gap")
                        break
                    self._subscriptions.note_event(frame)
                    await self._recv_queue.put(frame)
                elif frame_type == "pong":
                    continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            lost = exc
        if not self._closing:
            self._connection_lost(lost, ws=sock)

    async def _heartbeat_loop(self, ws: Any) -> None:
        try:
            while True:
                await asyncio.sleep(self._heartbeat_interval)
                if self._closing or ws is not self._ws:
                    return
                await ws.send('{"type":"ping"}')
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._connection_lost(exc, ws=ws)

    def _connection_lost(self, exc: BaseException, *, ws: Any = None) -> ConnectionError:
        """The socket is gone: reconnect when possible, else fail for good."""
        if ws is not None and ws is not self._ws:
            # A stale socket's listener or heartbeat finishing late.
            return ConnectionError(f"Gateway connection lost: {exc}")
        can_reconnect = (
            self.auto_reconnect
            and self._url is not None
            and not self._closing
            and self._connection_error is None
        )
        if not can_reconnect:
            return self._mark_connection_failed(exc)
        err = ConnectionError(f"Gateway connection lost (reconnecting): {exc}")
        self._fail_pending(err)
        self._stop_connection_tasks()
        self._ws = None
        self._ready_event().clear()
        if self._reconnect_task is None or self._reconnect_task.done():
            self._reconnect_task = asyncio.create_task(self._reconnect_loop())
        return err

    async def _reconnect_loop(self) -> None:
        delays = reconnect_delays(
            initial=self._reconnect_initial_delay_s, maximum=self._reconnect_max_delay_s
        )
        for delay in delays:
            await asyncio.sleep(delay)
            if self._closing:
                return
            try:
                await self._open()
            except asyncio.CancelledError:
                raise
            except GatewayHandshakeError as exc:
                if exc.recoverable:
                    continue
                self._mark_connection_failed(exc)
                self._ready_event().set()
                return
            except Exception:  # noqa: BLE001 - network errors: try again later
                continue
            self.reconnects += 1
            await self._resubscribe()
            return

    async def _resubscribe(self) -> None:
        for method, params in self._subscriptions.resubscribe_calls():
            try:
                await self.call(method, params)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the next gap or drop retries it
                pass

    def _fail_pending(self, err: BaseException) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(err)
        self._pending.clear()

    def _stop_connection_tasks(self) -> None:
        current_task = asyncio.current_task()
        for task in (self._heartbeat_task, self._listener_task):
            if task is not None and task is not current_task and not task.done():
                task.cancel()

    def _mark_connection_failed(self, exc: BaseException) -> ConnectionError:
        if isinstance(exc, ConnectionError):
            err = exc
        else:
            err = ConnectionError(f"Gateway connection lost: {exc}")
            err.__cause__ = exc
        if self._connection_error is None:
            self._connection_error = err
        else:
            err = self._connection_error
        self._fail_pending(err)
        self._stop_connection_tasks()
        return err


def _heartbeat_interval_from_policy(policy: dict[str, Any]) -> float:
    raw = policy.get("client_ws_keepalive_timeout_ms", 120_000)
    try:
        keepalive_ms = int(raw)
    except (TypeError, ValueError):
        keepalive_ms = 120_000
    keepalive_s = max(0, keepalive_ms) / 1000.0
    if keepalive_s <= 0.0:
        keepalive_s = 120.0
    minimum = 15.0 if keepalive_s > 15.0 else 0.05
    return max(minimum, keepalive_s * 0.4)
