"""Two-version gateway status: CLI + gateway version, mismatch diagnostic."""

from __future__ import annotations

import json
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

import agentos
from agentos.cli import gateway_cmd
from agentos.cli.gateway_lifecycle import GatewayLifecycleResult

runner = CliRunner()


def _app() -> typer.Typer:
    app = typer.Typer()
    app.command("status")(gateway_cmd.status_gateway)

    @app.command("noop")
    def _noop() -> None:  # keeps Typer in multi-command mode
        return None

    return app


class _FakeManager:
    def __init__(self, state: str) -> None:
        self._state = state

    def status(self) -> GatewayLifecycleResult:
        return GatewayLifecycleResult(
            action="status", state=self._state, ok=True, managed=True, port=18791
        )


def _install(
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    gateway_version: str | None,
    runtime: dict[str, Any] | None = None,
) -> None:
    monkeypatch.setattr(gateway_cmd, "_lifecycle_manager", lambda **_: _FakeManager(state))
    monkeypatch.setattr(
        gateway_cmd, "gateway_handshake_version", lambda **_: gateway_version
    )
    monkeypatch.setattr(gateway_cmd, "gateway_runtime_probe", lambda **_: runtime)


def test_status_shows_both_versions_matching(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, "running", agentos.__version__)
    result = runner.invoke(_app(), ["status"])
    assert result.exit_code == 0
    assert f"CLI version:     {agentos.__version__}" in result.output
    assert f"Gateway version: {agentos.__version__}" in result.output
    assert "Version mismatch" not in result.output


def test_status_flags_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, "running", "0.0.1")
    result = runner.invoke(_app(), ["status"])
    assert result.exit_code == 0
    assert "Gateway version: 0.0.1" in result.output
    assert "Version mismatch" in result.output
    assert "gateway restart" in result.output


def test_status_not_running_reports_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        gateway_cmd,
        "_lifecycle_manager",
        lambda **_: _FakeManager("not_started"),
    )
    calls: dict[str, Any] = {"handshake": 0}

    def _hs(**_: Any) -> None:
        calls["handshake"] += 1
        return None

    monkeypatch.setattr(gateway_cmd, "gateway_handshake_version", _hs)
    result = runner.invoke(_app(), ["status"])
    assert result.exit_code == 0
    assert "unknown (not running" in result.output
    # No handshake attempted for a non-running gateway.
    assert calls["handshake"] == 0


def test_status_json_carries_both_versions(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, "running", "0.0.1")
    result = runner.invoke(_app(), ["status", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["cliVersion"] == agentos.__version__
    assert payload["gatewayVersion"] == "0.0.1"
    assert payload["versionMismatch"] is True


def test_status_json_carries_the_supervisor_numbers(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = {
        "ready": True,
        "state": "safe_mode",
        "failing": ["safe_mode"],
        "safe_mode": {"active": True, "reason": "3 unclean gateway boots in the last 300s"},
        "drain_budget_ms": 32_000,
        "active_turns": 2,
    }
    _install(monkeypatch, "running", agentos.__version__, runtime)
    result = runner.invoke(_app(), ["status", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    # Top-level ``state`` stays the lifecycle state; the gateway's own state
    # is ``runtime.state``.
    assert payload["state"] == "running"
    assert payload["drain_budget_ms"] == 32_000
    assert payload["active_turns"] == 2
    assert payload["runtime"]["state"] == "safe_mode"
    assert payload["runtime"]["failing"] == ["safe_mode"]
    assert payload["runtime"]["safe_mode"]["active"] is True


def test_status_json_without_a_gateway_still_reports_the_drain_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gateway_cmd, "_lifecycle_manager", lambda **_: _FakeManager("not_started"))
    result = runner.invoke(_app(), ["status", "--json"])
    payload = json.loads(result.output)
    assert payload["drain_budget_ms"] == 32_000
    assert payload["active_turns"] is None
    assert payload["runtime"] is None


def test_status_text_names_safe_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = {
        "ready": True,
        "state": "safe_mode",
        "failing": ["safe_mode"],
        "safe_mode": {"active": True, "reason": "3 unclean", "clears_at": "2026-10-26T10:05:00Z"},
        "drain_budget_ms": 32_000,
        "active_turns": 0,
    }
    _install(monkeypatch, "running", agentos.__version__, runtime)
    result = runner.invoke(_app(), ["status"])
    assert "Gateway state:   safe_mode (active turns: 0), drain budget 32s" in result.output
    assert "agentos channels start --yes" in result.output
