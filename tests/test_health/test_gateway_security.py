"""Doctor findings for gateway exposure: bind, origins, CORS, proxy, rate limit, token."""

from __future__ import annotations

from typing import Any

import pytest

from agentos.gateway.config import (
    AuthConfig,
    AuthRateLimitConfig,
    ControlUiConfig,
    CorsConfig,
    GatewayConfig,
)
from agentos.gateway.exposure import gateway_security_payload
from agentos.health.gateway_security import MIN_TOKEN_CHARS, evaluate_gateway_security

_LONG_TOKEN = "t" * 43


def _ids(config: GatewayConfig) -> set[str]:
    return {f.id for f in evaluate_gateway_security(gateway_security_payload(config))}


def _public(**kwargs: Any) -> GatewayConfig:
    kwargs.setdefault("auth", AuthConfig(mode="token", token=_LONG_TOKEN))
    kwargs.setdefault("cors", CorsConfig(allowed_origins=["https://ui.example"]))
    return GatewayConfig(host="0.0.0.0", **kwargs)


def test_default_loopback_gateway_is_clean() -> None:
    assert _ids(GatewayConfig()) == {"gateway.security.ok"}


def test_well_configured_public_gateway_is_clean() -> None:
    assert _ids(_public()) == {"gateway.security.ok"}


def test_bind_no_auth() -> None:
    config = GatewayConfig.model_construct(
        host="0.0.0.0",
        auth=AuthConfig(mode="none"),
        cors=CorsConfig(allowed_origins=["https://ui.example"]),
        control_ui=ControlUiConfig(),
    )
    findings = evaluate_gateway_security(gateway_security_payload(config))
    bind = next(f for f in findings if f.id == "gateway.bind_no_auth")
    assert bind.severity == "error"
    assert any(
        (step.command or "").startswith("agentos config set auth.mode token --config ")
        for step in bind.fix_steps
    )


def test_allowed_origins_wildcard() -> None:
    config = GatewayConfig(control_ui=ControlUiConfig(allowed_origins=["*"]))
    assert "gateway.allowed_origins_wildcard" in _ids(config)


def test_cors_wildcard_with_credentials_only_matters_off_loopback() -> None:
    public = _public(cors=CorsConfig(allowed_origins=["*"], allow_credentials=True))
    assert "gateway.cors_wildcard_with_credentials" in _ids(public)
    no_credentials = _public(cors=CorsConfig(allowed_origins=["*"], allow_credentials=False))
    assert "gateway.cors_wildcard_with_credentials" not in _ids(no_credentials)
    # The shipped default on loopback is fenced by the origin guard.
    assert "gateway.cors_wildcard_with_credentials" not in _ids(GatewayConfig())


def test_cors_wildcard_is_reported_as_inert_not_as_a_risk() -> None:
    """The origin policy answers CORS only for admitted origins on every bind,
    so a ``*`` entry allows nothing; doctor must not claim otherwise."""
    public = _public(cors=CorsConfig(allowed_origins=["*"], allow_credentials=True))
    findings = evaluate_gateway_security(gateway_security_payload(public))
    finding = next(f for f in findings if f.id == "gateway.cors_wildcard_with_credentials")
    assert finding.severity == "info"
    assert "nothing does" not in finding.detail
    assert "origin policy" in finding.detail


def test_trusted_proxy_unset() -> None:
    config = _public(auth=AuthConfig(mode="trusted-proxy"))
    findings = evaluate_gateway_security(gateway_security_payload(config))
    unset = next(f for f in findings if f.id == "gateway.trusted_proxy_unset")
    assert unset.severity == "error"
    assert _ids(_public(auth=AuthConfig(mode="trusted-proxy", trusted_proxy="10.0.0.0/8"))) == {
        "gateway.security.ok"
    }


def test_auth_rate_limit_off_on_a_public_bind() -> None:
    off = AuthConfig(mode="token", token=_LONG_TOKEN, rate_limit=AuthRateLimitConfig(enabled=False))
    assert "gateway.auth_rate_limit_off" in _ids(_public(auth=off))
    assert "gateway.auth_rate_limit_off" not in _ids(GatewayConfig(auth=off))


@pytest.mark.parametrize(("length", "short"), [(8, True), (MIN_TOKEN_CHARS - 1, True), (24, False)])
def test_token_short(length: int, short: bool) -> None:
    config = GatewayConfig(auth=AuthConfig(mode="token", token="x" * length))
    assert ("gateway.token_short" in _ids(config)) is short


def test_payload_never_carries_the_token() -> None:
    payload = gateway_security_payload(_public())
    assert _LONG_TOKEN not in repr(payload)
    assert payload["tokenLength"] == len(_LONG_TOKEN)


