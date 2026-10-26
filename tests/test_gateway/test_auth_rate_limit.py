"""Failed-authentication rate limiting and the pre-auth socket budget."""

from __future__ import annotations

from typing import Any

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from agentos.gateway.app import create_gateway_app
from agentos.gateway.auth_rate_limit import (
    LOOPBACK_DELAY_MAX_S,
    LOOPBACK_DELAY_START_S,
    MAX_PREAUTH_SOCKETS_PER_IP,
    AuthRateLimiter,
    PreauthBudget,
)
from agentos.gateway.config import AuthConfig, AuthRateLimitConfig, GatewayConfig

_TOKEN = "correct-horse-battery-staple-0123456789"
_REMOTE = "203.0.113.7"


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _config(*, host: str = "127.0.0.1", **rate_limit: Any) -> GatewayConfig:
    return GatewayConfig(
        host=host,
        auth=AuthConfig(mode="token", token=_TOKEN, rate_limit=AuthRateLimitConfig(**rate_limit)),
    )


def _limiter(**rate_limit: Any) -> tuple[AuthRateLimiter, _Clock]:
    clock = _Clock()
    return AuthRateLimiter(_config(**rate_limit), clock=clock), clock


# ── the counter ────────────────────────────────────────────────────────────


def test_ten_failures_in_the_window_lock_the_key_out() -> None:
    limiter, clock = _limiter()
    for _ in range(9):
        assert limiter.record_failure(_REMOTE).allowed
        assert limiter.check(_REMOTE).allowed
    decision = limiter.record_failure(_REMOTE)
    assert decision.allowed is False
    assert decision.retry_after_ms == 300_000

    blocked = limiter.check(_REMOTE)
    assert blocked.allowed is False
    assert 0 < blocked.retry_after_ms <= 300_000

    clock.now += 299
    assert limiter.check(_REMOTE).allowed is False
    clock.now += 2
    assert limiter.check(_REMOTE).allowed is True


def test_failures_outside_the_window_do_not_accumulate() -> None:
    limiter, clock = _limiter()
    for _ in range(9):
        limiter.record_failure(_REMOTE)
    clock.now += 61
    assert limiter.record_failure(_REMOTE).allowed
    assert limiter.check(_REMOTE).allowed


def test_success_clears_the_history() -> None:
    limiter, _clock = _limiter()
    for _ in range(9):
        limiter.record_failure(_REMOTE)
    limiter.record_success(_REMOTE)
    for _ in range(9):
        assert limiter.record_failure(_REMOTE).allowed


def test_keys_are_per_client_and_scope() -> None:
    limiter, _clock = _limiter(max_failures=2)
    limiter.record_failure(_REMOTE)
    limiter.record_failure(_REMOTE)
    assert limiter.check(_REMOTE).allowed is False
    assert limiter.check("198.51.100.1").allowed is True
    assert limiter.check(_REMOTE, "other-scope").allowed is True


def test_loopback_is_never_locked_out_but_answers_slower_and_slower() -> None:
    limiter, _clock = _limiter()
    delays = [limiter.record_failure("127.0.0.1").delay_s for _ in range(12)]
    assert delays[0] == LOOPBACK_DELAY_START_S
    assert delays == sorted(delays)
    assert delays[-1] == LOOPBACK_DELAY_MAX_S
    assert limiter.check("127.0.0.1").allowed is True
    assert limiter.check("::1").allowed is True


def test_browser_attempts_on_loopback_are_lockable_whatever_the_origin() -> None:
    limiter, _clock = _limiter(max_failures=3)
    # Rotating the Origin value gains nothing: one browser bucket per address.
    for port in (5173, 5174, 5175):
        limiter.record_failure("127.0.0.1", origin=f"http://127.0.0.1:{port}")
    assert limiter.check("127.0.0.1", origin="http://127.0.0.1:9999").allowed is False
    assert limiter.check("127.0.0.1", origin="https://other.example").allowed is False
    # The local CLI (no Origin) is still exempt from lockout.
    assert limiter.check("127.0.0.1").allowed is True


