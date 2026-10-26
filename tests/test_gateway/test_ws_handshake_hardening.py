"""The WebSocket handshake through the real ASGI app.

Covers the advertised payload limits (the pre-auth cap is enforced, the
values in ``hello-ok.policy`` are the enforced ones), validation of the
``connect`` envelope and params against the protocol models (lenient
``client`` block, strict types), the challenge nonce/timestamp and its echo,
protocol-mismatch details and the advertised capabilities.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from agentos.gateway.app import create_gateway_app
from agentos.gateway.config import GatewayConfig
from agentos.gateway.protocol import (
    CAPABILITIES,
    MAX_BUFFERED_BYTES,
    MAX_PAYLOAD_BYTES,
    MAX_PREAUTH_PAYLOAD_BYTES,
    MIN_PROTOCOL_VERSION,
    PROTOCOL_VERSION,
    ClientInfo,
    ConnectParams,
    ErrorDetailCode,
)
from agentos.gateway.websocket import get_registry

_CLIENT = {"id": "test-suite", "version": "1.0", "platform": "linux", "mode": "test"}


def _client(**config: Any) -> TestClient:
    return TestClient(create_gateway_app(GatewayConfig(**config)), client=("127.0.0.1", 50000))


def _connect(params: Any = None, *, req_id: Any = "c1", **extra: Any) -> dict[str, Any]:
    if params is None:
        params = {"minProtocol": 3, "maxProtocol": 3, "client": dict(_CLIENT)}
    return {"type": "req", "id": req_id, "method": "connect", "params": params, **extra}


def _refused(ws: Any) -> tuple[dict[str, Any], int | None]:
    """Read the error response and the close code that follows it."""
    res = ws.receive_json()
    with pytest.raises(WebSocketDisconnect) as exc_info:
        ws.receive_text()
    return res, exc_info.value.code


# ── challenge, nonce, hello-ok ─────────────────────────────────────────────


def test_challenge_carries_nonce_and_server_timestamp_and_hello_echoes_it() -> None:
    before = int(time.time() * 1000)
    with _client().websocket_connect("/ws") as ws:
        challenge = ws.receive_json()
        assert challenge["event"] == "connect.challenge"
        nonce = challenge["payload"]["nonce"]
        ts = challenge["payload"]["ts"]
        assert isinstance(nonce, str) and len(nonce) >= 16
        assert isinstance(ts, int) and before - 1000 <= ts <= int(time.time() * 1000) + 1000

        ws.send_json(_connect())
        hello = ws.receive_json()
        assert hello["type"] == "hello-ok"
        assert hello["auth"] == {"nonce": nonce}

        # The nonce stays on the connection object for a signed-connect flow.
        conn = get_registry().get(hello["server"]["conn_id"])
        assert conn is not None
        assert conn.challenge_nonce == nonce
        assert conn.client_info is not None and conn.client_info.id == "test-suite"


def test_hello_ok_advertises_sorted_capabilities() -> None:
    with _client().websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json(_connect())
        hello = ws.receive_json()
    capabilities = hello["features"]["capabilities"]
    assert capabilities == sorted(CAPABILITIES)
    assert {"typed-errors", "challenge-ts", "auth-rate-limit", "rpc-http"} <= set(capabilities)


def test_policy_advertises_the_enforced_limits() -> None:
    with _client().websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json(_connect())
        hello = ws.receive_json()
    assert hello["policy"]["max_payload"] == MAX_PAYLOAD_BYTES
    assert hello["policy"]["max_buffered_bytes"] == MAX_BUFFERED_BYTES


# ── pre-auth payload cap (#3676) ───────────────────────────────────────────


def test_oversized_preauth_frame_is_refused_with_1009() -> None:
    frame = _connect({"minProtocol": 3, "maxProtocol": 3, "pad": "x" * MAX_PREAUTH_PAYLOAD_BYTES})
    with _client().websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_text(json.dumps(frame))
        res, code = _refused(ws)
    assert res["ok"] is False
    assert res["error"]["code"] == "INVALID_REQUEST"
    assert res["error"]["details"] == {
        "code": ErrorDetailCode.PAYLOAD_TOO_LARGE.value,
        "limit_bytes": MAX_PREAUTH_PAYLOAD_BYTES,
    }
    assert code == 1009


def test_preauth_frame_just_under_the_cap_is_accepted() -> None:
    base = json.dumps(_connect({"minProtocol": 3, "maxProtocol": 3, "pad": ""}))
    pad = MAX_PREAUTH_PAYLOAD_BYTES - len(base.encode())
    raw = json.dumps(_connect({"minProtocol": 3, "maxProtocol": 3, "pad": "y" * pad}))
    assert len(raw.encode()) == MAX_PREAUTH_PAYLOAD_BYTES
    with _client().websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_text(raw)
        assert ws.receive_json()["type"] == "hello-ok"


def test_multibyte_frame_is_measured_in_utf8_bytes() -> None:
    # 22k three-byte characters: under the cap in characters, over it in bytes.
    frame = _connect({"minProtocol": 3, "maxProtocol": 3, "pad": "€" * 22_000})
    raw = json.dumps(frame, ensure_ascii=False)
    assert len(raw) < MAX_PREAUTH_PAYLOAD_BYTES < len(raw.encode())
    with _client().websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_text(raw)
        res, code = _refused(ws)
    assert res["error"]["details"]["code"] == "PAYLOAD_TOO_LARGE"
    assert code == 1009


def test_uvicorn_is_configured_with_the_post_auth_ceiling(monkeypatch, tmp_path) -> None:
    import asyncio

    from agentos.gateway import boot

    captured: dict[str, Any] = {}

    class _Config:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    class _Server:
        def __init__(self, config: Any) -> None:
            self.should_exit = False
            self.force_exit = False

        async def serve(self) -> None:
            while not self.should_exit:
                await asyncio.sleep(0.01)

    monkeypatch.setattr(boot.uvicorn, "Config", _Config)
    monkeypatch.setattr(boot.uvicorn, "Server", _Server)
    monkeypatch.setattr(boot, "emit_skill_filter_banner", lambda config: None)
    monkeypatch.setattr("agentos.gateway.pidlock.GatewayPidLock.acquire", lambda self: None)
    monkeypatch.setattr("agentos.gateway.pidlock.GatewayPidLock.release", lambda self: None)

    async def run() -> None:
        config = GatewayConfig(
            state_dir=str(tmp_path / "state"),
            workspace_dir=str(tmp_path / "workspace"),
            control_ui={"enabled": False},
            channels={"channels": []},
        )
        server = await boot.start_gateway_server(config=config, run=True)
        await server.close()

    asyncio.run(run())
    assert captured["ws_max_size"] == MAX_PAYLOAD_BYTES


# ── envelope and params validation (#3680) ─────────────────────────────────


@pytest.mark.parametrize(
    "client_block",
    [
        {"name": "agentos-web"},  # the shape the web console sent before
        dict(_CLIENT),
        {"id": "x", "displayName": "Mine", "instanceId": "i-1", "unknownField": [1, 2]},
        {},
    ],
)
def test_lenient_client_blocks_are_accepted(client_block: dict[str, Any]) -> None:
    with _client().websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json(_connect({"minProtocol": 3, "maxProtocol": 3, "client": client_block}))
        assert ws.receive_json()["type"] == "hello-ok"


def test_connect_without_params_or_client_still_works() -> None:
    with _client().websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "req", "id": "c1", "method": "connect"})
        assert ws.receive_json()["type"] == "hello-ok"


@pytest.mark.parametrize(
    ("params", "loc"),
    [
        ({"minProtocol": 3, "maxProtocol": 3, "client": {"id": 7}}, ["client", "id"]),
        ({"minProtocol": 3, "maxProtocol": 3, "client": {"name": ["a"]}}, ["client", "name"]),
        ({"minProtocol": "3", "maxProtocol": 3}, ["minProtocol"]),
        ({"minProtocol": 3, "maxProtocol": True}, ["maxProtocol"]),
        ({"clientKind": 1}, ["clientKind"]),
        ({"auth": {"token": 12345}}, ["auth"]),
        ({"auth": "secret"}, ["auth"]),
        ({"client": "agentos-web"}, ["client"]),
        ({"caps": "all"}, ["caps"]),
        ({"minProtocol": 3, "maxProtocol": 1}, []),
        (["minProtocol", 3], []),
    ],
)
def test_malformed_connect_params_are_rejected_with_typed_details(
    params: Any, loc: list[str]
) -> None:
    with _client().websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json(_connect(params))
        res, code = _refused(ws)
    assert res["id"] == "c1"
    assert res["error"]["code"] == "INVALID_REQUEST"
    details = res["error"]["details"]
    assert details["code"] == ErrorDetailCode.INVALID_CONNECT_PARAMS.value
    assert details["errors"], details
    if loc:
        assert any(error["loc"][: len(loc)] == loc for error in details["errors"]), details
    assert code == 1002


@pytest.mark.parametrize(
    "frame",
    [
        {"type": "req", "id": 1, "method": "connect", "params": {}},
        {"type": "req", "id": "x" * 500, "method": "connect", "params": {}},
    ],
)
def test_malformed_connect_envelope_is_rejected(frame: dict[str, Any]) -> None:
    with _client().websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json(frame)
        res, _code = _refused(ws)
    assert res["error"]["code"] == "INVALID_REQUEST"
    assert res["error"]["details"]["code"] == ErrorDetailCode.INVALID_FRAME.value


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        json.dumps(["req"]),
        json.dumps({"type": "req", "id": "c1", "method": "status"}),
        json.dumps({"type": "event", "event": "connect"}),
    ],
)
def test_first_frame_must_be_a_connect_request(raw: str) -> None:
    with _client().websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_text(raw)
        res, _code = _refused(ws)
    assert res["error"]["code"] == "INVALID_REQUEST"
    assert res["error"]["details"]["code"] == ErrorDetailCode.INVALID_FRAME.value


def _handshaken(ws: Any) -> None:
    ws.receive_json()
    ws.send_json(_connect())
    assert ws.receive_json()["type"] == "hello-ok"


@pytest.mark.parametrize(
    ("frame", "expected_id"),
    [
        ({"type": "req", "id": 5, "method": "status"}, ""),
        ({"type": "req", "id": "r1", "method": ""}, "r1"),
        ({"type": "req", "id": "r2"}, "r2"),
        ({"type": "req", "id": "r3", "method": 9}, "r3"),
    ],
)
def test_malformed_request_envelopes_are_answered_and_the_socket_stays_open(
    frame: dict[str, Any], expected_id: str
) -> None:
    with _client().websocket_connect("/ws") as ws:
        _handshaken(ws)
        ws.send_json(frame)
        res = ws.receive_json()
        assert res["id"] == expected_id
        assert res["error"]["code"] == "INVALID_REQUEST"
        assert res["error"]["details"]["code"] == ErrorDetailCode.INVALID_FRAME.value
        # Still usable after a malformed frame.
        ws.send_json({"type": "req", "id": "ok", "method": "health", "params": {}})
        res = ws.receive_json()
        assert (res["id"], res["ok"], res["payload"]["status"]) == ("ok", True, "ok")


def test_non_object_frame_after_handshake_is_invalid() -> None:
    with _client().websocket_connect("/ws") as ws:
        _handshaken(ws)
        ws.send_text("[1, 2]")
        res = ws.receive_json()
        assert res["error"]["code"] == "INVALID_REQUEST"
        assert res["error"]["details"]["code"] == "INVALID_FRAME"


def test_models_accept_name_alias_and_reject_wrong_types() -> None:
    assert ClientInfo.model_validate({"name": "agentos-web"}).id == "agentos-web"
    params = ConnectParams.model_validate(
        {"minProtocol": 3, "maxProtocol": 3, "clientKind": "node", "userAgent": "ua"}
    )
    assert (params.min_protocol, params.max_protocol, params.client_kind) == (3, 3, "node")
    assert params.user_agent == "ua"
    with pytest.raises(ValueError):
        ConnectParams.model_validate({"auth": {"agentToken": 1}})


# ── protocol mismatch (#3681) ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("client_min", "client_max", "reload_required"),
    [(1, 2, True), (PROTOCOL_VERSION + 1, PROTOCOL_VERSION + 2, False)],
)
def test_protocol_mismatch_returns_typed_details(
    client_min: int, client_max: int, reload_required: bool
) -> None:
    with _client().websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json(_connect({"minProtocol": client_min, "maxProtocol": client_max}))
        res, _code = _refused(ws)
    assert res["error"]["code"] == "INVALID_REQUEST"
    assert res["error"]["details"] == {
        "code": "PROTOCOL_MISMATCH",
        "client_min": client_min,
        "client_max": client_max,
        "server_min": MIN_PROTOCOL_VERSION,
        "server_max": PROTOCOL_VERSION,
        "reload_required": reload_required,
    }


def test_a_range_that_includes_the_server_version_negotiates_it() -> None:
    with _client().websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json(_connect({"minProtocol": 1, "maxProtocol": PROTOCOL_VERSION + 5}))
        hello = ws.receive_json()
    assert hello["protocol"] == PROTOCOL_VERSION
