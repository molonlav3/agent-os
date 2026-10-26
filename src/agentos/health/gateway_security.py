"""Doctor findings for the gateway's network exposure and admission posture.

Each finding names one combination that leaves the Control surface (chat,
tools, sessions, config — with the operator's provider credentials) easier
to reach than the operator likely meant, and the exact command that fixes
it. ``docs/remote-access.md`` describes the supported ways to reach a
gateway from another machine. The payload comes from
``gateway.rpc_doctor.gateway_security_payload``.
"""

from __future__ import annotations

from typing import Any

from agentos.health.model import FixStep, HealthFinding

#: Shortest gateway token doctor accepts without a warning. A generated
#: token (``secrets.token_urlsafe(32)``) is 43 characters.
MIN_TOKEN_CHARS = 24

_SURFACE = "gateway"
_REMOTE_ACCESS_DOC = "docs/remote-access.md"


def _has_wildcard(origins: list[Any]) -> bool:
    return any(isinstance(origin, str) and "*" in origin for origin in origins)


def evaluate_gateway_security(payload: dict[str, Any]) -> list[HealthFinding]:
    host = str(payload.get("host") or "")
    loopback = bool(payload.get("loopbackBind", True))
    mode = str(payload.get("authMode") or "none")
    token_length = int(payload.get("tokenLength") or 0)
    trusted_proxy = str(payload.get("trustedProxy") or "").strip()
    rate_limit_on = bool(payload.get("authRateLimitEnabled", True))
    allowed_origins = list(payload.get("allowedOrigins") or [])
    cors_origins = list(payload.get("corsAllowedOrigins") or [])
    cors_credentials = bool(payload.get("corsAllowCredentials", False))
    evidence = {"host": host, "loopbackBind": loopback, "authMode": mode}
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
                        command="agentos config set auth.mode token",
                        detail="A token is generated at startup when none is set.",
                    ),
                    FixStep(
                        label="Or bind loopback and tunnel in",
                        command="agentos config set host 127.0.0.1",
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
                        command=(
                            "agentos config set control_ui.allowed_origins "
                            "'[\"https://agent.example.com\"]'"
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
                severity="warn",
                surface=_SURFACE,
                title="CORS allows every origin with credentials on a non-loopback bind",
                detail=(
                    'cors.allowed_origins = ["*"] with cors.allow_credentials = true makes '
                    "the gateway reflect any page's Origin with credentials. On a loopback "
                    "bind the origin guard fences this; on this bind nothing does."
                ),
                evidence={
                    **evidence,
                    "corsAllowedOrigins": cors_origins,
                    "corsAllowCredentials": cors_credentials,
                },
                fix_steps=[
                    FixStep(
                        label="Allow only the console origin",
                        command=(
                            "agentos config set cors.allowed_origins "
                            "'[\"https://agent.example.com\"]'"
                        ),
                    ),
                    FixStep(
                        label="Or stop sending credentials cross-origin",
                        command="agentos config set cors.allow_credentials false",
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
                        command="agentos config set auth.trusted_proxy 127.0.0.1",
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
                        command="agentos config set auth.rate_limit.enabled true",
                    )
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
                    "random characters (a generated one has 43)."
                ),
                evidence={**evidence, "tokenLength": token_length},
                fix_steps=[
                    FixStep(
                        label="Rotate to a long random token",
                        command=(
                            "agentos config set auth.token "
                            "\"$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')\""
                        ),
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
