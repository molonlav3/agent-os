"""One browser-origin policy for every gateway transport.

The WebSocket handshake, the HTTP origin guard, the CORS layer and the Host
guard all answer the same question -- "may a page served from this origin
drive the gateway?" -- so they share one implementation instead of three
slightly different ones. The policy, in order:

1. No ``Origin`` header: a non-browser client (CLI, curl, node peers). Callers
   decide what that means; the transports admit it, auth still applies.
2. The origin is in the allowlist: ``control_ui.allowed_origins``, explicit
   ``cors.allowed_origins`` entries, ``[gateway] public_origin``, and the
   loopback origins of the gateway's own port seeded at startup.
3. Private same-origin: the origin's host and port equal the request's
   ``Host`` and that host is private -- loopback, RFC 1918, link-local,
   IPv6 unique-local, the 100.64.0.0/10 range tailnets assign, or a
   ``.local`` / ``.ts.net`` name. On a loopback bind only loopback hosts
   qualify: a loopback gateway is never legitimately reached by a LAN name,
   so a spoofed ``.local`` name must not borrow loopback trust.
4. A loopback origin (another local port, e.g. the Vite dev server) on a
   genuinely local socket: the peer is loopback and the request was not
   forwarded by a proxy.

Anything else is denied, including an origin that does not parse.

A DNS-rebinding page fails rule 3 because its hostname is public; a page on
another site fails every rule. Before this policy, a non-loopback bind
accepted every origin and reflected it through CORS with credentials.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import structlog
from starlette.datastructures import Headers
from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from agentos.gateway.access import is_loopback_address, is_loopback_bind

log = structlog.get_logger(__name__)

_DEFAULT_PORTS = {"http": 80, "https": 443}

#: Name suffixes treated as private for the same-origin rule: mDNS names and
#: tailnet MagicDNS names. Neither resolves on the public internet.
PRIVATE_NAME_SUFFIXES: tuple[str, ...] = (".local", ".ts.net")

#: Address ranges treated as private for the same-origin rule.
_PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",  # IPv4 link-local
        "100.64.0.0/10",  # shared address space; tailnets assign from it
        "fc00::/7",  # IPv6 unique-local
        "fe80::/10",  # IPv6 link-local
    )
)

#: Headers that mark a request as relayed by a proxy. A loopback peer that
#: carries one of them is the proxy, not the browser, so it is not "local".
_FORWARDING_HEADERS = ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-real-ip")

WILDCARD_BIND_ADDRESSES = frozenset({"0.0.0.0", "::", ""})


OriginKey = tuple[str, str, int | None]


def origin_key(value: str) -> OriginKey | None:
    """Parse an origin into a comparable ``(scheme, host, port)`` key.

    Browsers send ``Origin`` canonically (lowercase host, default port
    omitted); operators type whatever variant comes to mind. Both sides are
    normalized -- lowercase, brackets and trailing dot stripped, the scheme's
    default port applied, ``ws``/``wss`` folded into ``http``/``https`` -- so
    ``https://agent.example.com:443`` equals ``https://agent.example.com.``.
    Returns ``None`` for anything that is not ``scheme://host[:port]``.
    """
    try:
        parts = urlsplit(value.strip())
        host = (parts.hostname or "").rstrip(".")
        port = parts.port
    except ValueError:
        return None
    scheme = {"ws": "http", "wss": "https"}.get(parts.scheme.lower(), parts.scheme.lower())
    if not scheme or not host:
        return None
    if port is None:
        port = _DEFAULT_PORTS.get(scheme)
    return (scheme, host, port)


def origin_in_allowlist(origin: str, allowed: Iterable[str]) -> bool:
    """True if ``origin`` equals an ``allowed`` entry after normalization."""
    key = origin_key(origin)
    if key is None:
        return False
    return any(origin_key(entry) == key for entry in allowed)


def host_header_key(host_header: str | None, scheme: str) -> tuple[str, int | None] | None:
    """Parse a ``Host`` header into ``(host, port)`` using ``scheme``'s default port."""
    raw = (host_header or "").strip()
    if not raw:
        return None
    try:
        parts = urlsplit(f"//{raw}")
        host = (parts.hostname or "").rstrip(".")
        port = parts.port
    except ValueError:
        return None
    if not host:
        return None
    if port is None:
        port = _DEFAULT_PORTS.get(scheme)
    return (host, port)


