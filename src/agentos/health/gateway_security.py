"""Doctor findings for the gateway's network exposure and admission posture.

Each finding names one combination that leaves the Control surface (chat,
tools, sessions, config — with the operator's provider credentials) easier
to reach than the operator likely meant, and the exact command that fixes
it. ``docs/remote-access.md`` describes the supported ways to reach a
gateway from another machine. The payload comes from
``gateway.exposure.gateway_security_payload``; it only needs the config, so
``agentos doctor`` evaluates it from a running gateway and, when none
answers, from the local config file.

Every fix command writes the config file (``agentos config set … --config
<path>``): without ``--config`` that command only prints an ``export`` line,
which changes nothing for a gateway started elsewhere.
"""

from __future__ import annotations

from typing import Any

from agentos.cli_quoting import config_cli_arg
from agentos.health.model import FixStep, HealthFinding

#: Shortest gateway token doctor accepts without a warning. A generated
#: token (``secrets.token_hex(32)``) is 64 characters.
MIN_TOKEN_CHARS = 24

_SURFACE = "gateway"
_REMOTE_ACCESS_DOC = "docs/remote-access.md"
_NEW_TOKEN = "\"$(python3 -c 'import secrets; print(secrets.token_hex(32))')\""


def _has_wildcard(origins: list[Any]) -> bool:
    return any(isinstance(origin, str) and "*" in origin for origin in origins)


def _set(key: str, value: str, config_path: str) -> str:
    """``agentos config set`` that writes ``config_path`` (not an env export)."""
    if config_path:
        return f"agentos config set {key} {value}{config_cli_arg(config_path)}"
    return f"agentos config set {key} {value}"