def test_rotating_origin_never_lifts_an_address_lockout() -> None:
    limiter, _clock = _limiter()
    for _ in range(10):
        limiter.record_failure(_REMOTE)
    for i in range(20):
        assert limiter.check(_REMOTE, origin=f"https://attacker-{i}.example").allowed is False


def test_rotating_origins_are_charged_to_the_address() -> None:
    limiter, _clock = _limiter()
    for i in range(10):
        limiter.record_failure(_REMOTE, origin=f"https://attacker-{i}.example")
    assert limiter.check(_REMOTE).allowed is False
    assert limiter.check(_REMOTE, origin="https://fresh.example").allowed is False


def test_an_origin_cannot_lock_out_another_address() -> None:
    limiter, _clock = _limiter()
    console = "http://192.168.1.20:18791"
    for _ in range(10):
        limiter.record_failure(_REMOTE, origin=console)
    assert limiter.check(_REMOTE, origin=console).allowed is False
    assert limiter.check("192.168.1.20", origin=console).allowed is True


def test_a_browser_success_does_not_reset_the_budget() -> None:
    limiter, _clock = _limiter(max_failures=3)
    for _ in range(2):
        limiter.record_failure(_REMOTE, origin="https://page.example")
    limiter.record_success(_REMOTE, origin="https://console.example")
    limiter.record_failure(_REMOTE, origin="https://page.example")
    assert limiter.check(_REMOTE, origin="https://page.example").allowed is False


def test_loopback_exemption_can_be_turned_off() -> None:
    limiter, _clock = _limiter(max_failures=2, exempt_loopback=False)
    limiter.record_failure("127.0.0.1")
    assert limiter.record_failure("127.0.0.1").allowed is False
    assert limiter.check("127.0.0.1").allowed is False


def test_disabled_limiter_never_blocks() -> None:
    limiter, _clock = _limiter(enabled=False, max_failures=1)
    for _ in range(5):
        assert limiter.record_failure(_REMOTE).allowed
    assert limiter.check(_REMOTE).allowed


def test_tracked_keys_are_bounded() -> None:
    limiter = AuthRateLimiter(_config(), max_tracked=50)
    for i in range(500):
        limiter.record_failure(f"198.51.100.{i % 250}-{i}")
    assert len(limiter._entries) <= 50  # noqa: SLF001


def test_preauth_budget_counts_per_address() -> None:
    budget = PreauthBudget(limit=2)
    assert budget.try_acquire("10.0.0.1")
    assert budget.try_acquire("10.0.0.1")
    assert not budget.try_acquire("10.0.0.1")
    assert budget.try_acquire("10.0.0.2")
    budget.release("10.0.0.1")
    assert budget.try_acquire("10.0.0.1")
    budget.release("10.0.0.1")
    budget.release("10.0.0.1")
    assert budget.pending("10.0.0.1") == 0
    assert PreauthBudget().limit == MAX_PREAUTH_SOCKETS_PER_IP == 128


def test_config_section_defaults_and_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = GatewayConfig().auth.rate_limit
    assert settings.enabled is True
    assert (settings.max_failures, settings.window_seconds, settings.lockout_seconds) == (
        10,
        60.0,
        300.0,
    )
    assert settings.exempt_loopback is True
    monkeypatch.setenv("AGENTOS_GATEWAY_AUTH__RATE_LIMIT__ENABLED", "false")
    monkeypatch.setenv("AGENTOS_GATEWAY_AUTH__RATE_LIMIT__MAX_FAILURES", "3")
    overridden = GatewayConfig().auth.rate_limit
    assert overridden.enabled is False
    assert overridden.max_failures == 3


def test_config_section_loads_from_toml(tmp_path) -> None:
    path = tmp_path / "agentos.toml"
    path.write_text(
        '[auth]\nmode = "token"\ntoken = "x"\n\n[auth.rate_limit]\nmax_failures = 4\n'
        "lockout_seconds = 30\nexempt_loopback = false\n",
        encoding="utf-8",
    )
    settings = GatewayConfig.load(path).auth.rate_limit
    assert (settings.max_failures, settings.lockout_seconds, settings.exempt_loopback) == (
        4,
        30.0,
        False,
    )


# ── HTTP ───────────────────────────────────────────────────────────────────


