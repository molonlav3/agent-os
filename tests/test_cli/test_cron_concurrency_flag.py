"""``agentos cron add|update --concurrency`` reaches the gateway as ``concurrency``."""

from __future__ import annotations

import re
from typing import Any

import pytest
from typer.testing import CliRunner

from agentos.cli import cron_cmd
from agentos.cli.main import app

runner = CliRunner()

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_BOX_DRAWING = re.compile(r"[\u2500-\u257f]")


def _plain(output: str) -> str:
    """Click's usage error as one line of text, whatever the terminal.

    Under CI (``GITHUB_ACTIONS`` forces a terminal) rich colours the error,
    boxes it and wraps it at the console width, so the message arrives split
    across box rows with escape codes inside the option name.
    """
    return " ".join(_BOX_DRAWING.sub(" ", _ANSI.sub("", output)).split())


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    recorded: list[tuple[str, dict[str, Any]]] = []

    class _Client:
        async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            recorded.append((method, params))
            return {"id": "job-1"}

    def _runner(fn, **_kwargs):
        import asyncio

        return asyncio.run(fn(_Client()))

    monkeypatch.setattr(cron_cmd, "run_gateway_sync", _runner)
    monkeypatch.setattr(cron_cmd, "_emit_success", lambda *a, **kw: None)
    return recorded


def test_cron_add_sends_the_policy(calls) -> None:
    result = runner.invoke(
        app,
        ["cron", "add", "--every", "5m", "--text", "ping", "--concurrency", "Replace"],
    )

    assert result.exit_code == 0, result.output
    method, params = calls[-1]
    assert method == "cron.add"
    assert params["concurrency"] == "replace"


def test_cron_add_omits_the_policy_when_not_given(calls) -> None:
    result = runner.invoke(app, ["cron", "add", "--every", "5m", "--text", "ping"])

    assert result.exit_code == 0, result.output
    assert "concurrency" not in calls[-1][1]


def test_cron_update_sends_the_policy_alone(calls) -> None:
    result = runner.invoke(app, ["cron", "update", "job-1", "--concurrency", "allow"])

    assert result.exit_code == 0, result.output
    assert calls[-1] == ("cron.update", {"id": "job-1", "concurrency": "allow"})


def test_an_unknown_policy_is_refused_before_any_call(calls) -> None:
    result = runner.invoke(
        app,
        ["cron", "add", "--every", "5m", "--text", "ping", "--concurrency", "parallel"],
    )

    assert result.exit_code == 2, result.output
    assert "--concurrency must be one of forbid, replace, allow" in _plain(result.output)
    assert calls == []
