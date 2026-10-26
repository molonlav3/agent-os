"""Sleep/thaw detection and the channel health sweep (#3691), on a fake clock."""

from __future__ import annotations

from typing import Any

import pytest

from agentos.gateway.maintenance import (
    ChannelHealthPolicy,
    GatewayMaintenance,
    MaintenanceClock,
    classify,
)


class FakeClock:
    def __init__(self) -> None:
        self.mono = 1000.0
        self.cpu = 5.0
        self.wall = 1_700_000_000.0

    def clock(self) -> MaintenanceClock:
        async def _sleep(_s: float) -> None:
            return None

        return MaintenanceClock(
            monotonic=lambda: self.mono,
            process_time=lambda: self.cpu,
            wall=lambda: self.wall,
            sleep=_sleep,
        )

    def advance(self, seconds: float, *, cpu: float = 0.1) -> None:
        self.mono += seconds
        self.wall += seconds
        self.cpu += cpu


class FakeManager:
    def __init__(self, statuses: dict[str, dict[str, Any]]) -> None:
        self.statuses = statuses
        self.restarts: list[str] = []
        self.busy: set[str] = set()

    async def runtime_status(self) -> dict[str, dict[str, Any]]:
        return {name: dict(status) for name, status in self.statuses.items()}

    async def restart_if_idle(self, name: str) -> bool:
        if name in self.busy:
            return False
        self.restarts.append(name)
        return True


class FakeScheduler:
    def __init__(self) -> None:
        self.nudges = 0

    def nudge(self) -> None:
        self.nudges += 1


def _status(state: str, **extra: Any) -> dict[str, Any]:
    return {"state": state, "transport": "socket", **extra}


def _maint(
    clock: FakeClock,
    manager: FakeManager,
    *,
    safe_mode: bool = False,
    scheduler: FakeScheduler | None = None,
    hooks: list[Any] | None = None,
) -> tuple[GatewayMaintenance, list[tuple[str, Any]]]:
    events: list[tuple[str, Any]] = []

    async def broadcast(event: str, payload: Any) -> None:
        events.append((event, payload))

    maint = GatewayMaintenance(
        channel_manager_ref=lambda: manager,
        scheduler_ref=lambda: scheduler,
        safe_mode_active=lambda: safe_mode,
        on_tick=hooks,
        broadcast=broadcast,
        clock=clock.clock(),
    )
    return maint, events


@pytest.mark.asyncio
async def test_a_long_gap_with_idle_cpu_is_a_thaw() -> None:
    clock = FakeClock()
    manager = FakeManager(
        {
            "tg": _status("connected"),
            "slack": _status("disconnected"),
            "dead": _status("terminal", terminal_reason="auth: 401"),
            "off": _status("stopped"),
        }
    )
    scheduler = FakeScheduler()
    maint, events = _maint(clock, manager, scheduler=scheduler)

    clock.advance(2 * 3600, cpu=0.4)  # lid closed for two hours
    await maint.tick()

    assert maint.last_thaw is not None
    assert maint.last_thaw.gap_s == pytest.approx(7200)
    # Terminal and operator-stopped channels are left alone.
    assert sorted(manager.restarts) == ["slack", "tg"]
    assert scheduler.nudges == 1
    assert events == [("health", {"reason": "thaw", "gap_ms": 7_200_000})]
    assert sorted(maint.snapshot()["last_thaw"]["restarted"]) == ["slack", "tg"]


@pytest.mark.asyncio
async def test_a_long_gap_with_busy_cpu_is_a_stalled_loop_not_a_thaw() -> None:
    clock = FakeClock()
    manager = FakeManager({"tg": _status("connected")})
    maint, events = _maint(clock, manager)
    clock.advance(60, cpu=55.0)  # the loop was blocked computing
    await maint.tick()
    assert maint.last_thaw is None
    assert manager.restarts == []
    assert events == []


@pytest.mark.asyncio
async def test_a_normal_tick_is_not_a_thaw() -> None:
    clock = FakeClock()
    manager = FakeManager({"tg": _status("connected")})
    maint, _ = _maint(clock, manager)
    clock.advance(30)
    await maint.tick()
    clock.advance(44)
    await maint.tick()
    assert maint.last_thaw is None


@pytest.mark.asyncio
async def test_thaw_in_safe_mode_restarts_nothing() -> None:
    clock = FakeClock()
    manager = FakeManager({"tg": _status("connected")})
    maint, _ = _maint(clock, manager, safe_mode=True)
    clock.advance(600)
    await maint.tick()
    assert maint.last_thaw is not None
    assert manager.restarts == []