def _http(config: GatewayConfig, host: str = _REMOTE) -> TestClient:
    return TestClient(create_gateway_app(config), client=(host, 50000), base_url="http://127.0.0.1")


def test_http_wrong_tokens_lock_out_with_429_and_retry_after() -> None:
    app = create_gateway_app(_config())
    client = TestClient(app, client=(_REMOTE, 50000), base_url="http://127.0.0.1")
    wrong = {"Authorization": "Bearer nope"}
    for _ in range(10):
        assert client.get("/api/system/status", headers=wrong).status_code == 401
    blocked = client.get("/api/system/status", headers=wrong)
    assert blocked.status_code == 429
    assert blocked.headers["retry-after"] == "300"
    body = blocked.json()
    assert body["code"] == "RATE_LIMITED"
    assert body["details"]["code"] == "AUTH_RATE_LIMITED"
    assert 0 < body["details"]["retry_after_ms"] <= 300_000
    # Locked out means locked out: the right token does not get in either.
    right = {"Authorization": f"Bearer {_TOKEN}"}
    assert client.get("/api/system/status", headers=right).status_code == 429
    # Another client of the same gateway is unaffected.
    other = TestClient(app, client=("198.51.100.9", 50000), base_url="http://127.0.0.1")
    assert other.get("/api/system/status", headers=right).status_code == 200


def test_http_missing_token_never_consumes_budget() -> None:
    client = _http(_config(max_failures=2))
    for _ in range(20):
        assert client.get("/api/system/status").status_code == 401
    assert (
        client.get("/api/system/status", headers={"Authorization": f"Bearer {_TOKEN}"}).status_code
        == 200
    )


def test_http_browser_origin_on_loopback_is_not_exempt() -> None:
    client = _http(_config(max_failures=2), host="127.0.0.1")
    headers = {"Authorization": "Bearer nope", "Origin": "http://127.0.0.1:5173"}
    for _ in range(2):
        assert client.get("/api/system/status", headers=headers).status_code == 401
    assert client.get("/api/system/status", headers=headers).status_code == 429


def test_http_success_resets_the_counter() -> None:
    client = _http(_config(max_failures=3))
    wrong = {"Authorization": "Bearer nope"}
    right = {"Authorization": f"Bearer {_TOKEN}"}
    for _ in range(2):
        assert client.get("/api/system/status", headers=wrong).status_code == 401
    assert client.get("/api/system/status", headers=right).status_code == 200
    for _ in range(2):
        assert client.get("/api/system/status", headers=wrong).status_code == 401
    assert client.get("/api/system/status", headers=right).status_code == 200


# ── WebSocket ──────────────────────────────────────────────────────────────


def _ws_attempt(client: TestClient, token: str | None, headers: dict[str, str] | None = None):
    with client.websocket_connect("/ws", headers=headers or {}) as ws:
        ws.receive_json()
        params: dict[str, Any] = {"minProtocol": 3, "maxProtocol": 3}
        if token is not None:
            params["auth"] = {"token": token}
        ws.send_json({"type": "req", "id": "c", "method": "connect", "params": params})
        return ws.receive_json()


def test_ws_wrong_tokens_lock_out_with_auth_rate_limited() -> None:
    client = _http(_config())
    for _ in range(10):
        res = _ws_attempt(client, "nope")
        assert res["error"]["code"] == "UNAUTHORIZED"
        assert res["error"]["details"] is None
    res = _ws_attempt(client, "nope")
    assert res["error"]["code"] == "UNAUTHORIZED"
    assert res["error"]["details"]["code"] == "AUTH_RATE_LIMITED"
    assert 0 < res["error"]["details"]["retry_after_ms"] <= 300_000
    assert res["error"]["retry_after_ms"] == res["error"]["details"]["retry_after_ms"]
    assert res["error"]["retryable"] is True
    # HTTP and WS share one budget per client.
    assert client.get("/api/system/status", headers={"Authorization": "Bearer x"}).status_code == (
        429
    )


def test_ws_missing_token_never_consumes_budget() -> None:
    client = _http(_config(max_failures=2))
    for _ in range(5):
        res = _ws_attempt(client, None)
        assert res["error"]["code"] == "UNAUTHORIZED"
        assert res["error"]["details"] is None
    assert _ws_attempt(client, _TOKEN)["type"] == "hello-ok"


