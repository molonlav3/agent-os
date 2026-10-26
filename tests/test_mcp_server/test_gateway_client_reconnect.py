"""Python gateway clients reconnect, detect event-sequence gaps, and stop when
retrying cannot help. Driven by a small fake gateway on a loopback port."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
import websockets
from websockets.asyncio.server import ServerConnection, serve

from agentos.cli.gateway_client import GatewayClient
from agentos.gateway.protocol import PROTOCOL_VERSION
from agentos.gateway_client import (
    CLIENT_PROTOCOL_VERSION,
    WS_CLOSE_SEQ_GAP,
    EventSeqTracker,
    GatewayHandshakeError,
    GatewayRPCClient,
    reconnect_delays,
)


class _FakeGateway:
    def __init__(self) -> None:
        self.connections: list[ServerConnection] = []
        self.connect_params: list[dict[str, Any]] = []
        self.calls: list[tuple[str, Any]] = []
        self.close_codes: list[int | None] = []
        self.handshake_error: dict[str, Any] | None = None
        #: Methods the fake receives but never answers.
        self.hold_methods: set[str] = set()
        self.connected = asyncio.Event()
        self._server: Any = None
        self.url = ""

    async def __aenter__(self) -> _FakeGateway:
        self._server = await serve(self._handler, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}/ws"
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._server.close()
        await self._server.wait_closed()

    async def _handler(self, ws: ServerConnection) -> None:
        await ws.send(
            json.dumps({"type": "event", "event": "connect.challenge", "payload": {"nonce": "n"}})
        )
        req = json.loads(await ws.recv())
        self.connect_params.append(req["params"])
        if self.handshake_error is not None:
            await ws.send(
                json.dumps(
                    {"type": "res", "id": req["id"], "ok": False, "error": self.handshake_error}
                )
            )
            await ws.close(1008)
            return
        await ws.send(
            json.dumps(
                {
                    "type": "hello-ok",
                    "protocol": PROTOCOL_VERSION,
                    "server": {"version": "test", "conn_id": f"c{len(self.connections)}"},
                    "features": {"methods": [], "events": [], "capabilities": []},
                    "snapshot": {},
                    "policy": {"client_ws_keepalive_timeout_ms": 120_000},
                    "auth": {"nonce": "n"},
                }
            )
        )
        self.connections.append(ws)
        self.connected.set()
        try:
            async for raw in ws:
                frame = json.loads(raw)
                if frame.get("type") != "req":
                    continue
                method, params = frame["method"], frame.get("params")
                self.calls.append((method, params))
                if method in self.hold_methods:
                    continue
                payload: dict[str, Any] = {"echo": method}
                if method == "sessions.messages.subscribe":
                    payload = {"key": params["key"], "current_stream_seq": 5}
                await ws.send(
                    json.dumps({"type": "res", "id": frame["id"], "ok": True, "payload": payload})
                )
        except websockets.ConnectionClosed:
            pass
        finally:
            self.close_codes.append(ws.close_code)

    async def wait_connections(self, count: int, timeout: float = 5.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while len(self.connections) < count:
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(f"only {len(self.connections)} connections")
            await asyncio.sleep(0.01)

    async def event(self, index: int, seq: int, payload: dict[str, Any]) -> None:
        frame = {"type": "event", "event": "session.event.text_delta", "payload": payload}
        frame["seq"] = seq
        await self.connections[index].send(json.dumps(frame))


async def _until(predicate: Any, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met")
        await asyncio.sleep(0.01)


def _rpc_client() -> GatewayRPCClient:
    return GatewayRPCClient(request_timeout_s=5.0, reconnect_initial_delay_s=0.05)


def _cli_client() -> GatewayClient:
    client = GatewayClient()
    client.reconnect_initial_delay_s = 0.05
    return client


# ── connect params ─────────────────────────────────────────────────────────


def test_client_protocol_version_is_the_gateways() -> None:
    assert CLIENT_PROTOCOL_VERSION == PROTOCOL_VERSION


@pytest.mark.parametrize(
    ("factory", "client_id", "mode"),
    [(_rpc_client, "agentos-python", "backend"), (_cli_client, "agentos-cli", "cli")],
)
async def test_connect_sends_an_honest_range_and_a_client_block(
    factory: Any, client_id: str, mode: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.setenv("AGENTOS_OPERATOR_SECRET_FILE", str(tmp_path / "absent"))
    from agentos import __version__

    async with _FakeGateway() as gateway:
        client = factory()
        await client.connect(gateway.url)
        await client.close()
    params = gateway.connect_params[0]
    assert params["minProtocol"] == params["maxProtocol"] == PROTOCOL_VERSION
    assert params["client"]["id"] == client_id
    assert params["client"]["mode"] == mode
    assert params["client"]["version"] == __version__
    assert isinstance(params["client"]["platform"], str) and params["client"]["platform"]


def test_reconnect_backoff_doubles_to_thirty_seconds() -> None:
    delays = reconnect_delays()
    assert [next(delays) for _ in range(8)] == [1, 2, 4, 8, 16, 30, 30, 30]


def test_seq_tracker() -> None:
    tracker = EventSeqTracker()
    assert tracker.observe({"seq": 1}) is None
    assert tracker.observe({"seq": 2}) is None
    assert tracker.observe({"no": "seq"}) is None
    assert tracker.observe({"seq": 4}) == (3, 4)
    tracker.reset()
    assert tracker.observe({"seq": 9}) is None


# ── reconnect ──────────────────────────────────────────────────────────────


async def test_rpc_client_reconnects_and_resubscribes_from_the_last_stream_seq() -> None:
    async with _FakeGateway() as gateway:
        client = _rpc_client()
        await client.connect(gateway.url)
        try:
            await client.call("sessions.messages.subscribe", {"key": "agent:main:x"})
            await gateway.event(0, 1, {"session_key": "agent:main:x", "stream_seq": 7})
            await client.recv_event(timeout=2.0)

            await gateway.connections[0].close(1012)
            await gateway.wait_connections(2)
            await _until(lambda: client.reconnects == 1)

            assert (
                "sessions.messages.subscribe",
                {"key": "agent:main:x", "since_stream_seq": 7},
            ) in (gateway.calls)
            assert await client.call("status") == {"echo": "status"}
            # Seq numbering restarts on the new connection.
            await gateway.event(1, 1, {"session_key": "agent:main:x", "stream_seq": 8})
            assert (await client.recv_event(timeout=2.0))["seq"] == 1
        finally:
            await client.close()


async def test_a_call_during_the_reconnect_waits_for_it() -> None:
    async with _FakeGateway() as gateway:
        client = _rpc_client()
        await client.connect(gateway.url)
        try:
            await gateway.connections[0].close(1012)
            await _until(lambda: client._ws is None)  # noqa: SLF001
            assert await client.call("health") == {"echo": "health"}
            assert len(gateway.connections) == 2
        finally:
            await client.close()


async def test_seq_gap_closes_4000_and_reconnects() -> None:
    async with _FakeGateway() as gateway:
        client = _rpc_client()
        await client.connect(gateway.url)
        try:
            await gateway.event(0, 1, {"n": 1})
            await gateway.event(0, 2, {"n": 2})
            await gateway.event(0, 4, {"n": 4})
            await gateway.wait_connections(2)
            await _until(lambda: gateway.close_codes)
            assert gateway.close_codes[0] == WS_CLOSE_SEQ_GAP
            received = [await client.recv_event(timeout=2.0) for _ in range(2)]
            assert [frame["payload"]["n"] for frame in received] == [1, 2]
            with pytest.raises(TimeoutError):
                await client.recv_event(timeout=0.1)
        finally:
            await client.close()


async def test_requests_in_flight_fail_when_the_socket_drops() -> None:
    async with _FakeGateway() as gateway:
        gateway.hold_methods.add("chat.history")
        client = _rpc_client()
        await client.connect(gateway.url)
        try:
            pending = asyncio.create_task(client.call("chat.history", {"sessionKey": "s"}))
            await _until(lambda: gateway.calls)
            await gateway.connections[0].close(1012)
            with pytest.raises(ConnectionError, match="reconnecting"):
                await pending
        finally:
            await client.close()


async def test_bad_token_on_reconnect_stops_for_good() -> None:
    async with _FakeGateway() as gateway:
        client = _rpc_client()
        await client.connect(gateway.url)
        try:
            gateway.handshake_error = {"code": "UNAUTHORIZED", "message": "Authentication failed"}
            await gateway.connections[0].close(1012)
            await _until(lambda: client._connection_error is not None)  # noqa: SLF001
            with pytest.raises(ConnectionError, match="UNAUTHORIZED"):
                await client.call("status")
            await asyncio.sleep(0.3)
            assert len(gateway.connect_params) == 2  # one refused attempt, no loop
        finally:
            await client.close()


async def test_protocol_mismatch_at_connect_is_not_recoverable() -> None:
    async with _FakeGateway() as gateway:
        gateway.handshake_error = {
            "code": "INVALID_REQUEST",
            "message": "Unsupported protocol version range",
            "details": {"code": "PROTOCOL_MISMATCH", "server_min": 4, "server_max": 4},
        }
        client = _rpc_client()
        with pytest.raises(GatewayHandshakeError) as exc_info:
            await client.connect(gateway.url)
    assert exc_info.value.detail_code == "PROTOCOL_MISMATCH"
    assert exc_info.value.recoverable is False


async def test_without_auto_reconnect_a_drop_is_final() -> None:
    async with _FakeGateway() as gateway:
        client = GatewayRPCClient(auto_reconnect=False)
        await client.connect(gateway.url)
        await gateway.connections[0].close(1012)
        await _until(lambda: client._connection_error is not None)  # noqa: SLF001
        with pytest.raises(ConnectionError):
            await client.call("status")
        await client.close()
    assert len(gateway.connect_params) == 1


# ── the CLI client ─────────────────────────────────────────────────────────


async def test_cli_client_reconnects_after_a_drop_and_after_a_gap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.setenv("AGENTOS_OPERATOR_SECRET_FILE", str(tmp_path / "absent"))
    async with _FakeGateway() as gateway:
        client = _cli_client()
        await client.connect(gateway.url)
        try:
            await client.call("sessions.messages.subscribe", {"key": "agent:main:cli"})
            await gateway.connections[0].close(1012)
            await gateway.wait_connections(2)
            await _until(lambda: client.reconnects == 1)
            assert (
                "sessions.messages.subscribe",
                {"key": "agent:main:cli", "since_stream_seq": 5},
            ) in gateway.calls
            assert await client.call("status") == {"echo": "status"}

            await gateway.event(1, 1, {"n": 1})
            await gateway.event(1, 3, {"n": 3})
            await gateway.wait_connections(3)
            await _until(lambda: len(gateway.close_codes) >= 2)
            assert gateway.close_codes[1] == WS_CLOSE_SEQ_GAP
            await _until(lambda: client.reconnects == 2)
        finally:
            await client.close()


async def test_cli_client_bad_token_still_exits_with_the_handshake_frame(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.setenv("AGENTOS_OPERATOR_SECRET_FILE", str(tmp_path / "absent"))
    async with _FakeGateway() as gateway:
        gateway.handshake_error = {"code": "UNAUTHORIZED", "message": "Authentication failed"}
        with pytest.raises(SystemExit, match="Handshake failed.*UNAUTHORIZED"):
            await _cli_client().connect(gateway.url, token="wrong")


async def test_cli_client_gives_up_after_its_reconnect_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A gateway that is gone for good must not leave `agentos chat` waiting."""
    monkeypatch.setenv("AGENTOS_OPERATOR_SECRET_FILE", str(tmp_path / "absent"))
    statuses: list[str] = []
    client = _cli_client()
    client.reconnect_budget_s = 0.4
    client.on_connection_status = lambda state, _text: statuses.append(state)
    async with _FakeGateway() as gateway:
        await client.connect(gateway.url)
    # The fake gateway is shut down: every reconnect attempt is refused.
    try:
        started = asyncio.get_running_loop().time()
        with pytest.raises(ConnectionError, match="restart chat or reconnect"):
            await client.call("status")
        assert asyncio.get_running_loop().time() - started < 3.0
        assert statuses == ["reconnecting", "lost"]
    finally:
        await client.close()


