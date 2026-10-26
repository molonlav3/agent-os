"""WebSocket Origin/Host validation — defense against CSWSH + DNS rebinding.

The WS upgrade path skips ``AuthMiddleware`` (middleware.py:33), and on a
loopback bind any browser peer is auto-upgraded to operator scopes purely by
``peer_ip`` proximity (auth.py:152). A malicious web page the victim visits
can therefore open ``ws://127.0.0.1:<port>/ws`` and drive the Control RPC
surface — this is the CVE-2026-53869 (Hermes) / CSWSH class. The handshake
must reject cross-origin and rebound-Host connections before ``accept()``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient, WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect, WebSocketState

from agentos.gateway.config import AuthConfig, ControlUiConfig, GatewayConfig
from agentos.gateway.websocket import handle_ws_connection, is_allowed_ws_origin


def _config(port: int = 18791, allowed_origins: list[str] | None = None) -> GatewayConfig:
    return GatewayConfig(
        port=port,
        control_ui=ControlUiConfig(allowed_origins=allowed_origins or []),
    )


class TestIsAllowedWsOrigin:
    def test_missing_origin_is_allowed(self) -> None:
        """Non-browser clients (CLI, node) send no Origin — must not be blocked."""
        assert is_allowed_ws_origin(None, _config()) is True
        assert is_allowed_ws_origin("", _config()) is True

    @pytest.mark.parametrize(
        "origin",
        [
            "http://127.0.0.1:18791",
            "http://127.0.0.1:3000",
            "http://localhost:18791",
            "https://localhost",
            "http://[::1]:18791",
        ],
    )
    def test_loopback_origins_allowed(self, origin: str) -> None:
        assert is_allowed_ws_origin(origin, _config()) is True

    @pytest.mark.parametrize(
        "origin",
        [
            "http://evil.com",
            "https://attacker.example",
            "http://127.0.0.1.evil.com",
            "http://192.168.1.5",
            "https://localhost.evil.com",
            "null",
        ],
    )
    def test_cross_origin_rejected(self, origin: str) -> None:
        assert is_allowed_ws_origin(origin, _config()) is False

    def test_explicit_allowlist_origin_allowed(self) -> None:
        cfg = _config(allowed_origins=["https://ui.example.com"])
        assert is_allowed_ws_origin("https://ui.example.com", cfg) is True

    def test_explicit_allowlist_does_not_widen_others(self) -> None:
        cfg = _config(allowed_origins=["https://ui.example.com"])
        assert is_allowed_ws_origin("https://other.example.com", cfg) is False

    def test_origin_match_is_exact_not_prefix(self) -> None:
        cfg = _config(allowed_origins=["https://ui.example.com"])
        assert is_allowed_ws_origin("https://ui.example.com.evil.com", cfg) is False

    @pytest.mark.parametrize(
        "origin",
        ["http://[::1", "http://[gg::1]", "http://[::1]%00.evil"],
    )
    def test_malformed_origin_rejected_not_raised(self, origin: str) -> None:
        """A browser-sendable but unparseable Origin must fail closed, not crash."""
        assert is_allowed_ws_origin(origin, _config()) is False

    def test_public_bind_allows_same_origin_private_address(self) -> None:
        """A browser on the LAN opens the console at the gateway's own address."""
        cfg = GatewayConfig(host="0.0.0.0", port=18791, auth=AuthConfig(mode="token"))
        assert (
            is_allowed_ws_origin("http://192.168.1.10:18791", cfg, host_header="192.168.1.10:18791")
            is True
        )

    def test_public_bind_gates_origin(self) -> None:
        """A non-loopback bind used to accept every Origin (#3692)."""
        cfg = GatewayConfig(host="203.0.113.7", port=18791, auth=AuthConfig(mode="token"))
        assert is_allowed_ws_origin("http://evil.com", cfg, host_header="evil.com") is False
        assert (
            is_allowed_ws_origin("http://evil.com:18791", cfg, host_header="evil.com:18791")
            is False
        )

    def test_public_ip_same_origin_needs_declaring(self) -> None:
        cfg = GatewayConfig(host="203.0.113.7", port=18791, auth=AuthConfig(mode="token"))
        origin = "http://203.0.113.7:18791"
        assert is_allowed_ws_origin(origin, cfg, host_header="203.0.113.7:18791") is False
        cfg.gateway.public_origin = origin
        assert is_allowed_ws_origin(origin, cfg, host_header="203.0.113.7:18791") is True

    @pytest.mark.parametrize(
        ("host", "origin", "host_header"),
        [
            ("0.0.0.0", "http://192.168.1.10:18791", "192.168.1.10:18791"),
            ("::", "http://[fd12::7]:18791", "[fd12::7]:18791"),
            ("0.0.0.0", "http://mybox.local:18791", "mybox.local:18791"),
            ("100.101.102.103", "http://mybox.tail1.ts.net:18791", "mybox.tail1.ts.net:18791"),
            ("100.101.102.103", "http://100.101.102.103:18791", "100.101.102.103:18791"),
        ],
    )
    def test_non_loopback_bind_admits_private_same_origin(
        self, host: str, origin: str, host_header: str
    ) -> None:
        cfg = GatewayConfig(host=host, port=18791, auth=AuthConfig(mode="token"))
        assert is_allowed_ws_origin(origin, cfg, host_header=host_header) is True

    @pytest.mark.parametrize(
        ("origin", "host_header"),
        [
            # A public IPv6 / an undeclared DNS name: not private.
            ("http://[2001:db8::7]:18791", "[2001:db8::7]:18791"),
            ("http://myserver.example:18791", "myserver.example:18791"),
            # Private, but not the Host the request was sent to.
            ("http://192.168.1.11:18791", "192.168.1.10:18791"),
        ],
    )
    def test_non_loopback_bind_refuses_other_origins(self, origin: str, host_header: str) -> None:
        cfg = GatewayConfig(host="0.0.0.0", port=18791, auth=AuthConfig(mode="token"))
        assert is_allowed_ws_origin(origin, cfg, host_header=host_header) is False

    def test_seeded_loopback_origin_admitted_on_a_lan_bind(self) -> None:
        """A LAN bind never locks out the browser on the gateway's own machine."""
        cfg = GatewayConfig(host="0.0.0.0", port=18791, auth=AuthConfig(mode="token"))
        assert (
            is_allowed_ws_origin(
                "http://127.0.0.1:18791", cfg, host_header="192.168.1.10:18791", peer_ip="10.0.0.9"
            )
            is True
        )

    def test_other_loopback_port_needs_a_local_peer_off_loopback(self) -> None:
        cfg = GatewayConfig(host="0.0.0.0", port=18791, auth=AuthConfig(mode="token"))
        origin = "http://localhost:5173"
        assert is_allowed_ws_origin(origin, cfg, peer_ip="127.0.0.1") is True
        assert is_allowed_ws_origin(origin, cfg, peer_ip="192.168.1.30") is False
        assert is_allowed_ws_origin(origin, cfg, peer_ip="127.0.0.1", forwarded=True) is False

    def test_loopback_bind_does_not_trust_private_names(self) -> None:
        """mDNS names are spoofable on a LAN; they never borrow loopback trust."""
        origin = "http://evil.local:18791"
        assert is_allowed_ws_origin(origin, _config(), host_header="evil.local:18791") is False


