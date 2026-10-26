"""CLI side of the supervisor contract (#3682): exit 78, handshake retry, channels start."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from agentos.cli import gateway_client as gcm
from agentos.cli import gateway_cmd
from agentos.cli.main import app
from agentos.gateway.config import GatewayConfigError

runner = CliRunner()


def test_gateway_run_exits_78_on_a_config_file_that_is_not_toml(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text("[tools\nprofile = 'x'\n", encoding="utf-8")
    result = runner.invoke(app, ["gateway", "run", "--config", str(config)])
    assert result.exit_code == 78, result.output
    assert "not valid TOML" in result.output
    assert "Traceback" not in result.output


def test_other_commands_keep_exit_2_for_a_bad_config(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text("[tools\n", encoding="utf-8")
    result = runner.invoke(app, ["gateway", "status", "--config", str(config)])
    assert result.exit_code == 2, result.output


def test_gateway_run_exits_78_on_a_config_class_startup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "agentos.toml"
    target.write_text("", encoding="utf-8")
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path / "home"))
    recorded: list[str] = []

    class _Tracker:
        def mark_startup_failed(self, reason: str) -> None:
            recorded.append(reason)

    monkeypatch.setattr(
        "agentos.gateway.boot_lifecycle.get_boot_tracker", lambda: _Tracker(), raising=True
    )

    async def fail(**_kwargs: Any) -> Any:
        raise GatewayConfigError("schema migration failed for sessions.db: boom")

    monkeypatch.setattr(gateway_cmd, "start_gateway_server", fail)
    result = runner.invoke(app, ["gateway", "run", "--config", str(target)])
    assert result.exit_code == 78, result.output
    assert "schema migration failed" in result.output
    # The boot is recorded as a startup failure, which safe mode does not count.
    assert recorded == ["GatewayConfigError: schema migration failed for sessions.db: boom"]


def test_gateway_run_keeps_exit_1_for_other_startup_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "agentos.toml"
    target.write_text("", encoding="utf-8")
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path / "home"))

    async def fail(**_kwargs: Any) -> Any:
        raise ValueError("memory.embedding.provider='openai' requires an api key")

    monkeypatch.setattr(gateway_cmd, "start_gateway_server", fail)
    result = runner.invoke(app, ["gateway", "run", "--config", str(target)])
    assert result.exit_code == 1, result.output


def test_migration_failure_is_a_gateway_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentos.gateway import boot

    def broken(_db: str, _dir: Path) -> list[str]:
        raise RuntimeError("table projects already exists")

    monkeypatch.setattr("agentos.persistence.migrator.apply_pending", broken)
    with pytest.raises(GatewayConfigError, match="schema migration failed"):
        asyncio.run(
            boot.build_services(
                config=boot.GatewayConfig(
                    state_dir=str(tmp_path / "state"), workspace_dir=str(tmp_path / "ws")
                ),
                session_db_path=str(tmp_path / "state" / "sessions.db"),
            )
        )


# ── the CLI client retries a booting gateway ─────────────────────────


class _FakeWs:
    def __init__(self, replies: list[dict[str, Any]]) -> None:
        self.replies = [json.dumps(r) for r in replies]
        self.sent: list[str] = []
        self.closed = False

    async def recv(self) -> str:
        return self.replies.pop(0)

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self) -> Any:
        return self

    async def __anext__(self) -> str:
        await asyncio.sleep(3600)
        raise StopAsyncIteration


_CHALLENGE = {"type": "event", "event": "connect.challenge", "payload": {"nonce": "n"}}
_REFUSAL = {
    "type": "res",
    "id": "x",
    "ok": False,
    "error": {
        "code": "UNAVAILABLE",
        "message": "Gateway is starting; retry shortly",
        "retryable": True,
        "details": {"code": "STARTUP_SIDECARS", "retry_after_ms": 100},
    },
}
_HELLO = {"type": "hello-ok", "protocol": 3, "server": {"version": "9.9", "conn_id": "c"}}


def _patch_websockets(monkeypatch: pytest.MonkeyPatch, sockets: list[_FakeWs]) -> None:
    import websockets

    async def fake_connect(_url: str) -> _FakeWs:
        return sockets.pop(0)

    monkeypatch.setattr(websockets, "connect", fake_connect)


def test_client_retries_a_handshake_refused_while_starting(monkeypatch) -> None:
    first, second = _FakeWs([_CHALLENGE, _REFUSAL]), _FakeWs([_CHALLENGE, _HELLO])
    _patch_websockets(monkeypatch, [first, second])

    async def go() -> str | None:
        client = gcm.GatewayClient()
        try:
            await client.connect("ws://127.0.0.1:1/ws", startup_wait_s=5)
            return client.server_version
        finally:
            await client.close()

    assert asyncio.run(go()) == "9.9"
    assert first.closed is True


def test_client_with_no_startup_wait_fails_fast(monkeypatch) -> None:
    _patch_websockets(monkeypatch, [_FakeWs([_CHALLENGE, _REFUSAL])])

    async def go() -> None:
        client = gcm.GatewayClient()
        try:
            await client.connect("ws://127.0.0.1:1/ws", startup_wait_s=0)
        finally:
            await client.close()

    with pytest.raises(SystemExit, match="still starting"):
        asyncio.run(go())


def test_channels_start_cli_calls_the_rpc(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentos.cli import channels_cmd

    calls: list[tuple[str, Any]] = []

    class _Client:
        async def call(self, method: str, params: Any) -> Any:
            calls.append((method, params))
            return {
                "status": "started",
                "channels": {"tg": True, "slack": False},
                "safe_mode_cleared": True,
            }

    def fake_run(fn: Any, **_kwargs: Any) -> Any:
        return asyncio.run(fn(_Client()))

    monkeypatch.setattr(channels_cmd, "run_gateway_sync", fake_run)
    result = runner.invoke(app, ["channels", "start", "--yes"])
    assert result.exit_code == 0, result.output
    assert calls == [("channels.start", {})]
    assert "Safe mode cleared" in result.output
    assert "Channels started: tg" in result.output

    calls.clear()
    named = runner.invoke(app, ["channels", "start", "tg", "--yes", "--json"])
    assert named.exit_code == 0, named.output
    assert calls == [("channels.start", {"name": "tg"})]


def test_gateway_stop_names_the_service_that_runs_the_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentos.cli import gateway_lifecycle

    monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path / "home"))
    monkeypatch.setattr(gateway_lifecycle.GatewayLifecycleManager, "_probe_health", lambda s: True)
    monkeypatch.setattr(
        gateway_cmd,
        "_deep_service_status",
        lambda: {
            "supported": True,
            "platform": "launchd",
            "label": "dev.useagentos.gateway",
            "path": "/x/dev.useagentos.gateway.plist",
            "installed": True,
            "loaded": True,
            "port": 18791,
        },
    )
    result = runner.invoke(app, ["gateway", "stop", "--port", "18791"])
    assert result.exit_code == 3
    assert "LaunchAgent" in result.output
    assert "agentos gateway uninstall" in result.output
    assert "launchctl bootout" in result.output

    as_json = runner.invoke(app, ["gateway", "stop", "--port", "18791", "--json"])
    assert json.loads(as_json.output)["details"]["service"]["installed"] is True

    # A service on another port is not this gateway.
    other = runner.invoke(app, ["gateway", "stop", "--port", "18792"])
    assert "LaunchAgent" not in other.output


def test_exit_78_under_launchd_waits_before_exiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentos.cli import gateway_service

    monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path / "home"))
    monkeypatch.setenv("AGENTOS_SERVICE", "launchd")
    slept: list[float] = []
    monkeypatch.setattr("time.sleep", slept.append)
    config = tmp_path / "config.toml"
    config.write_text("[tools\n", encoding="utf-8")
    first = runner.invoke(app, ["gateway", "run", "--config", str(config)])
    second = runner.invoke(app, ["gateway", "run", "--config", str(config)])
    assert first.exit_code == second.exit_code == 78
    assert slept == [5, 10]
    assert "waiting 5s before exiting" in " ".join(first.output.split())
    assert gateway_service.SERVICE_ENV == "AGENTOS_SERVICE"


def test_channels_status_table_shows_state_reason_and_safe_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentos.cli import channels_cmd

    payload = {
        "channels": [
            {
                "name": "tg",
                "type": "telegram",
                "status": "stopped",
                "state": "terminal",
                "terminal_reason": "auth: HTTP 401",
            },
            {
                "name": "sl",
                "type": "slack",
                "status": "stopped",
                "state": "start_failed",
                "start_error": "ConnectionError: offline",
            },
        ],
        "health_policy": {"safe_mode": {"active": True, "reason": "3 unclean", "clears_at": "T"}},
    }
    monkeypatch.setattr(channels_cmd, "run_gateway_sync", lambda fn, **_kw: payload)
    out = runner.invoke(app, ["channels", "status"]).output
    assert "terminal" in out and "auth: HTTP 401" in out
    assert "start_failed" in out and "ConnectionError: offline" in out
    assert "Safe mode: on (3 unclean)" in " ".join(out.split())

    payload["health_policy"] = {"safe_mode": {"active": False}}
    assert "Safe mode: off" in runner.invoke(app, ["channels", "status"]).output