def test_every_finding_has_a_fix_step() -> None:
    config = GatewayConfig.model_construct(
        host="0.0.0.0",
        auth=AuthConfig.model_construct(
            mode="token", token="short", rate_limit=AuthRateLimitConfig(enabled=False)
        ),
        cors=CorsConfig(allowed_origins=["*"], allow_credentials=True),
        control_ui=ControlUiConfig(allowed_origins=["*"]),
    )
    findings = evaluate_gateway_security(gateway_security_payload(config))
    assert {f.id for f in findings} == {
        "gateway.allowed_origins_wildcard",
        "gateway.cors_wildcard_with_credentials",
        "gateway.auth_rate_limit_off",
        "gateway.token_short",
    }
    assert all(f.fix_steps and all(step.command for step in f.fix_steps) for f in findings)


async def test_doctor_status_reports_gateway_findings() -> None:
    from agentos.gateway.rpc import RpcContext, get_dispatcher

    config = GatewayConfig(auth=AuthConfig(mode="token", token="short"))
    result = await get_dispatcher().dispatch(
        "d", "doctor.status", {"deep": False}, RpcContext(conn_id="t", config=config)
    )
    assert result.ok, result.error
    ids = {finding["id"] for finding in result.payload["findings"]}
    assert "gateway.token_short" in ids


def test_fix_commands_write_the_config_file(tmp_path) -> None:
    """``config set`` without ``--config`` only prints an export line."""
    path = tmp_path / "my config.toml"
    config = GatewayConfig.model_construct(
        host="0.0.0.0",
        auth=AuthConfig.model_construct(
            mode="token", token="short", rate_limit=AuthRateLimitConfig(enabled=False)
        ),
        cors=CorsConfig(allowed_origins=["*"], allow_credentials=True),
        control_ui=ControlUiConfig(allowed_origins=["*"]),
        config_path=str(path),
    )
    findings = evaluate_gateway_security(gateway_security_payload(config))
    commands = [step.command for f in findings for step in f.fix_steps if step.command]
    assert commands
    assert all(f"--config '{path}'" in command for command in commands), commands


def test_fix_commands_default_to_the_user_config_without_a_loaded_file() -> None:
    from agentos.paths import default_agentos_home

    findings = evaluate_gateway_security(
        gateway_security_payload(GatewayConfig(auth=AuthConfig(mode="token", token="x")))
    )
    command = findings[0].fix_steps[0].command or ""
    assert str(default_agentos_home() / "config.toml") in command


def test_loopback_exempt_behind_a_same_host_proxy() -> None:
    proxied = ControlUiConfig(allowed_origins=["https://box.tail1234.ts.net"])
    config = GatewayConfig(auth=AuthConfig(mode="token", token=_LONG_TOKEN), control_ui=proxied)
    assert "gateway.rate_limit_loopback_exempt_behind_proxy" in _ids(config)
    # Trusting the local proxy (X-Forwarded-For names the client) fixes it...
    trusted = AuthConfig(mode="token", token=_LONG_TOKEN, trusted_proxy="127.0.0.1")
    assert "gateway.rate_limit_loopback_exempt_behind_proxy" not in _ids(
        GatewayConfig(auth=trusted, control_ui=proxied)
    )
    # ...and so does locking out loopback too.
    strict = AuthConfig(
        mode="token",
        token=_LONG_TOKEN,
        rate_limit=AuthRateLimitConfig(exempt_loopback=False),
    )
    assert "gateway.rate_limit_loopback_exempt_behind_proxy" not in _ids(
        GatewayConfig(auth=strict, control_ui=proxied)
    )
    # Loopback-only origins mean no proxy: nothing to report.
    local = ControlUiConfig(allowed_origins=["http://localhost:5173"])
    assert "gateway.rate_limit_loopback_exempt_behind_proxy" not in _ids(
        GatewayConfig(auth=AuthConfig(mode="token", token=_LONG_TOKEN), control_ui=local)
    )


@pytest.mark.parametrize(
    ("toml", "finding"),
    [
        ('host = "0.0.0.0"\n[auth]\nmode = "none"\n', "gateway.bind_no_auth"),
        ('[auth]\nmode = "trusted-proxy"\n', "gateway.trusted_proxy_unset"),
        ('[auth]\nmode = "token"\ntoken = "short"\n', "gateway.token_short"),
    ],
)
def test_offline_doctor_reports_exposure_from_the_config_file(
    tmp_path, toml: str, finding: str
) -> None:
    """The findings a gateway cannot report itself (it will not start, or
    admits nobody) come from the local config when no gateway answers."""
    from agentos.cli.doctor_cmd import _offline_report

    path = tmp_path / "agentos.toml"
    path.write_text(toml, encoding="utf-8")
    report = _offline_report(
        ConnectionError("connection refused"),
        gateway_url="ws://127.0.0.1:19999/ws",
        config_path=path,
    )
    ids = {item["id"] for item in report["findings"]}
    assert finding in ids
    assert "gateway.security.ok" not in ids
