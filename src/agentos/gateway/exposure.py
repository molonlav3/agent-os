"""What ``agentos doctor`` needs to judge the gateway's network exposure.

Built from a ``GatewayConfig`` alone, so the same checks run against a live
gateway (``doctor.status``) and against the local config file when no
gateway answers (``agentos doctor`` offline). Never carries a secret: the
token is reduced to its length.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from agentos.gateway.access import is_loopback_address, is_loopback_bind, peer_is_trusted_proxy


def _proxied_origins(origins: list[Any]) -> list[str]:
    """Allowed browser origins that are not loopback: served through a proxy."""
    proxied: list[str] = []
    for origin in origins:
        if not isinstance(origin, str) or "*" in origin:
            continue
        try:
            host = urlsplit(origin.strip()).hostname or ""
        except ValueError:
            continue
        if host and host != "localhost" and not is_loopback_address(host):
            proxied.append(origin)
    return proxied


def effective_config_path(config: Any) -> str:
    """The file a fix should write: the loaded one, else the default user config."""
    path = getattr(config, "config_path", None)
    if isinstance(path, str) and path:
        return path
    from agentos.paths import default_agentos_home

    return str(default_agentos_home() / "config.toml")


def gateway_security_payload(config: Any) -> dict[str, Any]:
    auth = getattr(config, "auth", None)
    rate_limit = getattr(auth, "rate_limit", None)
    cors = getattr(config, "cors", None)
    control_ui = getattr(config, "control_ui", None)
    host = str(getattr(config, "host", "") or "")
    token = getattr(auth, "token", None)
    trusted_proxy = str(getattr(auth, "trusted_proxy", "") or "")
    allowed_origins = list(getattr(control_ui, "allowed_origins", []) or [])
    return {
        "host": host,
        "loopbackBind": is_loopback_bind(host),
        "authMode": str(getattr(auth, "mode", "none") or "none"),
        "tokenLength": len(token) if isinstance(token, str) else 0,
        "trustedProxy": trusted_proxy,
        "trustedProxyCoversLoopback": peer_is_trusted_proxy(trusted_proxy, "127.0.0.1")
        or peer_is_trusted_proxy(trusted_proxy, "::1"),
        "authRateLimitEnabled": bool(getattr(rate_limit, "enabled", True)),
        "authRateLimitExemptLoopback": bool(getattr(rate_limit, "exempt_loopback", True)),
        "allowedOrigins": allowed_origins,
        "proxiedOrigins": _proxied_origins(allowed_origins),
        "corsAllowedOrigins": list(getattr(cors, "allowed_origins", []) or []),
        "corsAllowCredentials": bool(getattr(cors, "allow_credentials", False)),
        "configPath": effective_config_path(config),
    }


__all__ = ["effective_config_path", "gateway_security_payload"]