def _ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        address = ipaddress.ip_address(value.strip("[]").split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def is_ip_literal(host: str) -> bool:
    return _ip(host) is not None


def is_private_host(host: str) -> bool:
    """Loopback, a private/link-local/tailnet address, or a ``.local``/``.ts.net`` name."""
    value = host.strip().lower().rstrip(".")
    if not value:
        return False
    if value == "localhost" or is_loopback_address(value):
        return True
    address = _ip(value)
    if address is not None:
        return address.is_loopback or any(address in net for net in _PRIVATE_NETWORKS)
    return any(value.endswith(suffix) for suffix in PRIVATE_NAME_SUFFIXES)


def is_wildcard_bind(host: str | None) -> bool:
    return (host or "").strip() in WILDCARD_BIND_ADDRESSES


def bind_requires_origin_policy(host: str | None) -> bool:
    """True for a bind no browser can reach through the private same-origin rule.

    A specific public address or a public DNS name: every browser that reaches
    it arrives with a public origin, so without an allowlisted origin the
    Control UI could never connect. Loopback, wildcard and private binds are
    served by rules 3 and 4.
    """
    value = (host or "").strip()
    if is_wildcard_bind(value) or is_loopback_bind(value):
        return False
    return not is_private_host(value)


def request_is_forwarded(headers: Mapping[str, str] | Any) -> bool:
    """True when proxy forwarding headers are present."""
    getter = getattr(headers, "get", None)
    if getter is None:
        return False
    return any(getter(name) for name in _FORWARDING_HEADERS)


@dataclass(frozen=True, slots=True)
class OriginDecision:
    allowed: bool
    #: ``no-origin`` | ``allowlist`` | ``private-same-origin`` | ``loopback-local``
    #: | ``malformed`` | ``denied``
    reason: str


@dataclass(frozen=True)
class OriginPolicy:
    """The resolved policy for one listener posture.

    Built once when the app is assembled: ``config.apply`` mutates the live
    config in place without rebinding the socket, so the bind posture and the
    allowlist must not be re-read per request.
    """

    allowed_origins: tuple[str, ...] = ()
    bind_is_loopback: bool = True
    _keys: frozenset[OriginKey] = field(default=frozenset(), repr=False, compare=False)

    def __post_init__(self) -> None:
        keys = frozenset(
            key for key in (origin_key(entry) for entry in self.allowed_origins) if key
        )
        object.__setattr__(self, "_keys", keys)

    def evaluate(
        self,
        origin: str | None,
        *,
        host_header: str | None = None,
        peer_ip: str | None = None,
        forwarded: bool = False,
    ) -> OriginDecision:
        if not origin:
            return OriginDecision(True, "no-origin")
        key = origin_key(origin)
        if key is None:
            return OriginDecision(False, "malformed")
        if key in self._keys:
            return OriginDecision(True, "allowlist")
        scheme, origin_host, origin_port = key
        host = host_header_key(host_header, scheme)
        if host is not None and host == (origin_host, origin_port):
            same_origin_ok = (
                is_loopback_bind(origin_host)
                if self.bind_is_loopback
                else is_private_host(origin_host)
            )
            if same_origin_ok:
                return OriginDecision(True, "private-same-origin")
        if is_loopback_bind(origin_host) and not forwarded:
            # A loopback-bound socket only ever has loopback peers; on any
            # other bind the transport peer has to be loopback itself.
            if self.bind_is_loopback or is_loopback_address(peer_ip):
                return OriginDecision(True, "loopback-local")
        return OriginDecision(False, "denied")

    def allows(
        self,
        origin: str | None,
        *,
        host_header: str | None = None,
        peer_ip: str | None = None,
        forwarded: bool = False,
    ) -> bool:
        return self.evaluate(
            origin, host_header=host_header, peer_ip=peer_ip, forwarded=forwarded
        ).allowed

    def allowed_hostnames(self) -> frozenset[str]:
        """Hostnames of every allowlisted origin, for the Host guard."""
        return frozenset(key[1] for key in self._keys)

    def host_allowed(self, host: str) -> bool:
        """Whether a ``Host`` header names this gateway (DNS-rebinding guard).

        On a loopback bind: loopback hosts and allowlisted names. Otherwise
        additionally any IP literal (rebinding needs a hostname) and private
        names. Everything else is a name the operator never declared.
        """
        value = host.strip().lower().rstrip(".")
        if not value:
            return False
        if is_loopback_address(value) or value == "localhost" or value in self.allowed_hostnames():
            return True
        if self.bind_is_loopback:
            return False
        return is_ip_literal(value) or is_private_host(value)


def _scheme(config: Any) -> str:
    tls = getattr(config, "tls", None)
    return "https" if getattr(tls, "keyfile", "") and getattr(tls, "certfile", "") else "http"


def seeded_local_origins(port: int, scheme: str = "http") -> tuple[str, ...]:
    """The gateway's own loopback origins, so a LAN bind never locks out the local browser."""
    return tuple(f"{scheme}://{host}:{port}" for host in ("localhost", "127.0.0.1", "[::1]"))


def configured_origins(config: Any) -> list[str]:
    """Operator-declared origins: ``control_ui``, explicit ``cors`` entries, ``public_origin``."""
    origins: list[str] = []
    control_ui = getattr(config, "control_ui", None)
    origins.extend(str(o) for o in (getattr(control_ui, "allowed_origins", None) or []))
    cors = getattr(config, "cors", None)
    origins.extend(str(o) for o in (getattr(cors, "allowed_origins", None) or []) if o != "*")
    gateway = getattr(config, "gateway", None)
    public_origin = str(getattr(gateway, "public_origin", "") or "").strip()
    if public_origin:
        origins.append(public_origin)
    return origins


def build_origin_policy(config: Any, *, bind_is_loopback: bool | None = None) -> OriginPolicy:
    """Resolve the policy for ``config``'s listener."""
    if bind_is_loopback is None:
        bind_is_loopback = is_loopback_bind(getattr(config, "host", None))
    port = int(getattr(config, "port", 0) or 0)
    origins = configured_origins(config)
    if port:
        origins.extend(seeded_local_origins(port, _scheme(config)))
    deduped = tuple(dict.fromkeys(origins))
    return OriginPolicy(allowed_origins=deduped, bind_is_loopback=bind_is_loopback)


def warn_unparseable_origins(config: Any) -> None:
    """Name every allowlist entry that can never match anything."""
    for origin in configured_origins(config):
        if origin_key(origin) is None:
            log.warning(
                "gateway.allowed_origin_unparseable",
                origin=origin,
                hint="origin entries must be full origins: scheme://host[:port], "
                "e.g. https://agent.example.com",
            )


def enforce_public_bind_origin_guard(config: Any) -> None:
    """Refuse a Control UI on a public bind that no browser origin could reach.

    On a specific public address or DNS name, rule 3 never applies, so the
    console would load and then fail every request. Ask for the origin the
    operator will actually open instead of starting a UI that cannot work.
    A ``GatewayConfigError``: only a config change fixes it, so ``gateway
    run`` exits ``EX_CONFIG`` and a supervisor does not just restart.
    """
    from agentos.gateway.config import GatewayConfigError

    host = getattr(config, "host", None)
    control_ui = getattr(config, "control_ui", None)
    if not getattr(control_ui, "enabled", True) or not bind_requires_origin_policy(host):
        return
    declared = [o for o in configured_origins(config) if origin_key(o) is not None]
    if declared:
        return
    raise GatewayConfigError(
        f"Refusing to serve the Control UI on the public bind {host!r} without an origin "
        "policy: every browser reaching it sends a public Origin, which the gateway refuses "
        "unless it is declared. Fix one of these: "
        '(1) set [gateway] public_origin = "https://<the address you open>" in agentos.toml; '
        "(2) list it in control_ui.allowed_origins; "
        "(3) disable the console with control_ui.enabled = false (the API keeps working); "
        "(4) bind a private address instead (--bind-mode lan or --bind-mode tailnet)."
    )


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------

_CORS_SCOPE: ContextVar[Scope | None] = ContextVar("agentos_cors_scope", default=None)


class OriginPolicyCORSMiddleware(CORSMiddleware):
    """Starlette's CORS handling with ``OriginPolicy`` deciding the origin.

    The answer depends on the request (its ``Host`` and transport peer), which
    Starlette's ``is_allowed_origin(origin)`` hook does not receive, so the
    scope travels in a context variable for the duration of the call. An
    allowed origin is reflected explicitly; nothing is ever answered with
    ``Access-Control-Allow-Origin: *``, credentials or not.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        policy: OriginPolicy,
        allow_credentials: bool = True,
        allow_methods: Sequence[str] = ("GET",),
        allow_headers: Sequence[str] = (),
    ) -> None:
        super().__init__(
            app,
            allow_origins=(),
            allow_methods=allow_methods,
            allow_headers=allow_headers,
            allow_credentials=allow_credentials,
        )
        self._policy = policy

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        token = _CORS_SCOPE.set(scope)
        try:
            await super().__call__(scope, receive, send)
        finally:
            _CORS_SCOPE.reset(token)

    def is_allowed_origin(self, origin: str) -> bool:
        scope = _CORS_SCOPE.get()
        if scope is None:
            return False
        headers = Headers(scope=scope)
        client = scope.get("client")
        return self._policy.allows(
            origin,
            host_header=headers.get("host"),
            peer_ip=client[0] if client else None,
            forwarded=request_is_forwarded(headers),
        )


__all__ = [
    "PRIVATE_NAME_SUFFIXES",
    "OriginDecision",
    "OriginPolicy",
    "OriginPolicyCORSMiddleware",
    "bind_requires_origin_policy",
    "build_origin_policy",
    "configured_origins",
    "enforce_public_bind_origin_guard",
    "host_header_key",
    "is_ip_literal",
    "is_private_host",
    "is_wildcard_bind",
    "origin_in_allowlist",
    "origin_key",
    "request_is_forwarded",
    "seeded_local_origins",
    "warn_unparseable_origins",
]
