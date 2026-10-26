"""Origin policy and bind modes (#3692).

One ``OriginPolicy`` decides for the WebSocket handshake, the HTTP origin
guard and CORS on every bind; ``[gateway] bind`` / ``--bind-mode`` picks the
listener. These tests pin the policy matrix, the CORS answers, the startup
refusal for a public Control UI without a declared origin, and bind
resolution (with ``tailscale`` faked).
"""

from __future__ import annotations

import socket
from collections.abc import Sequence

import pytest
from pydantic import ValidationError
from starlette.testclient import TestClient

from agentos.cli.gateway_cmd import resolve_cli_bind
from agentos.gateway.app import create_gateway_app
from agentos.gateway.bind_modes import (
    BindResolutionError,
    normalize_bind_mode,
    resolve_bind,
    tailscale_ipv4,
)
from agentos.gateway.boot import bind_listen_socket
from agentos.gateway.config import (
    AuthConfig,
    ControlUiConfig,
    CorsConfig,
    GatewayConfig,
    GatewayServeConfig,
)
from agentos.gateway.origin_policy import (
    OriginPolicy,
    bind_requires_origin_policy,
    build_origin_policy,
    enforce_public_bind_origin_guard,
    is_private_host,
)

# ---------------------------------------------------------------------------
# Policy matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("host", "private"),
    [
        ("127.0.0.1", True),
        ("localhost", True),
        ("::1", True),
        ("10.1.2.3", True),
        ("172.16.0.9", True),
        ("172.32.0.9", False),
        ("192.168.0.4", True),
        ("169.254.10.10", True),
        ("100.100.1.1", True),
        ("fd00::1", True),
        ("fe80::1", True),
        ("8.8.8.8", False),
        ("2001:db8::1", False),
        ("box.local", True),
        ("box.tail9.ts.net", True),
        ("box.lan", False),
        ("example.com", False),
        ("local", False),
    ],
)
def test_private_host_predicate(host: str, private: bool) -> None:
    assert is_private_host(host) is private


def _lan_policy(*origins: str) -> OriginPolicy:
    return OriginPolicy(allowed_origins=origins, bind_is_loopback=False)


class TestPolicyMatrix:
    def test_no_origin_is_admitted(self) -> None:
        assert _lan_policy().evaluate(None).reason == "no-origin"

    def test_malformed_origin_fails_closed(self) -> None:
        decision = _lan_policy().evaluate("http://[::1")
        assert decision.allowed is False
        assert decision.reason == "malformed"

    def test_allowlist_wins_regardless_of_host(self) -> None:
        policy = _lan_policy("https://agent.example.com")
        decision = policy.evaluate("https://agent.example.com", host_header="10.0.0.2:18791")
        assert decision.reason == "allowlist"

    def test_private_same_origin_requires_matching_port(self) -> None:
        policy = _lan_policy()
        assert policy.allows("http://10.0.0.2:18791", host_header="10.0.0.2:18791")
        assert not policy.allows("http://10.0.0.2:8080", host_header="10.0.0.2:18791")

    def test_same_origin_default_ports(self) -> None:
        policy = _lan_policy()
        assert policy.allows("https://box.local", host_header="box.local:443")
        assert policy.allows("http://box.local:80", host_header="box.local")

    def test_public_same_origin_is_refused(self) -> None:
        policy = _lan_policy()
        decision = policy.evaluate("http://agent.example.com", host_header="agent.example.com")
        assert decision.allowed is False
        assert decision.reason == "denied"

    def test_dns_rebinding_name_is_refused(self) -> None:
        """A rebound hostname carries itself in both Origin and Host."""
        policy = _lan_policy()
        assert not policy.allows(
            "http://attacker.example:18791", host_header="attacker.example:18791"
        )

    def test_loopback_origin_needs_local_unforwarded_peer_off_loopback(self) -> None:
        policy = _lan_policy()
        assert policy.evaluate("http://localhost:5173", peer_ip="127.0.0.1").reason == (
            "loopback-local"
        )
        assert not policy.allows("http://localhost:5173", peer_ip="192.168.1.4")
        assert not policy.allows("http://localhost:5173", peer_ip="127.0.0.1", forwarded=True)

    def test_loopback_bind_trusts_loopback_origins_on_any_port(self) -> None:
        policy = OriginPolicy(bind_is_loopback=True)
        assert policy.allows("http://127.0.0.1:3000")
        assert not policy.allows("http://127.0.0.1:3000", forwarded=True)

    def test_loopback_bind_refuses_private_same_origin(self) -> None:
        policy = OriginPolicy(bind_is_loopback=True)
        assert not policy.allows("http://10.0.0.2:18791", host_header="10.0.0.2:18791")
        assert not policy.allows("http://box.local:18791", host_header="box.local:18791")

    def test_build_seeds_local_origins_and_declared_ones(self) -> None:
        config = GatewayConfig(
            host="0.0.0.0",
            port=18804,
            auth=AuthConfig(mode="token", token="t"),
            control_ui=ControlUiConfig(allowed_origins=["https://ui.example.com"]),
            cors=CorsConfig(allowed_origins=["*", "https://app.example.com"]),
            gateway=GatewayServeConfig(public_origin="https://agent.example.com"),
        )
        policy = build_origin_policy(config)
        assert policy.bind_is_loopback is False
        assert set(policy.allowed_origins) >= {
            "http://localhost:18804",
            "http://127.0.0.1:18804",
            "https://ui.example.com",
            "https://app.example.com",
            "https://agent.example.com",
        }
        assert "*" not in policy.allowed_origins
        assert policy.allows("http://127.0.0.1:18804", host_header="192.168.1.2:18804")

    def test_tls_seeds_https_origins(self) -> None:
        config = GatewayConfig(port=18804)
        config.tls.keyfile = "key.pem"
        config.tls.certfile = "cert.pem"
        assert "https://localhost:18804" in build_origin_policy(config).allowed_origins


