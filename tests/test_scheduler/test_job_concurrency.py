"""Per-job concurrency policy: forbid | replace | allow.

The situation each policy answers: a job's next slot comes due while its
previous run is still in flight.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import pytest

from agentos.scheduler.persistence import JobStore
from agentos.scheduler.timer import REPLACED_RUN_ERROR, SchedulerClock, SchedulerTimer
from agentos.scheduler.types import (
    ConcurrencyPolicy,
    CronJob,
    JobReservation,
    JobStatus,
    ReservationRejectionReason,
    ScheduleKind,
    SessionTarget,
    normalize_concurrency,
)

T0 = datetime(2026, 3, 2, 10, 0, 0, tzinfo=UTC)


class _Clock(SchedulerClock):
    def __init__(self) -> None:
        self.wall_now = T0

    def wall(self) -> datetime:
        return self.wall_now

    def monotonic(self) -> float:
        return 0.0


def _job(policy: ConcurrencyPolicy) -> CronJob:
    return CronJob(
        id=f"job-{policy.value}",
        name=f"job-{policy.value}",
        cron_expr="60",
        schedule_raw="60",
        handler_key="static_message",
        payload={"kind": "reminder", "text": "ping"},
        session_target=SessionTarget.ISOLATED,
        schedule_kind=ScheduleKind.EVERY,
        anchor_at=T0,
        next_run_at=T0,
        status=JobStatus.PENDING,
        concurrency=policy,
    )


class _SlowHandler:
    """Each call blocks until released; records start order and cancellations."""

    def __init__(self) -> None:
        self.started: list[int] = []
        self.cancelled: list[int] = []
        self.release: dict[int, asyncio.Event] = {}

    async def __call__(self, job: CronJob) -> str:
        n = len(self.started) + 1
        self.started.append(n)
        gate = self.release.setdefault(n, asyncio.Event())
        try:
            await gate.wait()
        except asyncio.CancelledError:
            self.cancelled.append(n)
            raise
        return f"run {n}"

    def finish(self, n: int) -> None:
        self.release.setdefault(n, asyncio.Event()).set()


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def _start_first_run(policy: ConcurrencyPolicy):
    store = JobStore(":memory:")
    await store.open()
    await store.save(_job(policy))
    clock = _Clock()
    handler = _SlowHandler()
    timer = SchedulerTimer(store, handlers={"static_message": handler}, clock=clock)
    await timer._tick()
    await _settle()
    assert handler.started == [1]
    return store, clock, handler, timer


def test_normalize_concurrency_accepts_the_three_policies_and_rejects_others() -> None:
    assert normalize_concurrency(None) is ConcurrencyPolicy.FORBID
    assert normalize_concurrency("") is ConcurrencyPolicy.FORBID
    assert normalize_concurrency(" Replace ") is ConcurrencyPolicy.REPLACE
    assert normalize_concurrency("allow") is ConcurrencyPolicy.ALLOW
    with pytest.raises(ValueError, match="forbid, replace, allow"):
        normalize_concurrency("parallel")


@pytest.mark.asyncio
async def test_forbid_skips_the_overlapping_slot_and_logs_it_once(caplog) -> None:
    store, clock, handler, timer = await _start_first_run(ConcurrencyPolicy.FORBID)
    try:
        clock.wall_now = T0 + timedelta(seconds=61)
        with caplog.at_level(logging.INFO, logger="agentos.scheduler.timer"):
            await timer._tick()
            await timer._tick()
            await _settle()

        assert handler.started == [1]
        assert caplog.text.count("concurrency_skip") == 1

        # When the long run ends, the next slot is computed from that moment.
        clock.wall_now = T0 + timedelta(seconds=150)
        handler.finish(1)
        await asyncio.gather(*list(timer._running.values()))
        job = await store.get("job-forbid")
        assert job is not None
        assert job.status == JobStatus.PENDING
        assert job.next_run_at == T0 + timedelta(seconds=180)
        assert job.run_count == 1
    finally:
        await timer.stop()
        await store.close()


@pytest.mark.asyncio
async def test_replace_cancels_the_running_run_and_starts_the_new_one() -> None:
    store, clock, handler, timer = await _start_first_run(ConcurrencyPolicy.REPLACE)
    try:
        clock.wall_now = T0 + timedelta(seconds=61)
        await timer._tick()
        await _settle()

        assert handler.cancelled == [1]
        assert handler.started == [1, 2]
        job = await store.get("job-replace")
        assert job is not None
        assert job.status == JobStatus.RUNNING
        assert job.reservation_source == "overlap-replace"
        assert job.scheduled_run_at == T0 + timedelta(seconds=60)

        handler.finish(2)
        await asyncio.gather(*list(timer._running.values()))
        job = await store.get("job-replace")
        assert job is not None
        assert job.status == JobStatus.PENDING
        # The replaced run is history, not a failure.
        assert job.consecutive_errors == 0
        assert job.error_count == 0
        assert job.run_count == 1
        runs = await store.list_executions("job-replace")
        replaced = [r for r in runs if r.error == REPLACED_RUN_ERROR]
        assert len(replaced) == 1
        assert replaced[0].delivery_status == "replaced"
        assert [r.summary for r in runs if r.success] == ["run 2"]
    finally:
        await timer.stop()
        await store.close()


@pytest.mark.asyncio
async def test_allow_runs_both_and_the_newest_run_owns_the_job() -> None:
    store, clock, handler, timer = await _start_first_run(ConcurrencyPolicy.ALLOW)
    try:
        first_task = timer._running["job-allow"]
        clock.wall_now = T0 + timedelta(seconds=61)
        await timer._tick()
        await _settle()

        assert handler.started == [1, 2]
        assert handler.cancelled == []
        assert timer._active_count() == 2

        # The newer run finishes first and applies the job's state ...
        clock.wall_now = T0 + timedelta(seconds=70)
        handler.finish(2)
        await timer._running["job-allow"]
        job = await store.get("job-allow")
        assert job is not None and job.status == JobStatus.PENDING
        assert job.next_run_at == T0 + timedelta(seconds=120)

        # ... the earlier one only lands in history.
        handler.finish(1)
        await first_task
        job = await store.get("job-allow")
        assert job is not None
        assert job.run_count == 1
        assert job.next_run_at == T0 + timedelta(seconds=120)
        runs = await store.list_executions("job-allow")
        assert sorted(r.summary or "" for r in runs) == ["run 1", "run 2"]
    finally:
        await timer.stop()
        await store.close()


@pytest.mark.asyncio
async def test_allow_respects_max_concurrent() -> None:
    store = JobStore(":memory:")
    await store.open()
    await store.save(_job(ConcurrencyPolicy.ALLOW))
    clock = _Clock()
    handler = _SlowHandler()
    timer = SchedulerTimer(
        store, handlers={"static_message": handler}, clock=clock, max_concurrent=1
    )
    try:
        await timer._tick()
        await _settle()
        clock.wall_now = T0 + timedelta(seconds=61)
        await timer._tick()
        await _settle()
        assert handler.started == [1]
    finally:
        await timer.stop()
        await store.close()


@pytest.mark.asyncio
async def test_overlap_slot_wakes_the_loop_for_non_forbid_jobs() -> None:
    store, clock, handler, timer = await _start_first_run(ConcurrencyPolicy.REPLACE)
    try:
        # No pending job is due, but the running job's next slot is.
        assert await store.next_due_at() is None
        assert await timer._next_wake_at() == T0 + timedelta(seconds=60)
    finally:
        await timer.stop()
        await store.close()


@pytest.mark.asyncio
async def test_stop_cancels_superseded_allow_runs() -> None:
    store, clock, handler, timer = await _start_first_run(ConcurrencyPolicy.ALLOW)
    clock.wall_now = T0 + timedelta(seconds=61)
    await timer._tick()
    await _settle()

    await timer.stop()
    await _settle()

    assert sorted(handler.cancelled) == [1, 2]
    await store.close()


@pytest.mark.asyncio
async def test_takeover_is_a_compare_and_swap_on_the_running_token() -> None:
    async with JobStore(":memory:") as store:
        await store.save(_job(ConcurrencyPolicy.REPLACE))
        first = await store.reserve_due_job("job-replace", T0)
        assert isinstance(first, JobReservation)
        slot = T0 + timedelta(seconds=60)

        taken = await store.takeover_running_job(
            "job-replace", first.token, slot + timedelta(seconds=1), slot=slot
        )
        stale = await store.takeover_running_job(
            "job-replace", first.token, slot + timedelta(seconds=2), slot=slot
        )

        assert isinstance(taken, JobReservation)
        assert taken.token != first.token
        assert taken.job.scheduled_run_at == slot
        assert not isinstance(stale, JobReservation)
        assert stale.reason == ReservationRejectionReason.STATUS_CONFLICT
        current = await store.get("job-replace")
        assert current is not None and current.reservation_token == taken.token