async def test_cli_client_reports_a_recovered_outage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.setenv("AGENTOS_OPERATOR_SECRET_FILE", str(tmp_path / "absent"))
    statuses: list[str] = []
    async with _FakeGateway() as gateway:
        client = _cli_client()
        client.on_connection_status = lambda state, _text: statuses.append(state)
        await client.connect(gateway.url)
        try:
            await gateway.connections[0].close(1012)
            await _until(lambda: client.reconnects == 1)
            assert statuses == ["reconnecting", "reconnected"]
        finally:
            await client.close()


# ── a send lost with the socket ─────────────────────────────────────────────


@pytest.mark.parametrize("which", ["rpc", "cli"])
async def test_a_send_lost_with_the_socket_is_resent_once_with_the_same_key(which: str) -> None:
    """The gateway dedupes on the key: the resend returns the turn it started."""
    async with _FakeGateway() as gateway:
        gateway.hold_methods.add("sessions.send")
        client: Any = _rpc_client() if which == "rpc" else _cli_client()
        await client.connect(gateway.url)
        try:
            params = {"key": "agent:main:main", "message": "hi", "idempotency_key": "k-1"}
            send = (
                client.call_idempotent("sessions.send", dict(params))
                if which == "rpc"
                else client._call_idempotent("sessions.send", dict(params))
            )
            pending = asyncio.create_task(send)
            await _until(lambda: any(m == "sessions.send" for m, _ in gateway.calls))
            gateway.hold_methods.discard("sessions.send")
            await gateway.connections[0].close(1012)

            result = await asyncio.wait_for(pending, timeout=5.0)
        finally:
            await client.close()

    sends = [p for m, p in gateway.calls if m == "sessions.send"]
    assert len(sends) == 2
    assert sends[0]["idempotency_key"] == sends[1]["idempotency_key"] == "k-1"
    assert result == {"echo": "sessions.send"}


async def test_call_idempotent_mints_a_key_when_the_caller_has_none() -> None:
    async with _FakeGateway() as gateway:
        client = _rpc_client()
        await client.connect(gateway.url)
        try:
            await client.call_idempotent("sessions.send", {"key": "agent:main:main"})
        finally:
            await client.close()

    sends = [p for m, p in gateway.calls if m == "sessions.send"]
    assert isinstance(sends[0]["idempotency_key"], str) and sends[0]["idempotency_key"]
