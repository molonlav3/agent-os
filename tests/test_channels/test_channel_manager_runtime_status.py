"""ChannelManager runtime status for the gateway health sweep (#3691)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from agentos.channels import manager as manager_mod
from agentos.channels.manager import ChannelManager, terminal_reason_for
from agentos.channels.types import ChannelHealth


class _Adapter:
    transport_name = "socket"

    def __init__(self, *, connected: bool = True, fail_with: Exception | None = None) -> None:
        self.connected = connected
        self.fail_with = fail_with
        self.starts = 0
        self.stops = 0
        self.extra: dict[str, Any] = {}

    async def start(self) -> None:
        self.starts += 1
        if self.fail_with is not None:
            raise self.fail_with

    async def stop(self) -> None:
        self.stops += 1

    async def health_check(self) -> ChannelHealth:
        return ChannelHealth(connected=self.connected, extra=dict(self.extra))


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def fake_dispatch(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Replace the dispatch loop: build one session key per queued message, then idle."""
    inbox: list[Any] = []

    async def fake_run_channel_dispatch(**kwargs: Any) -> None:
        while True:
            if inbox:
                kwargs["session_key_builder"](inbox.pop(0))
            await asyncio.sleep(0)

    monkeypatch.setattr(manager_mod, "run_channel_dispatch", fake_run_channel_dispatch)
    return inbox


def _manager(adapters: dict[str, Any], clock: _Clock | None = None) -> ChannelManager:
    manager = ChannelManager(dict(adapters), None, None)
    if clock is not None:
        manager._clock = clock
    return manager


def _msg() -> Any:
    return SimpleNamespace(metadata={}, channel_id="c1", sender_id="u1")


@pytest.mark.asyncio
async def test_runtime_status_reports_state_start_and_last_event(fake_dispatch: list[Any]) -> None:
    clock = _Clock()
    adapter = _Adapter()
    manager = _manager({"tg": adapter}, clock)
    await manager.start_all()
    try:
        status = (await manager.runtime_status())["tg"]
        assert status["state"] == "connected"
        assert status["started_at"] == 1_000.0
        assert status["last_event_at"] is None
        assert status["terminal_reason"] is None

        clock.now = 1_100.0
        fake_dispatch.append(_msg())
        for _ in range(5):
            await asyncio.sleep(0)
        status = (await manager.runtime_status())["tg"]
        assert status["last_event_at"] == 1_100.0

        adapter.connected = False
        assert (await manager.runtime_status())["tg"]["state"] == "disconnected"
    finally:
        await manager.stop_all()
    assert (await manager.runtime_status())["tg"]["state"] == "stopped"


@pytest.mark.asyncio
async def test_a_credential_failure_is_terminal_and_never_auto_restarted(
    fake_dispatch: list[Any],
) -> None:
    adapter = _Adapter(fail_with=RuntimeError("401 Unauthorized: invalid token"))
    manager = _manager({"tg": adapter})
    assert await manager.start_all() == {"tg": False}
    status = (await manager.runtime_status())["tg"]
    assert status["state"] == "terminal"
    assert status["terminal_reason"].startswith("auth: RuntimeError: 401 Unauthorized")
    assert await manager.restart_if_idle("tg") == "terminal"
    assert adapter.starts == 1

    # An operator restart after fixing the token is the way out.
    adapter.fail_with = None
    await manager.restart_channel("tg")
    try:
        status = (await manager.runtime_status())["tg"]
        assert status["state"] == "connected"
        assert status["terminal_reason"] is None
    finally:
        await manager.stop_all()


@pytest.mark.asyncio
async def test_an_adapter_can_report_blocked(fake_dispatch: list[Any]) -> None:
    adapter = _Adapter()
    adapter.extra = {"blocked": "account suspended"}
    manager = _manager({"tg": adapter})
    await manager.start_all()
    try:
        status = (await manager.runtime_status())["tg"]
        assert status["state"] == "blocked"
        assert status["terminal_reason"] == "blocked: account suspended"
    finally:
        await manager.stop_all()


@pytest.mark.asyncio
async def test_restart_if_idle_restarts_running_and_dead_but_not_stopped(
    fake_dispatch: list[Any],
) -> None:
    adapter = _Adapter()
    manager = _manager({"tg": adapter, "off": _Adapter()})
    await manager.start_all()
    await manager.stop_channel("off")
    try:
        assert await manager.restart_if_idle("tg") == "restarted"
        assert adapter.starts == 2
        assert await manager.restart_if_idle("off") == "stopped"  # the operator stopped it
        assert await manager.restart_if_idle("missing") == "unknown"

        manager._dispatch_states["tg"] = "dead"
        assert (await manager.runtime_status())["tg"]["state"] == "stuck"
        assert await manager.restart_if_idle("tg") == "restarted"
    finally:
        await manager.stop_all()


@pytest.mark.asyncio
async def test_restart_if_idle_defers_a_channel_with_replies_in_flight(
    fake_dispatch: list[Any],
) -> None:
    adapter = _Adapter()
    manager = _manager({"tg": adapter})
    await manager.start_all()
    try:
        token = object()
        assert manager._in_flight_sets["tg"].try_acquire(token)
        assert manager.in_flight_count("tg") == 1
        assert await manager.restart_if_idle("tg") == "busy"
        assert adapter.starts == 1
        manager._in_flight_sets["tg"].release(token)
        assert await manager.restart_if_idle("tg") == "restarted"
    finally:
        await manager.stop_all()


@pytest.mark.asyncio
async def test_start_stopped_leaves_running_channels_alone(fake_dispatch: list[Any]) -> None:
    running, stopped = _Adapter(), _Adapter()
    manager = _manager({"run": running, "stop": stopped})
    await manager.start_all()
    await manager.stop_channel("stop")
    try:
        assert await manager.start_stopped() == {"stop": True}
        assert running.starts == 1
        assert stopped.starts == 2
    finally:
        await manager.stop_all()


@pytest.mark.parametrize(
    ("error", "terminal"),
    [
        (RuntimeError("HTTP 401 Unauthorized"), True),
        (RuntimeError("invalid_auth"), True),
        (RuntimeError("Discord closed the gateway: 4004 Authentication failed"), True),
        (RuntimeError("token_revoked"), True),
        (TimeoutError("timed out after 4013ms"), False),
        (ConnectionResetError("Connection reset by peer"), False),
    ],
)
def test_terminal_reason_classification(error: Exception, terminal: bool) -> None:
    assert (terminal_reason_for(error) is not None) is terminal