async def _run_ticks(maint: GatewayMaintenance, clock: FakeClock, seconds: float) -> None:
    """Tick every 30 s for ``seconds`` of quiet, awake time."""
    for _ in range(int(seconds // 30)):
        clock.advance(30)
        await maint.tick()


@pytest.mark.asyncio
async def test_sweep_waits_for_the_startup_grace_then_runs_every_five_minutes() -> None:
    clock = FakeClock()
    manager = FakeManager({"tg": _status("connected")})
    maint, _ = _maint(clock, manager)

    clock.advance(30)
    await maint.tick()
    assert maint.last_sweep is None  # inside the 60 s grace
    clock.advance(30)
    await maint.tick()
    assert maint.last_sweep is not None and maint.last_sweep.index == 1

    await _run_ticks(maint, clock, 270)
    assert maint.last_sweep.index == 1
    await _run_ticks(maint, clock, 30)
    assert maint.last_sweep.index == 2


@pytest.mark.asyncio
async def test_sweep_restarts_unhealthy_channels_at_most_once_per_two_sweeps() -> None:
    clock = FakeClock()
    manager = FakeManager(
        {
            "disc": _status("disconnected"),
            "stuck": _status("stuck"),
            "ok": _status("connected", started_at=clock.wall, last_event_at=clock.wall),
            "bad": _status("terminal", terminal_reason="auth: invalid_auth"),
            "blocked": _status("blocked", terminal_reason="blocked: account"),
        }
    )
    maint, _ = _maint(clock, manager)

    first = await maint.sweep()
    assert sorted(first.restarted) == ["disc", "stuck"]
    assert first.states["bad"] == "terminal"
    assert first.states["blocked"] == "blocked"

    second = await maint.sweep()
    assert second.restarted == []
    assert second.skipped == {"disc": "cooldown", "stuck": "cooldown"}

    third = await maint.sweep()
    assert sorted(third.restarted) == ["disc", "stuck"]
    assert sorted(manager.restarts) == ["disc", "disc", "stuck", "stuck"]


@pytest.mark.asyncio
async def test_sweep_restarts_a_stale_socket_but_not_a_quiet_webhook() -> None:
    clock = FakeClock()
    long_ago = clock.wall - 31 * 60
    manager = FakeManager(
        {
            "socket": _status("connected", started_at=long_ago, last_event_at=long_ago),
            "hook": {
                "state": "connected",
                "transport": "webhook",
                "started_at": long_ago,
                "last_event_at": None,
            },
            "fresh": _status("connected", started_at=long_ago, last_event_at=clock.wall - 60),
        }
    )
    maint, _ = _maint(clock, manager)
    report = await maint.sweep()
    assert report.states == {"socket": "stale-socket", "hook": "connected", "fresh": "connected"}
    assert manager.restarts == ["socket"]


@pytest.mark.asyncio
async def test_sweep_skips_busy_channels_and_retries_next_time() -> None:
    clock = FakeClock()
    manager = FakeManager({"disc": _status("disconnected")})
    manager.busy.add("disc")
    maint, _ = _maint(clock, manager)
    report = await maint.sweep()
    assert report.skipped == {"disc": "busy"}
    manager.busy.clear()
    report = await maint.sweep()
    assert report.restarted == ["disc"]


@pytest.mark.asyncio
async def test_sweep_in_safe_mode_touches_nothing() -> None:
    clock = FakeClock()
    manager = FakeManager({"disc": _status("disconnected")})
    maint, _ = _maint(clock, manager, safe_mode=True)
    report = await maint.sweep()
    assert report.skipped == {"disc": "safe_mode"}
    assert manager.restarts == []


@pytest.mark.asyncio
async def test_tick_runs_the_safe_mode_reevaluation_hook() -> None:
    clock = FakeClock()
    calls: list[int] = []

    async def hook() -> None:
        calls.append(1)

    maint, _ = _maint(clock, FakeManager({}), hooks=[hook])
    clock.advance(30)
    await maint.tick()
    clock.advance(30)
    await maint.tick()
    assert calls == [1, 1]


def test_policy_payload_names_what_is_never_restarted() -> None:
    payload = ChannelHealthPolicy().to_payload()
    assert payload["never_auto_restart"] == ["blocked", "terminal"]
    assert payload["restart_states"] == ["disconnected", "stale-socket", "stuck"]
    assert payload["sweep_interval_s"] == 300.0
    assert payload["startup_grace_s"] == 60.0
    assert payload["stale_after_s"] == 1800.0
    assert payload["restart_cooldown_sweeps"] == 2


def test_classify_uses_the_later_of_start_and_last_event() -> None:
    policy = ChannelHealthPolicy()
    now = 10_000.0
    restarted_recently = {
        "state": "connected",
        "transport": "socket",
        "started_at": now - 60,
        "last_event_at": now - 3 * 3600,
    }
    assert classify(restarted_recently, now=now, policy=policy) == "connected"
    assert classify({"state": "stuck"}, now=now, policy=policy) == "stuck"


def test_suspend_aware_clock_is_monotonic() -> None:
    from agentos.gateway.maintenance import suspend_aware_monotonic

    first = suspend_aware_monotonic()
    second = suspend_aware_monotonic()
    assert second >= first