# ---------------------------------------------------------------------------
# CORS through the real app
# ---------------------------------------------------------------------------


def _lan_client() -> TestClient:
    config = GatewayConfig(
        host="0.0.0.0",
        port=18791,
        auth=AuthConfig(mode="token", token="test-token"),
    )
    return TestClient(
        create_gateway_app(config),
        base_url="http://192.168.1.10:18791",
        headers={"authorization": "Bearer test-token"},
    )


class TestCors:
    def test_foreign_origin_is_refused_and_never_reflected(self) -> None:
        resp = _lan_client().get("/api/sessions", headers={"origin": "https://evil.example"})
        assert resp.status_code == 403
        assert "access-control-allow-origin" not in resp.headers

    def test_same_origin_lan_page_is_reflected_explicitly(self) -> None:
        resp = _lan_client().get("/api/sessions", headers={"origin": "http://192.168.1.10:18791"})
        assert resp.status_code == 200
        assert resp.headers["access-control-allow-origin"] == "http://192.168.1.10:18791"
        assert resp.headers["access-control-allow-credentials"] == "true"

    def test_preflight_for_allowed_origin(self) -> None:
        resp = _lan_client().options(
            "/api/v1/files/upload",
            headers={
                "origin": "http://192.168.1.10:18791",
                "access-control-request-method": "POST",
                "access-control-request-headers": "authorization",
            },
        )
        assert resp.status_code == 200
        assert resp.headers["access-control-allow-origin"] == "http://192.168.1.10:18791"

    def test_wildcard_is_never_answered(self) -> None:
        """``cors.allowed_origins = ["*"]`` (the old default) adds nothing."""
        config = GatewayConfig(cors=CorsConfig(allowed_origins=["*"]))
        client = TestClient(create_gateway_app(config), base_url="http://localhost")
        resp = client.get("/health", headers={"origin": "https://evil.example"})
        assert resp.status_code == 200
        assert resp.headers.get("access-control-allow-origin") is None

    def test_ws_handshake_on_lan_bind_refuses_foreign_origin(self) -> None:
        from starlette.testclient import WebSocketDenialResponse
        from starlette.websockets import WebSocketDisconnect

        client = _lan_client()
        with pytest.raises((WebSocketDisconnect, WebSocketDenialResponse)):
            with client.websocket_connect("/ws", headers={"origin": "https://evil.example"}):
                pass  # pragma: no cover

    def test_ws_handshake_on_lan_bind_accepts_same_origin(self) -> None:
        client = _lan_client()
        # The test client always sends ``Host: testserver`` on a websocket;
        # a browser sends the address it opened.
        headers = {"origin": "http://192.168.1.10:18791", "host": "192.168.1.10:18791"}
        with client.websocket_connect("/ws", headers=headers) as ws:
            assert ws.receive_json()["event"] == "connect.challenge"


