"""``POST /api/v1/rpc``: allowlisted RPC over HTTP behind the bearer token."""

from __future__ import annotations

from typing import Any

import pytest
from starlette.testclient import TestClient

from agentos.gateway.app import create_gateway_app
from agentos.gateway.config import AuthConfig, GatewayConfig
from agentos.gateway.http_rpc import HTTP_RPC_METHODS
from agentos.gateway.rpc import get_dispatcher

_TOKEN = "an-http-rpc-test-token-0123456789abcdef"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}


def _client(mode: str = "token") -> TestClient:
    config = GatewayConfig(auth=AuthConfig(mode=mode, token=_TOKEN if mode == "token" else None))
    return TestClient(
        create_gateway_app(config), client=("127.0.0.1", 50000), base_url="http://127.0.0.1"
    )


def _rpc(client: TestClient, body: Any, headers: dict[str, str] | None = None) -> Any:
    return client.post("/api/v1/rpc", json=body, headers=_AUTH if headers is None else headers)


def test_allowlisted_method_answers_like_a_res_frame() -> None:
    res = _rpc(_client(), {"id": "a1", "method": "health", "params": {}})
    assert res.status_code == 200
    body = res.json()
    assert body["id"] == "a1"
    assert body["ok"] is True
    assert body["payload"]["status"] == "ok"
    assert "type" not in body


def test_id_is_optional_and_may_be_an_integer() -> None:
    client = _client()
    assert _rpc(client, {"method": "status"}).json()["id"] is None
    body = _rpc(client, {"id": 7, "method": "status"}).json()
    assert (body["id"], body["ok"]) == (7, True)
    assert body["payload"]["status"] == "running"


@pytest.mark.parametrize(
    "method",
    [
        "config.apply",
        "config.patch",
        "config.set",
        "secrets.resolve",
        "env.set",
        "env.reveal",
        "gateway.restart",
        "trading.swap",
        "wallet.export",
        "sessions.delete",
        "exec.approval.resolve",
        "no.such.method",
    ],
)
def test_methods_off_the_allowlist_are_forbidden(method: str) -> None:
    res = _rpc(_client(), {"id": "x", "method": method, "params": {}})
    assert res.status_code == 403
    body = res.json()
    assert body["ok"] is False
    assert body["id"] == "x"
    assert body["error"]["code"] == "FORBIDDEN"
    assert body["error"]["details"] == {"code": "METHOD_NOT_ALLOWED_OVER_HTTP", "method": method}


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}])
def test_the_bearer_token_is_required(headers: dict[str, str]) -> None:
    res = _rpc(_client(), {"method": "health"}, headers=headers)
    assert res.status_code == 401


def test_loopback_without_auth_works_like_the_other_routes() -> None:
    res = _rpc(_client(mode="none"), {"method": "health"}, headers={})
    assert res.status_code == 200
    assert res.json()["ok"] is True


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ("not an object", 400),
        ({"params": {}}, 400),
        ({"method": ""}, 400),
        ({"method": 5}, 400),
        ({"method": "health", "params": [1]}, 400),
        ({"method": "health", "id": {"nested": True}}, 400),
        ({"method": "health", "id": True}, 400),
    ],
)
def test_malformed_bodies_are_invalid_requests(body: Any, status: int) -> None:
    res = _rpc(_client(), body)
    assert res.status_code == status
    error = res.json()["error"]
    assert error["code"] == "INVALID_REQUEST"
    assert error["details"]["code"] == "INVALID_FRAME"


def test_invalid_json_is_an_invalid_request() -> None:
    res = _client().post(
        "/api/v1/rpc", content=b"{nope", headers={**_AUTH, "content-type": "application/json"}
    )
    assert res.status_code == 400
    assert res.json()["error"]["details"]["code"] == "INVALID_FRAME"


def test_handler_errors_keep_their_code_and_map_to_a_status() -> None:
    # No session manager in this bare app: the capability is unavailable.
    res = _rpc(_client(), {"id": "s", "method": "sessions.get", "params": {"key": "k"}})
    assert res.status_code == 503
    body = res.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "UNAVAILABLE"
    assert body["error"]["retryable"] is True


def test_oversized_body_is_refused_before_it_is_read() -> None:
    res = _client().post(
        "/api/v1/rpc",
        content=b"{}",
        headers={**_AUTH, "content-type": "application/json", "content-length": "999999999"},
    )
    assert res.status_code == 413
    assert res.json()["error"]["details"]["code"] == "PAYLOAD_TOO_LARGE"


def test_every_allowlisted_method_is_registered() -> None:
    registered = set(get_dispatcher().list_methods())
    assert HTTP_RPC_METHODS <= registered, sorted(HTTP_RPC_METHODS - registered)


def test_allowlist_excludes_destructive_and_privileged_families() -> None:
    forbidden_prefixes = (
        "config.apply",
        "config.patch",
        "config.set",
        "secrets.",
        "env.",
        "trading.",
        "wallet.",
        "gateway.restart",
        "updates.apply",
        "mcp.connect",
        "skills.install",
        "skills.uninstall",
    )
    offenders = sorted(
        method
        for method in HTTP_RPC_METHODS
        if method.startswith(forbidden_prefixes)
        or method.endswith((".delete", ".remove", ".reset", ".truncate"))
        or "approval.resolve" in method
    )
    assert offenders == []
