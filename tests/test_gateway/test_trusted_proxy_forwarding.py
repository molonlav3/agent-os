"""Trusted-proxy CIDR entries, right-to-left X-Forwarded-For, the anti-spoof check."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from agentos.gateway import access
from agentos.gateway.access import peer_is_trusted_proxy, resolve_client_ip
from agentos.gateway.app import create_gateway_app
from agentos.gateway.config import AuthConfig, GatewayConfig

_NO_LOCAL: frozenset[str] = frozenset()


@pytest.mark.parametrize(
    ("configured", "peer", "trusted"),
    [
        ("10.0.0.0/8", "10.20.30.40", True),
        ("10.0.0.0/8", "11.0.0.1", False),
        ("192.168.1.10/24", "192.168.1.99", True),  # host bits are ignored
        ("fd7a:115c:a1e0::/48", "fd7a:115c:a1e0:ab12::1", True),
        ("fd7a:115c:a1e0::/48", "fd7a:115d::1", False),
        # A dual-stack listener reports IPv4 peers in mapped form.
        ("10.0.0.0/8", "::ffff:10.0.0.1", True),
        ("10.0.0.0/8", "::ffff:11.0.0.1", False),
        ("127.0.0.1, 10.0.0.0/8", "10.1.1.1", True),
        # An unparseable block keeps the exact string comparison.
        ("10.0.0.0/99", "10.0.0.0/99", True),
        ("10.0.0.0/99", "10.0.0.1", False),
        # Exact entries keep working.
        ("10.0.0.1", "10.0.0.1", True),
    ],
)
def test_cidr_entries(configured: str, peer: str, trusted: bool) -> None:
    assert peer_is_trusted_proxy(configured, peer) is trusted


@pytest.mark.parametrize(
    ("trusted", "header", "client"),
    [
        # The hop the trusted proxy received the request from is the client.
        ("10.0.0.1", "203.0.113.99, 10.0.0.5", "10.0.0.5"),
        # Trusted hops are skipped right to left.
        ("10.0.0.0/24", "203.0.113.99, 10.0.0.5", "203.0.113.99"),
        # Every hop trusted: the left-most is the best we know.
        ("10.0.0.0/24", "10.0.0.7, 10.0.0.5", "10.0.0.7"),
        # A value that is not an address stops the walk at the last trusted hop.
        ("10.0.0.0/24", "garbage, 10.0.0.5", "10.0.0.5"),
        # Port suffixes some proxies add.
        ("10.0.0.1", "198.51.100.4:51234", "198.51.100.4"),
        ("10.0.0.1", "[2001:db8::7]:443", "2001:db8::7"),
        # Blank entries are ignored.
        ("10.0.0.1", " , 198.51.100.4 ,", "198.51.100.4"),
    ],
)
def test_forwarded_for_is_walked_right_to_left(trusted: str, header: str, client: str) -> None:
    assert resolve_client_ip("10.0.0.1", header, trusted, local_addresses=_NO_LOCAL) == client


def test_header_from_an_untrusted_peer_is_ignored() -> None:
    assert (
        resolve_client_ip("198.51.100.1", "1.2.3.4", "10.0.0.1", local_addresses=_NO_LOCAL)
        == "198.51.100.1"
    )
    assert resolve_client_ip(None, "1.2.3.4", "10.0.0.1") == "unknown"
    assert resolve_client_ip("10.0.0.1", None, "10.0.0.1") == "10.0.0.1"


@pytest.mark.parametrize("claimed", ["127.0.0.1", "::1", "::ffff:127.0.0.1", "192.0.2.50"])
def test_forwarded_address_naming_this_host_is_a_spoof(claimed: str) -> None:
    # 192.0.2.50 stands in for one of this host's own interface addresses.
    own = frozenset({"192.0.2.50"})
    assert resolve_client_ip("10.0.0.1", claimed, "10.0.0.1", local_addresses=own) == "10.0.0.1"


def test_local_addresses_are_discovered_without_dns_and_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    def _discover() -> frozenset[str]:
        calls.append(1)
        return frozenset({"192.0.2.9"})

    monkeypatch.setattr(access, "_discover_local_addresses", _discover)
    monkeypatch.setattr(access, "_local_address_cache", None)
    assert access.local_interface_addresses() == frozenset({"192.0.2.9"})
    assert access.local_interface_addresses() == frozenset({"192.0.2.9"})
    assert calls == [1]
    assert resolve_client_ip("10.0.0.1", "192.0.2.9", "10.0.0.1") == "10.0.0.1"


def test_trusted_proxy_mode_admits_a_peer_inside_a_cidr_block() -> None:
    config = GatewayConfig(
        host="0.0.0.0", auth=AuthConfig(mode="trusted-proxy", trusted_proxy="10.0.0.0/8")
    )
    inside = TestClient(create_gateway_app(config), client=("10.9.8.7", 4000))
    outside = TestClient(create_gateway_app(config), client=("203.0.113.1", 4000))
    assert inside.get("/api/system/status").status_code == 200
    assert outside.get("/api/system/status").status_code == 401