# ---------------------------------------------------------------------------
# Startup guard for a public Control UI
# ---------------------------------------------------------------------------


class TestPublicBindOriginGuard:
    @pytest.mark.parametrize(
        ("host", "needs"),
        [
            ("127.0.0.1", False),
            ("0.0.0.0", False),
            ("::", False),
            ("192.168.1.5", False),
            ("100.64.0.9", False),
            ("203.0.113.7", True),
            ("agent.example.com", True),
        ],
    )
    def test_which_binds_need_a_declared_origin(self, host: str, needs: bool) -> None:
        assert bind_requires_origin_policy(host) is needs

    def test_public_ip_without_origin_is_refused_with_the_fix(self) -> None:
        config = GatewayConfig(host="203.0.113.7", auth=AuthConfig(mode="token", token="t"))
        with pytest.raises(ValueError) as excinfo:
            enforce_public_bind_origin_guard(config)
        message = str(excinfo.value)
        assert "public_origin" in message
        assert "control_ui.allowed_origins" in message

    def test_public_origin_satisfies_the_guard(self) -> None:
        config = GatewayConfig(
            host="203.0.113.7",
            auth=AuthConfig(mode="token", token="t"),
            gateway=GatewayServeConfig(public_origin="https://agent.example.com"),
        )
        enforce_public_bind_origin_guard(config)

    def test_disabled_control_ui_is_not_refused(self) -> None:
        config = GatewayConfig(
            host="203.0.113.7",
            auth=AuthConfig(mode="token", token="t"),
            control_ui=ControlUiConfig(enabled=False),
        )
        enforce_public_bind_origin_guard(config)

    def test_unparseable_origin_does_not_count(self) -> None:
        config = GatewayConfig(
            host="203.0.113.7",
            auth=AuthConfig(mode="token", token="t"),
            control_ui=ControlUiConfig(allowed_origins=["agent.example.com"]),
        )
        with pytest.raises(ValueError):
            enforce_public_bind_origin_guard(config)


class TestPublicOriginValidation:
    @pytest.mark.parametrize(
        "value", ["https://agent.example.com", "http://10.0.0.2:18791", "https://a.example/"]
    )
    def test_accepts_full_origins(self, value: str) -> None:
        assert GatewayServeConfig(public_origin=value).public_origin == value.rstrip("/")

    @pytest.mark.parametrize(
        "value", ["agent.example.com", "ftp://a.example", "https://a.example/path", "https://"]
    )
    def test_rejects_non_origins(self, value: str) -> None:
        with pytest.raises(ValidationError):
            GatewayServeConfig(public_origin=value)


# ---------------------------------------------------------------------------
# Bind modes
# ---------------------------------------------------------------------------


def _runner(code: int = 0, stdout: str = "100.101.102.103\n", stderr: str = ""):  # noqa: ANN202
    calls: list[list[str]] = []

    def run(argv: Sequence[str]) -> tuple[int, str, str]:
        calls.append(list(argv))
        return code, stdout, stderr

    run.calls = calls  # type: ignore[attr-defined]
    return run