class TestAllowedOriginNormalization:
    """control_ui.allowed_origins entries and the browser Origin must compare
    semantically (default ports, trailing slash/dot, case), not byte-for-byte —
    browsers always send the canonical form."""

    def test_explicit_default_port_entry_matches_canonical_origin(self) -> None:
        cfg = _config(allowed_origins=["https://agent.example.com:443"])
        assert is_allowed_ws_origin("https://agent.example.com", cfg) is True

    def test_bare_entry_matches_origin_with_explicit_default_port(self) -> None:
        cfg = _config(allowed_origins=["https://agent.example.com"])
        assert is_allowed_ws_origin("https://agent.example.com:443", cfg) is True

    def test_trailing_slash_entry_matches(self) -> None:
        cfg = _config(allowed_origins=["https://agent.example.com/"])
        assert is_allowed_ws_origin("https://agent.example.com", cfg) is True

    def test_trailing_dot_fqdn_origin_matches_dotless_entry(self) -> None:
        cfg = _config(allowed_origins=["https://agent.example.com"])
        assert is_allowed_ws_origin("https://agent.example.com.", cfg) is True

    def test_host_case_is_normalized(self) -> None:
        cfg = _config(allowed_origins=["https://Agent.Example.com"])
        assert is_allowed_ws_origin("https://agent.example.com", cfg) is True

    def test_trailing_dot_loopback_origin_allowed(self) -> None:
        assert is_allowed_ws_origin("http://localhost.:18791", _config()) is True

    def test_normalization_does_not_widen_scheme_or_port(self) -> None:
        cfg = _config(allowed_origins=["https://agent.example.com"])
        assert is_allowed_ws_origin("http://agent.example.com", cfg) is False
        assert is_allowed_ws_origin("https://agent.example.com:8443", cfg) is False

    def test_schemeless_entry_matches_nothing(self) -> None:
        """A scheme-less entry cannot express an origin — it must not match
        (and resolve_trusted_hosts warns about it at startup)."""
        cfg = _config(allowed_origins=["agent.example.com"])
        assert is_allowed_ws_origin("https://agent.example.com", cfg) is False