def _declared_origins_config(count: int) -> GatewayConfig:
    """A non-loopback bind that declares every attacker origin, so the origin
    policy admits each of them and only the limiter stands."""
    config = _config(host="0.0.0.0")
    config.control_ui.allowed_origins = [f"https://attacker-{i}.example" for i in range(count)]
    return config


def test_http_rotating_origins_cannot_bypass_the_lockout() -> None:
    client = _http(_declared_origins_config(40))
    for i in range(10):
        headers = {"Authorization": "Bearer nope", "Origin": f"https://attacker-{i}.example"}
        assert client.get("/api/system/status", headers=headers).status_code == 401
    for i in range(10, 40):
        headers = {"Authorization": "Bearer nope", "Origin": f"https://attacker-{i}.example"}
        assert client.get("/api/system/status", headers=headers).status_code == 429
    no_origin = {"Authorization": "Bearer nope"}
    assert client.get("/api/system/status", headers=no_origin).status_code == 429


def test_ws_rotating_origins_cannot_bypass_the_lockout() -> None:
    client = _http(_declared_origins_config(20))
    for _ in range(10):
        assert _ws_attempt(client, "nope")["error"]["details"] is None
    for i in range(20):
        res = _ws_attempt(client, "nope", {"origin": f"https://attacker-{i}.example"})
        assert res["error"]["details"]["code"] == "AUTH_RATE_LIMITED"


def test_ws_attacker_using_the_console_origin_does_not_lock_out_the_operator() -> None:
    app = create_gateway_app(_config(host="0.0.0.0"))
    # The console opened at the gateway's LAN address: a private same-origin
    # page the origin policy admits from any peer (the test client's
    # WebSocket sends Host: testserver unless told otherwise).
    console = {"origin": "http://192.168.1.20:18791", "host": "192.168.1.20:18791"}
    attacker = TestClient(app, client=(_REMOTE, 50000))
    operator = TestClient(app, client=("192.168.1.20", 50000))
    for _ in range(10):
        _ws_attempt(attacker, "nope", console)
    assert _ws_attempt(attacker, _TOKEN, console)["error"]["details"]["code"] == (
        "AUTH_RATE_LIMITED"
    )
    assert _ws_attempt(operator, _TOKEN, console)["type"] == "hello-ok"


def test_ws_loopback_rotating_origins_get_locked_out() -> None:
    client = _http(_config(), host="127.0.0.1")
    results = [
        _ws_attempt(client, "nope", {"origin": f"http://127.0.0.1:{20000 + i}"}) for i in range(11)
    ]
    assert [r["error"]["details"] for r in results[:10]] == [None] * 10
    assert results[10]["error"]["details"]["code"] == "AUTH_RATE_LIMITED"
    # The local CLI keeps working.
    assert _ws_attempt(client, _TOKEN)["type"] == "hello-ok"


def test_ws_browser_origin_on_loopback_is_not_exempt() -> None:
    client = _http(_config(max_failures=2), host="127.0.0.1")
    origin = {"origin": "http://127.0.0.1:5173"}
    for _ in range(2):
        assert _ws_attempt(client, "nope", origin)["error"]["details"] is None
    res = _ws_attempt(client, "nope", origin)
    assert res["error"]["details"]["code"] == "AUTH_RATE_LIMITED"


def test_ws_preauth_budget_refuses_extra_unauthenticated_sockets() -> None:
    app = create_gateway_app(_config())
    app.state.auth_guard.preauth.limit = 1
    client = TestClient(app, client=(_REMOTE, 50000), base_url="http://127.0.0.1")
    with client.websocket_connect("/ws") as first:
        assert first.receive_json()["event"] == "connect.challenge"
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/ws") as second:
                second.receive_json()
        assert exc_info.value.code == 1013
        # Finishing the handshake frees the slot.
        first.send_json(
            {
                "type": "req",
                "id": "c",
                "method": "connect",
                "params": {"auth": {"token": _TOKEN}},
            }
        )
        assert first.receive_json()["type"] == "hello-ok"
        with client.websocket_connect("/ws") as third:
            assert third.receive_json()["event"] == "connect.challenge"