class TestBindModes:
    def test_loopback(self) -> None:
        resolution = resolve_bind("loopback", host="0.0.0.0")
        assert (resolution.mode, resolution.hosts) == ("loopback", ("127.0.0.1",))

    def test_lan(self) -> None:
        resolution = resolve_bind("lan", host="127.0.0.1")
        assert (resolution.mode, resolution.hosts) == ("lan", ("0.0.0.0",))

    def test_tailnet_binds_the_tailnet_ip_and_loopback(self) -> None:
        runner = _runner()
        resolution = resolve_bind("tailnet", host="127.0.0.1", tailscale_runner=runner)
        assert resolution.mode == "tailnet"
        assert resolution.hosts == ("100.101.102.103", "127.0.0.1")
        assert runner.calls == [["tailscale", "ip", "-4"]]  # type: ignore[attr-defined]

    @pytest.mark.parametrize(
        ("code", "stdout", "stderr"),
        [
            (1, "", "Tailscale is stopped."),
            (0, "", ""),
            (0, "192.168.1.5\n", ""),
            (0, "not an address\n", ""),
        ],
    )
    def test_tailnet_never_falls_back(self, code: int, stdout: str, stderr: str) -> None:
        with pytest.raises(BindResolutionError) as excinfo:
            resolve_bind(
                "tailnet", host="127.0.0.1", tailscale_runner=_runner(code, stdout, stderr)
            )
        assert "tailnet" in str(excinfo.value)

    def test_tailnet_without_cli(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("agentos.gateway.bind_modes.shutil.which", lambda _name: None)
        with pytest.raises(BindResolutionError, match="tailscale CLI"):
            tailscale_ipv4()

    def test_custom_uses_the_host(self) -> None:
        assert resolve_bind("custom", host="10.0.0.4").hosts == ("10.0.0.4",)

    def test_unset_mode_follows_host(self) -> None:
        assert resolve_bind(None, host="127.0.0.1").mode == "loopback"
        assert resolve_bind(None, host="10.0.0.4").mode == "custom"

    def test_unknown_mode(self) -> None:
        with pytest.raises(BindResolutionError):
            normalize_bind_mode("public")

    def test_config_rejects_unknown_mode(self) -> None:
        with pytest.raises(ValidationError):
            GatewayServeConfig(bind="public")  # type: ignore[arg-type]


class TestCliBindPrecedence:
    def _config(self, **gateway: object) -> GatewayConfig:
        return GatewayConfig(gateway=GatewayServeConfig(**gateway))  # type: ignore[arg-type]

    def test_listen_wins_and_is_custom(self) -> None:
        resolution = resolve_cli_bind(
            self._config(bind="lan"), explicit_host="10.0.0.4", bind_mode="tailnet", env={}
        )
        assert (resolution.mode, resolution.host) == ("custom", "10.0.0.4")

    def test_flag_mode_beats_env_and_config(self) -> None:
        resolution = resolve_cli_bind(
            self._config(bind="loopback"),
            explicit_host=None,
            bind_mode="lan",
            env={"AGENTOS_LISTEN": "10.0.0.4"},
        )
        assert resolution.host == "0.0.0.0"

    def test_env_host_beats_config_mode(self) -> None:
        resolution = resolve_cli_bind(
            self._config(bind="lan"),
            explicit_host=None,
            bind_mode=None,
            env={"AGENTOS_LISTEN": "10.0.0.4"},
        )
        assert (resolution.mode, resolution.host) == ("custom", "10.0.0.4")

    def test_config_mode_applies(self) -> None:
        resolution = resolve_cli_bind(
            self._config(bind="tailnet"),
            explicit_host=None,
            bind_mode=None,
            env={},
            tailscale_runner=_runner(),
        )
        assert resolution.hosts == ("100.101.102.103", "127.0.0.1")

    def test_default_is_loopback(self) -> None:
        resolution = resolve_cli_bind(GatewayConfig(), explicit_host=None, bind_mode=None, env={})
        assert (resolution.mode, resolution.host) == ("loopback", "127.0.0.1")


def test_bind_listen_socket_binds_and_reports_failures() -> None:
    sock = bind_listen_socket("127.0.0.1", 0)
    try:
        assert sock.getsockname()[1] > 0
        assert sock.family == socket.AF_INET
    finally:
        sock.close()
    # An address this machine does not own (TEST-NET-3) cannot be bound.
    with pytest.raises(ValueError, match="cannot listen"):
        bind_listen_socket("203.0.113.250", 0)


def test_lifecycle_manager_forwards_bind_mode_to_the_spawned_gateway() -> None:
    from agentos.cli.gateway_lifecycle import GatewayLifecycleManager

    tailnet = GatewayLifecycleManager(host="100.101.102.103", port=18804, bind_mode="tailnet")
    argv = tailnet._gateway_run_argv()
    assert argv[argv.index("--bind-mode") + 1] == "tailnet"
    assert "--listen" not in argv
    custom = GatewayLifecycleManager(host="10.0.0.4", port=18804, bind_mode="custom")
    argv = custom._gateway_run_argv()
    assert argv[argv.index("--listen") + 1] == "10.0.0.4"


def test_new_default_sections_are_not_persisted() -> None:
    """An install that never sets them writes a file older releases still load."""
    data = GatewayConfig().to_toml_dict()
    assert "gateway" not in data
    changed = GatewayConfig(gateway=GatewayServeConfig(bind="lan")).to_toml_dict()
    assert changed["gateway"]["bind"] == "lan"