class _RecordingWebSocket:
    """Fake WS capturing accept/close and carrying request headers."""

    client_state = WebSocketState.CONNECTED
    client = SimpleNamespace(host="127.0.0.1", port=12345)

    def __init__(self, headers: dict[str, str] | None = None) -> None:
        self.headers = headers or {}
        self.accepted = False
        self.closed_code: int | None = None

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000) -> None:
        self.closed_code = code


class TestHandshakeOriginEnforcement:
    @pytest.mark.asyncio
    async def test_cross_origin_handshake_rejected_before_accept(self) -> None:
        ws = _RecordingWebSocket(headers={"origin": "http://evil.com"})

        await handle_ws_connection(ws, _config(), dispatcher=object())

        assert ws.accepted is False
        assert ws.closed_code is not None

    @pytest.mark.asyncio
    async def test_loopback_origin_handshake_accepts(self) -> None:
        ws = _RecordingWebSocket(headers={"origin": "http://127.0.0.1:18791"})

        # Reaches accept() then disconnects on the challenge send (no send_text).
        try:
            await handle_ws_connection(ws, _config(), dispatcher=object())
        except AttributeError:
            pass  # fake lacks send_text; we only assert accept happened

        assert ws.accepted is True

    @pytest.mark.asyncio
    async def test_no_origin_handshake_accepts(self) -> None:
        ws = _RecordingWebSocket(headers={})

        try:
            await handle_ws_connection(ws, _config(), dispatcher=object())
        except AttributeError:
            pass

        assert ws.accepted is True


class TestRealAppHandshake:
    """The Origin guard through the real ASGI stack — real ``/ws`` route
    wiring in ``create_gateway_app``, a real starlette ``WebSocket`` denied
    before ``accept()``, and real case-insensitive ``Headers`` semantics.
    A fake-only suite would stay green if the guard were unwired."""

    def _client(self) -> TestClient:
        from agentos.gateway.app import create_gateway_app

        return TestClient(create_gateway_app(GatewayConfig()))

    def test_cross_origin_handshake_denied_through_real_app(self) -> None:
        client = self._client()
        with pytest.raises((WebSocketDisconnect, WebSocketDenialResponse)):
            with client.websocket_connect("/ws", headers={"Origin": "http://evil.com"}):
                pass  # pragma: no cover — handshake must not complete

    def test_loopback_origin_handshake_accepted_through_real_app(self) -> None:
        client = self._client()
        with client.websocket_connect("/ws", headers={"origin": "http://127.0.0.1:18791"}) as ws:
            frame = ws.receive_json()
            assert frame["event"] == "connect.challenge"

    def test_no_origin_handshake_accepted_through_real_app(self) -> None:
        client = self._client()
        with client.websocket_connect("/ws") as ws:
            frame = ws.receive_json()
            assert frame["event"] == "connect.challenge"


class TestBindPostureIsCaptured:
    """[P2] The guards must key off the bind posture captured when the app is
    built, not a mutable ``config.host`` read at request time. ``config.apply``
    mutates the config in place without rebinding the live socket; a host
    change to a public address must NOT silently disable the Origin/Host
    guards while the process still listens on loopback."""

    def test_ws_origin_guard_survives_runtime_host_mutation(self) -> None:
        from agentos.gateway.app import create_gateway_app

        cfg = GatewayConfig()  # loopback bind, socket built now
        client = TestClient(create_gateway_app(cfg), base_url="http://localhost")
        cfg.host = "0.0.0.0"  # config.apply-style in-place mutation, no rebind
        resp = client.get("/api/config", headers={"origin": "http://evil.com"})
        assert resp.status_code == 403
        assert "access-control-allow-origin" not in resp.headers

    def test_host_guard_survives_runtime_host_mutation(self) -> None:
        from agentos.gateway.app import create_gateway_app

        cfg = GatewayConfig()
        client = TestClient(create_gateway_app(cfg), base_url="http://localhost")
        cfg.host = "0.0.0.0"
        # Host allowlist must still reject a rebound foreign Host.
        assert client.get("/ready", headers={"host": "attacker.com"}).status_code == 400
