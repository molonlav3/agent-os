"""Supervisor contract (#3682): /ready, /startupz, startup handshake, shutdown intent."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from agentos.gateway.app import create_gateway_app
from agentos.gateway.boot import GatewayServer
from agentos.gateway.config import GatewayConfig
from agentos.gateway.readiness import (
    EX_CONFIG,
    MIN_RESTART_EXPECTED_MS,
    SIDECAR_CHANNELS,
    SIDECAR_ROUTER,
    STARTUP_SIDECARS,
    WS_CLOSE_TRY_AGAIN_LATER,
    GatewayReadiness,
    drain_budget_ms,
)
from agentos.gateway.websocket import get_registry


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _app(readiness: GatewayReadiness | None, *, ready_flag: bool | None = None) -> Any:
    app = create_gateway_app(GatewayConfig())
    if readiness is not None:
        app.state.readiness = readiness
    if ready_flag is not None:
        app.state.gateway_ready = ready_flag
    return app


def _client(app: Any) -> TestClient:
    # Loopback Host: the DNS-rebinding guard rejects the default "testserver".
    return TestClient(app, base_url="http://localhost")


def test_ex_config_is_sysexits_value() -> None:
    assert EX_CONFIG == 78


def test_ready_reports_starting_with_pending_sidecars_then_ready() -> None:
    readiness = GatewayReadiness(clock=_Clock())
    readiness.begin_sidecar(SIDECAR_CHANNELS)
    client = _client(_app(readiness, ready_flag=False))

    starting = client.get("/ready")
    assert starting.status_code == 503
    body = starting.json()
    assert body["ready"] is False
    assert body["state"] == "starting"
    assert body["status"] == "starting"
    assert body["failing"] == ["channels"]
    assert body["drain_budget_ms"] == drain_budget_ms() == 32_000

    readiness.settle_sidecar(SIDECAR_CHANNELS)
    readiness.mark_serving()
    client.app.state.gateway_ready = True
    ready = client.get("/ready")
    assert ready.status_code == 200
    assert ready.json()["state"] == "ready"
    assert ready.json()["failing"] == []
    assert isinstance(ready.json()["uptime_ms"], int)


def test_ready_reports_draining_for_close_and_for_the_restart_flag() -> None:
    readiness = GatewayReadiness(clock=_Clock())
    readiness.mark_serving()
    app = _app(readiness, ready_flag=True)
    client = _client(app)
    assert client.get("/ready").status_code == 200

    # The in-place restart stops admission by flipping the legacy flag.
    app.state.gateway_ready = False
    draining = client.get("/ready")
    assert draining.status_code == 503
    assert draining.json()["state"] == "draining"
    assert draining.json()["failing"] == ["draining"]

    app.state.gateway_ready = True
    readiness.begin_drain()
    assert client.get("/ready").json()["state"] == "draining"


def test_ready_reports_safe_mode_as_ready_with_the_reason() -> None:
    readiness = GatewayReadiness(clock=_Clock())
    readiness.attach_safe_mode(
        lambda: {"active": True, "reason": "3 unclean gateway boots in the last 300s"}
    )
    readiness.mark_serving()
    client = _client(_app(readiness, ready_flag=True))

    resp = client.get("/ready")
    # 200: the control plane is up so the operator can repair what tripped it,
    # and ``gateway start`` must not kill a gateway that is only in safe mode.
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is True
    assert body["state"] == "safe_mode"
    assert body["failing"] == ["safe_mode"]
    assert body["safe_mode"]["reason"].startswith("3 unclean gateway boots")


def test_ready_names_down_channels_without_becoming_unready() -> None:
    readiness = GatewayReadiness(clock=_Clock())
    readiness.mark_serving()
    down = ["tg"]
    readiness.attach_failing_channels(lambda: list(down))
    client = _client(_app(readiness, ready_flag=True))
    resp = client.get("/ready")
    assert resp.status_code == 200
    assert resp.json()["failing"] == ["channel:tg"]
    down.clear()  # the health sweep brought it back
    assert client.get("/ready").json()["failing"] == []


def test_startupz_waits_for_every_boot_sidecar() -> None:
    readiness = GatewayReadiness(clock=_Clock())
    readiness.begin_sidecar(SIDECAR_CHANNELS)
    client = _client(_app(readiness, ready_flag=False))
    assert client.get("/startupz").status_code == 503

    readiness.settle_sidecar(SIDECAR_CHANNELS)
    readiness.begin_sidecar(SIDECAR_ROUTER)
    readiness.mark_serving()
    client.app.state.gateway_ready = True
    # Ready to serve, but the router preload has not settled yet.
    assert client.get("/ready").status_code == 200
    pending = client.get("/startupz")
    assert pending.status_code == 503
    assert pending.json()["pending"] == ["router"]

    readiness.settle_sidecar(SIDECAR_ROUTER, error="RuntimeError: no bundle")
    done = client.get("/startupz")
    assert done.status_code == 200
    assert done.json()["started"] is True
    assert client.get("/ready").json()["startup"]["failed"] == {"router": "RuntimeError: no bundle"}


def test_apps_without_a_readiness_object_keep_the_old_meaning() -> None:
    client = _client(_app(None))
    assert client.get("/ready").status_code == 200
    assert client.get("/ready").json()["state"] == "ready"
    assert client.get("/startupz").status_code == 200
    client.app.state.gateway_ready = False
    assert client.get("/ready").status_code == 503
    assert client.get("/startupz").status_code == 503


def test_startupz_is_public_under_token_auth() -> None:
    config = GatewayConfig()
    config.auth = config.auth.model_copy(update={"mode": "token", "token": "secret-token"})
    client = _client(create_gateway_app(config))
    assert client.get("/startupz").status_code == 200
    assert client.get("/api/config").status_code in (401, 403)


def test_restart_estimate_comes_from_this_boot() -> None:
    clock = _Clock()
    readiness = GatewayReadiness(clock=clock)
    assert readiness.restart_expected_ms() == MIN_RESTART_EXPECTED_MS
    clock.now += 7.5
    readiness.mark_serving()
    assert readiness.startup_duration_ms() == 7500
    assert readiness.restart_expected_ms() == 9500


def _connect_frame() -> dict[str, Any]:
    return {"type": "req", "id": "c1", "method": "connect", "params": {}}


def test_ws_connect_while_starting_is_told_to_retry_and_closed_1013() -> None:
    readiness = GatewayReadiness(clock=_Clock())
    readiness.begin_sidecar(SIDECAR_CHANNELS)
    client = _client(_app(readiness, ready_flag=False))

    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["event"] == "connect.challenge"
        ws.send_json(_connect_frame())
        res = ws.receive_json()
        assert res["type"] == "res"
        assert res["id"] == "c1"
        assert res["ok"] is False
        error = res["error"]
        assert error["code"] == "UNAVAILABLE"
        assert error["retryable"] is True
        assert error["retry_after_ms"] == 1000
        assert error["details"]["code"] == STARTUP_SIDECARS == "STARTUP_SIDECARS"
        assert error["details"]["retry_after_ms"] == 1000
        assert error["details"]["pending"] == ["channels"]
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    assert closed.value.code == WS_CLOSE_TRY_AGAIN_LATER == 1013
    assert closed.value.reason == "gateway starting"


def test_ws_connect_after_boot_is_not_refused() -> None:
    readiness = GatewayReadiness(clock=_Clock())
    readiness.mark_serving()
    config = GatewayConfig()
    config.auth = config.auth.model_copy(update={"mode": "token", "token": "secret-token"})
    app = create_gateway_app(config)
    app.state.readiness = readiness
    app.state.gateway_ready = True
    client = _client(app)

    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["event"] == "connect.challenge"
        frame = _connect_frame()
        frame["params"] = {"auth": {"token": "secret-token"}}
        ws.send_json(frame)
        hello = ws.receive_json()
        assert hello["type"] == "hello-ok"


class _RecordingConn:
    def __init__(self) -> None:
        self.conn_id = "rec"
        self.authenticated = True
        self.events: list[tuple[str, Any]] = []
        self.closed = False

    async def send_event(self, event: str, payload: Any = None) -> None:
        self.events.append((event, payload))

    async def close(self, *args: Any, **kwargs: Any) -> None:
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("expected", [None, 4500])
async def test_shutdown_event_carries_restart_intent(expected: int | None) -> None:
    conn = _RecordingConn()
    registry = get_registry()
    registry.register(conn)  # type: ignore[arg-type]
    readiness = GatewayReadiness(clock=_Clock())
    readiness.mark_serving()
    server = GatewayServer(app=create_gateway_app(GatewayConfig()), config=GatewayConfig())
    server.readiness = readiness
    try:
        await server.close("restart:safe" if expected else "shutdown", restart_expected_ms=expected)
    finally:
        registry.unregister(conn.conn_id)

    assert conn.events == [
        (
            "shutdown",
            {
                "reason": "restart:safe" if expected else "shutdown",
                "restart_expected_ms": expected,
            },
        )
    ]
    assert conn.closed is True
    assert readiness.phase == "draining"
    assert readiness.state() == "draining"


class _WireRecorder:
    """Starlette-shaped socket: refuses ``send`` once a close has been sent."""

    def __init__(self) -> None:
        from starlette.websockets import WebSocketState

        self.client_state = WebSocketState.CONNECTED
        self.sent: list[dict[str, Any]] = []
        self.close_codes: list[int] = []

    async def send_text(self, text: str) -> None:
        import json

        if self.close_codes:
            raise RuntimeError('Cannot call "send" once a close message has been sent.')
        self.sent.append(json.loads(text))

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.close_codes.append(code)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "close() broadcasts `shutdown` into each connection's writer queue and "
        "closes the socket 1012 before the writer has flushed it, so the frame "
        "is dropped. The flush-before-close ordering lands with the gateway "
        "hardening branch; drop this marker once this branch is rebased on it."
    ),
)
@pytest.mark.asyncio
async def test_shutdown_frame_reaches_a_client_through_its_writer_queue() -> None:
    from agentos.gateway.access import ConnectionSurface
    from agentos.gateway.auth import AccessContext
    from agentos.gateway.websocket import WsConnection

    wire = _WireRecorder()
    conn = WsConnection(conn_id="writer-conn", ws=wire)  # type: ignore[arg-type]
    conn.access = AccessContext(
        surface=ConnectionSurface.CONTROL, admitted=True, credential_verified=True
    )
    conn._start_writer(maxsize=64, enabled=True)
    registry = get_registry()
    registry.register(conn)
    readiness = GatewayReadiness(clock=_Clock())
    readiness.mark_serving()
    server = GatewayServer(app=create_gateway_app(GatewayConfig()), config=GatewayConfig())
    server.readiness = readiness
    try:
        await server.close("restart:safe", restart_expected_ms=4500)
        await asyncio.sleep(0.05)  # give the writer every chance to run
    finally:
        registry.unregister(conn.conn_id)
        await conn._stop_writer()

    assert wire.close_codes == [1012]
    shutdown = [f for f in wire.sent if f.get("event") == "shutdown"]
    assert shutdown and shutdown[0]["payload"] == {
        "reason": "restart:safe",
        "restart_expected_ms": 4500,
    }