def evaluate_gateway_security(payload: dict[str, Any]) -> list[HealthFinding]:
    host = str(payload.get("host") or "")
    loopback = bool(payload.get("loopbackBind", True))
    mode = str(payload.get("authMode") or "none")
    token_length = int(payload.get("tokenLength") or 0)
    trusted_proxy = str(payload.get("trustedProxy") or "").strip()
    trusted_proxy_covers_loopback = bool(payload.get("trustedProxyCoversLoopback", False))
    rate_limit_on = bool(payload.get("authRateLimitEnabled", True))
    exempt_loopback = bool(payload.get("authRateLimitExemptLoopback", True))
    allowed_origins = list(payload.get("allowedOrigins") or [])
    proxied_origins = list(payload.get("proxiedOrigins") or [])
    cors_origins = list(payload.get("corsAllowedOrigins") or [])
    cors_credentials = bool(payload.get("corsAllowCredentials", False))
    config_path = str(payload.get("configPath") or "")
    evidence = {"host": host, "loopbackBind": loopback, "authMode": mode}
    if config_path:
        evidence["configPath"] = config_path
    findings: list[HealthFinding] = []

    if not loopback and mode == "none":
        findings.append(
            HealthFinding(
                id="gateway.bind_no_auth",
                severity="error",
                surface=_SURFACE,
                title="Gateway listens beyond loopback without authentication",
                detail=(
                    f'The gateway binds {host!r} with auth.mode = "none": anyone who can '
                    "reach the port gets chat, tools, sessions and config. It refuses to "
                    "start like this; fix the config before the next start."
                ),
                evidence=evidence,
                fix_steps=[
                    FixStep(
                        label="Enforce a token",
                        command=_set("auth.mode", "token", config_path),
                        detail="A token is generated at startup when none is set.",
                    ),
                    FixStep(
                        label="Or bind loopback and tunnel in",
                        command=_set("host", "127.0.0.1", config_path),
                        detail=f"SSH tunnel or Tailscale Serve: see {_REMOTE_ACCESS_DOC}.",
                    ),
                ],
                restart_required=True,
            )
        )

    if _has_wildcard(allowed_origins):
        findings.append(
            HealthFinding(
                id="gateway.allowed_origins_wildcard",
                severity="warn",
                surface=_SURFACE,
                title="control_ui.allowed_origins contains a wildcard",
                detail=(
                    "Origins are compared exactly; a wildcard entry matches nothing, and "
                    "allowing every browser origin would let any page you visit drive the "
                    "gateway. List each origin the console is served from."
                ),
                evidence={**evidence, "allowedOrigins": allowed_origins},
                fix_steps=[
                    FixStep(
                        label="List the console origin explicitly",
                        command=_set(
                            "control_ui.allowed_origins",
                            "'[\"https://agent.example.com\"]'",
                            config_path,
                        ),
                    )
                ],
                restart_required=True,
            )
        )

    if not loopback and cors_credentials and _has_wildcard(cors_origins):
        findings.append(
            HealthFinding(
                id="gateway.cors_wildcard_with_credentials",
                severity="info",
                surface=_SURFACE,
                title="cors.allowed_origins lists a wildcard, which allows nothing",
                detail=(
                    'cors.allowed_origins = ["*"] with cors.allow_credentials = true reads '
                    "as if any page's Origin were reflected with credentials. It is not: on "
                    "every bind the gateway answers CORS only for origins its origin policy "
                    "admits (declared origins, a private address opened as itself, its own "
                    "localhost), never with *. List the console origin so the config says "
                    "what the gateway does."
                ),
                evidence={
                    **evidence,
                    "corsAllowedOrigins": cors_origins,
                    "corsAllowCredentials": cors_credentials,
                },
                fix_steps=[
                    FixStep(
                        label="Allow only the console origin",
                        command=_set(
                            "cors.allowed_origins",
                            "'[\"https://agent.example.com\"]'",
                            config_path,
                        ),
                    ),
                    FixStep(
                        label="Or stop sending credentials cross-origin",
                        command=_set("cors.allow_credentials", "false", config_path),
                    ),
                ],
                restart_required=True,
            )
        )

    if mode == "trusted-proxy" and not trusted_proxy:
        findings.append(
            HealthFinding(
                id="gateway.trusted_proxy_unset",
                severity="error",
                surface=_SURFACE,
                title="trusted-proxy mode with no trusted proxy",
                detail=(
                    'auth.mode = "trusted-proxy" admits only requests whose transport peer '
                    "is listed in auth.trusted_proxy. The list is empty, so every request is "
                    "refused."
                ),
                evidence=evidence,
                fix_steps=[
                    FixStep(
                        label="Name the reverse proxy's address (IP or CIDR)",
                        command=_set("auth.trusted_proxy", "127.0.0.1", config_path),
                        detail=f"See the reverse-proxy recipe in {_REMOTE_ACCESS_DOC}.",
                    )
                ],
                restart_required=True,
            )
        )

    if mode == "token" and not loopback and not rate_limit_on:
        findings.append(
            HealthFinding(
                id="gateway.auth_rate_limit_off",
                severity="warn",
                surface=_SURFACE,
                title="Failed-token rate limiting is off on a non-loopback bind",
                detail=(
                    "With auth.rate_limit.enabled = false a wrong token is only logged, so the "
                    "gateway token can be guessed at network speed."
                ),
                evidence=evidence,
                fix_steps=[
                    FixStep(
                        label="Turn rate limiting back on",
                        command=_set("auth.rate_limit.enabled", "true", config_path),
                    )
                ],
                restart_required=True,
            )
        )

    if (
        mode == "token"
        and loopback
        and rate_limit_on
        and exempt_loopback
        and proxied_origins
        and not trusted_proxy_covers_loopback
    ):
        findings.append(
            HealthFinding(
                id="gateway.rate_limit_loopback_exempt_behind_proxy",
                severity="warn",
                surface=_SURFACE,
                title="Remote clients behind a same-host proxy are never locked out",
                detail=(
                    "control_ui.allowed_origins names a non-loopback origin, so a proxy on "
                    "this host (Tailscale Serve, Caddy, nginx) publishes the loopback "
                    "gateway. Every client it forwards connects from 127.0.0.1, and "
                    "loopback is exempt from the failed-token lockout: a script behind the "
                    "proxy can guess the token without ever being locked out."
                ),
                evidence={**evidence, "proxiedOrigins": proxied_origins},
                fix_steps=[
                    FixStep(
                        label="Trust the local proxy so its X-Forwarded-For names the client",
                        command=_set("auth.trusted_proxy", "127.0.0.1", config_path),
                    ),
                    FixStep(
                        label="Or lock out loopback too",
                        command=_set("auth.rate_limit.exempt_loopback", "false", config_path),
                    ),
                ],
                restart_required=True,
            )
        )

    if mode == "token" and token_length < MIN_TOKEN_CHARS:
        findings.append(
            HealthFinding(
                id="gateway.token_short",
                severity="warn",
                surface=_SURFACE,
                title="Gateway token is short",
                detail=(
                    f"The token has {token_length} characters; use at least {MIN_TOKEN_CHARS} "
                    "random characters."
                ),
                evidence={**evidence, "tokenLength": token_length},
                fix_steps=[
                    FixStep(
                        label="Rotate to a long random token",
                        command=_set("auth.token", _NEW_TOKEN, config_path),
                        detail="Then restart the gateway and update every client's token.",
                    )
                ],
                restart_required=True,
            )
        )

    if not findings:
        findings.append(
            HealthFinding(
                id="gateway.security.ok",
                severity="ok",
                surface=_SURFACE,
                title="Gateway exposure checks passed",
                detail=(
                    "Loopback bind."
                    if loopback
                    else f"Non-loopback bind {host!r} with auth.mode = {mode!r}."
                ),
                evidence=evidence,
            )
        )
    return findings


__all__ = ["MIN_TOKEN_CHARS", "evaluate_gateway_security"]
