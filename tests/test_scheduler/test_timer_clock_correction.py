"""Sleep-aware timer arming (#3683): sleep gaps, clock jumps, late wakes, coalescing.

Every test drives the timer through an injected clock, so a two-hour lid
close or a thirty-minute backward NTP correction takes no real time and the
outcome does not depend on the machine running the suite.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import pytest

from agentos.scheduler.jobs import _apply_result_state
from agentos.scheduler.persistence import JobStore
from agentos.scheduler.timer import ArmedTimer, SchedulerClock, SchedulerTimer, WakeReport
from agentos.scheduler.types import (
    CronJob,
    JobExecution,
    JobStatus,
    ScheduleKind,
    SessionTarget,
)

T0 = datetime(2026, 3, 2, 10, 0, 0, tzinfo=UTC)


class FakeClock(SchedulerClock):
    """Wall + monotonic clock under test control.

    ``wait`` consumes one scripted ``(wall_seconds, mono_seconds)`` step per
    call -- what the two clocks did while the loop was asleep -- and otherwise
    lets both advance by the full timeout, like an undisturbed sleep.
    """

    def __init__(self, wall: datetime = T0, mono: float = 5_000.0) -> None:
        self.wall_now = wall
        self.mono_now = mono
        self.script: list[tuple[float, float]] = []
        self.waits: list[float] = []

    def wall(self) -> datetime:
        return self.wall_now

    def monotonic(self) -> float:
        return self.mono_now

    def advance(self, wall_seconds: float, mono_seconds: float | None = None) -> None:
        self.wall_now += timedelta(seconds=wall_seconds)
        self.mono_now += wall_seconds if mono_seconds is None else mono_seconds

    async def wait(self, event: asyncio.Event, timeout: float) -> bool:
        self.waits.append(timeout)
        await asyncio.sleep(0)
        if event.is_set():
            return True
        if self.script:
            wall_s, mono_s = self.script.pop(0)
            self.advance(wall_s, mono_s)
        else:
            self.advance(timeout)
        return False


def _every_job(job_id: str, *, interval: int, next_run_at: datetime, anchor: datetime) -> CronJob:
    return CronJob(
        id=job_id,
        name=job_id,
        cron_expr=str(interval),
        schedule_raw=str(interval),
        handler_key="static_message",
        payload={"kind": "reminder", "text": "ping"},
        session_target=SessionTarget.ISOLATED,
        schedule_kind=ScheduleKind.EVERY,
        anchor_at=anchor,
        next_run_at=next_run_at,
        status=JobStatus.PENDING,
    )


def _hourly_job(job_id: str, *, next_run_at: datetime) -> CronJob:
    return CronJob(
        id=job_id,
        name=job_id,
        cron_expr="0 * * * *",
        schedule_raw="0 * * * *",
        handler_key="static_message",
        payload={"kind": "reminder", "text": "ping"},
        session_target=SessionTarget.ISOLATED,
        schedule_kind=ScheduleKind.CRON,
        next_run_at=next_run_at,
        status=JobStatus.PENDING,
    )


async def _drain(timer: SchedulerTimer) -> None:
    await asyncio.gather(*list(timer._running.values()), return_exceptions=True)


async def _cycle(timer: SchedulerTimer) -> WakeReport:
    """One loop iteration: arm + wait, tick, let the launched runs finish."""
    report = await timer._wait_for_next_due()
    await timer._tick()
    await _drain(timer)
    return report


# ── arming ───────────────────────────────────────────────────────────────────


def test_armed_timer_uses_the_earlier_of_wall_and_monotonic() -> None:
    armed = ArmedTimer(armed_wall=T0, armed_mono=100.0, delay=60.0)

    # Undisturbed: both clocks agree.
    assert armed.remaining(T0 + timedelta(seconds=20), 120.0) == pytest.approx(40.0)
    # Lid closed for an hour: wall raced ahead, monotonic barely moved -> due.
    assert armed.remaining(T0 + timedelta(hours=1), 105.0) < 0
    # Wall clock set back 30 min: monotonic still counts down to the target.
    assert armed.remaining(T0 - timedelta(minutes=30), 160.0) == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_wait_slices_never_exceed_the_max_timer_delay() -> None:
    clock = FakeClock()
    async with JobStore(":memory:") as store:
        far = _every_job("far", interval=86_400, next_run_at=T0 + timedelta(hours=5), anchor=T0)
        await store.save(far)
        timer = SchedulerTimer(store, handlers={}, clock=clock)

        report = await timer._wait_for_next_due()

    assert report.expected_seconds == SchedulerTimer.MAX_TIMER_DELAY
    assert clock.waits and max(clock.waits) <= SchedulerTimer.MAX_TIMER_DELAY


# ── sleep gap ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sleep_gap_fires_the_overdue_job_exactly_once(caplog) -> None:
    clock = FakeClock()
    fired: list[datetime] = []

    async def handler(job: CronJob) -> str:
        fired.append(clock.wall())
        return "ok"

    async with JobStore(":memory:") as store:
        await store.save(
            _every_job("every-min", interval=60, next_run_at=T0 + timedelta(seconds=60), anchor=T0)
        )
        timer = SchedulerTimer(store, handlers={"static_message": handler}, clock=clock)
        # The lid closes 10 s into the 60 s wait and opens two hours later.
        clock.script = [(2 * 3600.0, 10.0)]

        with caplog.at_level(logging.WARNING, logger="agentos.scheduler.timer"):
            report = await _cycle(timer)

        assert report.forward_gap == pytest.approx(2 * 3600.0 - 10.0)
        assert report.late is True
        assert "scheduler_wake_gap" in caplog.text
        assert "scheduler_late_wake" in caplog.text
        # 120 missed one-minute slots coalesce into a single run.
        assert fired == [T0 + timedelta(hours=2)]
        job = await store.get("every-min")
        assert job is not None
        assert job.next_run_at is not None and job.next_run_at > clock.wall()
        assert job.next_run_at == T0 + timedelta(hours=2, seconds=60)

        # The very next wake at the same moment finds nothing left to run.
        clock.script = [(0.0, 0.0)]
        timer.nudge()
        await _cycle(timer)
        assert len(fired) == 1


@pytest.mark.asyncio
async def test_coalesced_missed_ticks_run_once_and_fast_forward() -> None:
    clock = FakeClock()
    runs = 0

    async def handler(job: CronJob) -> str:
        nonlocal runs
        runs += 1
        return "ok"

    async with JobStore(":memory:") as store:
        anchor = T0 - timedelta(hours=3)
        await store.save(
            _every_job("missed", interval=60, next_run_at=T0 - timedelta(hours=1), anchor=anchor)
        )
        timer = SchedulerTimer(store, handlers={"static_message": handler}, clock=clock)

        await timer._tick()
        await _drain(timer)
        await timer._tick()
        await _drain(timer)

        assert runs == 1
        job = await store.get("missed")
        assert job is not None and job.next_run_at is not None
        assert job.next_run_at > T0
        # Still on the anchor grid.
        assert (job.next_run_at - anchor).total_seconds() % 60 == 0


# ── backward clock jump ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_backward_clock_jump_during_a_wait_is_logged_and_fires_once(caplog) -> None:
    clock = FakeClock()
    fired: list[datetime] = []

    async def handler(job: CronJob) -> str:
        fired.append(clock.wall())
        return "ok"

    async with JobStore(":memory:") as store:
        slot = T0 + timedelta(seconds=60)
        await store.save(_every_job("every-min", interval=60, next_run_at=slot, anchor=T0))
        timer = SchedulerTimer(store, handlers={"static_message": handler}, clock=clock)
        # 5 s into the wait NTP sets the wall clock back 30 minutes.
        clock.script = [(-1800.0 + 5.0, 5.0)]

        with caplog.at_level(logging.WARNING, logger="agentos.scheduler.timer"):
            report = await _cycle(timer)

        # Monotonic time still woke the loop on schedule ...
        assert report.mono_elapsed == pytest.approx(60.0)
        assert report.backward_jump == pytest.approx(1800.0)
        assert "scheduler_clock_back" in caplog.text
        # ... but by the wall clock nothing is due yet, so nothing fired early.
        assert fired == []

        # Walk the wall clock forward to the slot: it fires once, at the slot.
        for _ in range(40):
            await _cycle(timer)
            if fired:
                break
        assert fired == [slot]
        # And the following wake fires the *next* slot, never the same one again.
        await _cycle(timer)
        assert fired == [slot, slot + timedelta(seconds=60)]


@pytest.mark.asyncio
async def test_backward_jump_during_a_run_does_not_refire_the_same_slot() -> None:
    clock = FakeClock()
    fired: list[datetime] = []

    async def handler(job: CronJob) -> str:
        fired.append(clock.wall())
        # While the run is in flight the wall clock is set back 25 minutes.
        clock.advance(-1500.0, 1.0)
        return "ok"

    async with JobStore(":memory:") as store:
        await store.save(_hourly_job("hourly", next_run_at=T0))
        timer = SchedulerTimer(store, handlers={"static_message": handler}, clock=clock)

        await timer._tick()
        await _drain(timer)

        job = await store.get("hourly")
        assert job is not None
        # Computed after the moment the run took the job, not after the
        # rewound "now" (09:35), which would have handed back 10:00 again.
        assert job.next_run_at == T0 + timedelta(hours=1)

        clock.wall_now = T0 + timedelta(minutes=1)
        await timer._tick()
        await _drain(timer)
        assert fired == [T0]


def test_apply_result_state_bases_the_next_slot_on_the_reservation() -> None:
    job = _hourly_job("hourly", next_run_at=T0)
    job.reserved_at = T0
    job.status = JobStatus.RUNNING
    rewound = T0 - timedelta(minutes=25)

    _apply_result_state(job, JobExecution(job_id=job.id, success=True), rewound)

    assert job.next_run_at == T0 + timedelta(hours=1)
    assert job.status == JobStatus.PENDING


def test_apply_result_state_keeps_now_as_base_without_a_reservation() -> None:
    job = _hourly_job("hourly", next_run_at=T0)

    _apply_result_state(job, JobExecution(job_id=job.id, success=True), T0 + timedelta(minutes=5))

    assert job.next_run_at == T0 + timedelta(hours=1)


# ── late wake ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_late_wake_is_logged_when_the_loop_oversleeps(caplog) -> None:
    clock = FakeClock()
    async with JobStore(":memory:") as store:
        timer = SchedulerTimer(store, handlers={}, clock=clock)
        # An event loop blocked for 200 s: both clocks agree, so no gap -- just late.
        clock.script = [(200.0, 200.0)]

        with caplog.at_level(logging.WARNING, logger="agentos.scheduler.timer"):
            report = await timer._wait_for_next_due()

    assert report.expected_seconds == SchedulerTimer.MAX_TIMER_DELAY
    assert report.late is True
    assert report.forward_gap == 0.0
    assert "scheduler_late_wake" in caplog.text
    assert "scheduler_wake_gap" not in caplog.text


@pytest.mark.asyncio
async def test_on_time_and_nudged_wakes_are_not_late(caplog) -> None:
    clock = FakeClock()
    async with JobStore(":memory:") as store:
        timer = SchedulerTimer(store, handlers={}, clock=clock)

        with caplog.at_level(logging.WARNING, logger="agentos.scheduler.timer"):
            on_time = await timer._wait_for_next_due()
            timer.nudge()
            nudged = await timer._wait_for_next_due()

    assert on_time.late is False and on_time.nudged is False
    assert nudged.nudged is True and nudged.late is False
    assert "scheduler_late_wake" not in caplog.text


def test_late_wake_floor_ignores_short_floor_waits() -> None:
    # The 0.1 s floor wait finishing at 0.5 s is more than 2x, but not late.
    assert WakeReport(0.1, 0.5, 0.5, nudged=False).late is False
    assert WakeReport(10.0, 25.0, 25.0, nudged=False).late is True


# ── nudge ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_engine_nudge_wakes_the_timer() -> None:
    from agentos.scheduler.engine import SchedulerEngine

    async with JobStore(":memory:") as store:
        engine = SchedulerEngine(store)
        assert not engine._timer._nudge_event.is_set()
        engine.nudge()
        assert engine._timer._nudge_event.is_set()
