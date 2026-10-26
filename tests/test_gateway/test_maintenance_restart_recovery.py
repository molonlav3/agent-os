"""A failed automatic restart must not leave a channel stopped (#3691 regression).

Real ``ChannelManager`` + ``GatewayMaintenance`` on a fake clock, with an
adapter whose start fails the way it does right after a wake: the network is
not back yet.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agentos.channels import manager as manager_mod
from agentos.channels.manager import ChannelManager
from agentos.channels.types import ChannelHealth
from agentos.gateway.maintenance import ChannelHealthPolicy, GatewayMaintenance, MaintenanceClock


class FlakyAdapter:
    transport_name = "polling"

    def __init__(self) -> None:
        self.starts = 0
        self.fail_next = 0
        self.fail_with: Exception = ConnectionError(
            "[Errno 8] nodename nor servname provided, or not known"
        )
        self.connected = True

    async def start(self) -> None:
        self.starts += 1
        if self.fail_next > 0:
            self.fail_next -= 1
            raise self.fail_with

    async def stop(self) -> None:
        return None

    async def health_check(self) -> ChannelHealth:
        return ChannelHealth(connected=self.connected)


class Clock:
    def __init__(self) -> None:
        self.mono = 0.0
        self.cpu = 0.0
        self.wall = 1_000_000.0

    def maintenance_clock(self) -> MaintenanceClock:
        return MaintenanceClock(
            monotonic=lambda: self.mono,
            process_time=lambda: self.cpu,
            wall=lambda: self.wall,
            sleep=asyncio.sleep,
        )

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.wall += seconds


@pytest.fixture
def idle_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_dispatch(**_kwargs: Any) -> None:
        while True:
            await asyncio.sleep(0.01)

    monkeypatch.setattr(manager_mod, "run_channel_dispatch", fake_dispatch)


async def _instant_sleep(_seconds: float) -> None:
    await asyncio.sleep(0)


def _manager(adapter: FlakyAdapter) -> ChannelManager:
    manager = ChannelManager({"tg": adapter}, None, None)
    manager._sleep = _instant_sleep  # backoff without wall time
    return manager


async def _settle(manager: ChannelManager, *, until: str, rounds: int = 50) -> str:
    state = ""
    for _ in range(rounds):
        state = (await manager.runtime_status())["tg"]["state"]
        if state == until:
            break
        await asyncio.sleep(0)
    return state


@pytest.mark.asyncio
@pytest.mark.usefixtures("idle_dispatch")
async def test_thaw_restart_that_fails_transiently_recovers() -> None:
    adapter = FlakyAdapter()
    manager = _manager(adapter)
    await manager.start_all()
    clock = Clock()
    maint = GatewayMaintenance(
        channel_manager_ref=lambda: manager,
        policy=ChannelHealthPolicy(),
        clock=clock.maintenance_clock(),
    )
    try:
        # Two hours asleep; on wake the first start fails (no network yet).
        clock.advance(7200)
        adapter.fail_next = 1
        await maint.tick()
        assert maint.last_thaw is not None
        assert maint.last_thaw.failed == ["tg"]
        assert maint.last_thaw.restarted == []
        assert (await manager.runtime_status())["tg"]["state"] in {"retrying", "connected"}

        # The manager's retry loop brings it back without any operator.
        assert await _settle(manager, until="connected") == "connected"
        assert adapter.starts == 3  # boot, the failed thaw restart, the retry

        # And the sweeps that follow see a healthy channel.
        for _ in range(40):
            clock.advance(30)
            await maint.tick()
        status = (await manager.runtime_status())["tg"]
        assert status["state"] == "connected"
        assert status["start_error"] is None
        assert maint.last_sweep is not None
        assert maint.last_sweep.failed == []
    finally:
        await manager.stop_all()


@pytest.mark.asyncio
@pytest.mark.usefixtures("idle_dispatch")
async def test_exhausted_retries_read_stuck_and_the_sweep_picks_them_up() -> None:
    adapter = FlakyAdapter()
    manager = _manager(adapter)
    manager._start_retry_attempts = 2
    await manager.start_all()
    maint = GatewayMaintenance(
        channel_manager_ref=lambda: manager, clock=Clock().maintenance_clock()
    )
    try:
        adapter.fail_next = 3  # the restart and both retries fail
        assert await manager.restart_if_idle("tg") == "failed"
        assert await _settle(manager, until="stuck") == "stuck"
        assert manager.failed_channels() == {
            "tg": "ConnectionError: [Errno 8] nodename nor servname provided, or not known"
        }

        report = await maint.sweep()  # never terminal: the sweep tries again
        assert report.states["tg"] == "stuck"
        assert report.restarted == ["tg"]
        assert (await manager.runtime_status())["tg"]["state"] == "connected"
    finally:
        await manager.stop_all()


@pytest.mark.asyncio
@pytest.mark.usefixtures("idle_dispatch")
async def test_a_failed_restart_is_reported_as_failed_not_busy() -> None:
    adapter = FlakyAdapter()
    manager = _manager(adapter)
    manager._start_retry_attempts = 1
    await manager.start_all()
    maint = GatewayMaintenance(
        channel_manager_ref=lambda: manager, clock=Clock().maintenance_clock()
    )
    try:
        manager._dispatch_states["tg"] = "dead"
        adapter.fail_next = 5
        report = await maint.sweep()
        assert report.failed == ["tg"]
        assert "tg" not in report.skipped
        # The cooldown applies to a failed attempt as well.
        await _settle(manager, until="stuck")
        second = await maint.sweep()
        assert second.skipped == {"tg": "cooldown"}
    finally:
        await manager.stop_all()


@pytest.mark.asyncio
@pytest.mark.usefixtures("idle_dispatch")
async def test_a_credential_failure_during_retry_ends_in_terminal() -> None:
    adapter = FlakyAdapter()
    manager = _manager(adapter)
    await manager.start_all()
    try:
        adapter.fail_next = 1
        assert await manager.restart_if_idle("tg") == "failed"
        adapter.fail_with = RuntimeError("401 Unauthorized")
        adapter.fail_next = 1
        state = await _settle(manager, until="terminal")
        assert state == "terminal"
        assert await manager.restart_if_idle("tg") == "terminal"
    finally:
        await manager.stop_all()


@pytest.mark.asyncio
@pytest.mark.usefixtures("idle_dispatch")
async def test_a_boot_start_failure_is_restartable_and_an_operator_stop_is_not() -> None:
    adapter = FlakyAdapter()
    adapter.fail_next = 1
    manager = _manager(adapter)
    assert await manager.start_all() == {"tg": False}
    try:
        assert (await manager.runtime_status())["tg"]["state"] == "start_failed"
        maint = GatewayMaintenance(
            channel_manager_ref=lambda: manager, clock=Clock().maintenance_clock()
        )
        report = await maint.sweep()
        assert report.restarted == ["tg"]
        assert (await manager.runtime_status())["tg"]["state"] == "connected"

        # Logged out by the operator after its loop died: stays stopped.
        manager._dispatch_states["tg"] = "dead"
        await manager.stop_channel("tg")
        assert (await manager.runtime_status())["tg"]["state"] == "stopped"
        assert await manager.restart_if_idle("tg") == "stopped"
        report = await maint.sweep()
        assert report.restarted == [] and report.failed == []
    finally:
        await manager.stop_all()
