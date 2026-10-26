"""Post-boot deferral of missed agent jobs (#3683).

Missed agent turns wait ``startup_agent_delay_seconds`` after boot so they do
not compete with channel start-up for the model budget; jobs that never call
a model are caught up at once. The cap of five catch-up jobs and the 10 s
stagger are unchanged.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from agentos.scheduler import timer as timer_module
from agentos.scheduler.engine import DEFAULT_STARTUP_AGENT_DELAY_SECONDS, SchedulerEngine
from agentos.scheduler.persistence import JobStore
from agentos.scheduler.timer import AGENT_HANDLER_KEYS, SchedulerClock, SchedulerTimer
from agentos.scheduler.types import CronJob, JobStatus, ScheduleKind, SessionTarget

BOOT = datetime(2026, 3, 2, 8, 0, 0, tzinfo=UTC)

_PAYLOADS = {
    "agent_run": ({"kind": "agent_turn", "task": "noop", "agent_id": "main"}, "isolated"),
    "system_event": ({"kind": "system_event", "text": "check in"}, "main"),
    "static_message": ({"kind": "reminder", "text": "stand up"}, "isolated"),
    "script_run": ({"kind": "script", "script": "watch.sh"}, "isolated"),
}


class _StillClock(SchedulerClock):
    def __init__(self, wall: datetime) -> None:
        self.wall_now = wall

    def wall(self) -> datetime:
        return self.wall_now

    def monotonic(self) -> float:
        return 0.0


def _missed(job_id: str, handler_key: str, *, minutes_late: int = 30) -> CronJob:
    payload, target = _PAYLOADS[handler_key]
    return CronJob(
        id=job_id,
        name=job_id,
        cron_expr="0 * * * *",
        schedule_raw="0 * * * *",
        handler_key=handler_key,
        payload=dict(payload),
        session_target=SessionTarget(target),
        schedule_kind=ScheduleKind.CRON,
        next_run_at=BOOT - timedelta(minutes=minutes_late),
        status=JobStatus.PENDING,
    )


@pytest.fixture
def fixed_stagger(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], float]]:
    """Deterministic stagger: job i of n waits i + 1 seconds; records each call.

    Never zero, so a staggered catch-up run is still pending when the test
    inspects the store (the tests register no handlers).
    """
    calls: list[tuple[list[str], float]] = []

    def _spread(job_ids: list[str], *, window_seconds: float) -> dict[str, float]:
        calls.append((list(job_ids), window_seconds))
        return {job_id: float(i + 1) for i, job_id in enumerate(sorted(job_ids))}

    monkeypatch.setattr(timer_module, "spread_jobs", _spread)
    return calls


async def _stop(timer: SchedulerTimer) -> None:
    await timer.stop()
    await asyncio.sleep(0)


def test_agent_handler_keys_are_the_model_driving_handlers() -> None:
    assert AGENT_HANDLER_KEYS == frozenset({"agent_run", "system_event"})


@pytest.mark.asyncio
async def test_missed_agent_jobs_are_deferred_and_others_run_now(fixed_stagger) -> None:
    async with JobStore(":memory:") as store:
        for key in _PAYLOADS:
            await store.save(_missed(f"job-{key}", key))
        timer = SchedulerTimer(
            store,
            handlers={},
            clock=_StillClock(BOOT),
            startup_agent_delay_seconds=120.0,
        )

        await timer.startup_catchup()

        for key in ("agent_run", "system_event"):
            job = await store.get(f"job-{key}")
            assert job is not None
            # Not reserved: still pending, honest next_run_at = boot + 120 + stagger.
            assert job.status == JobStatus.PENDING
            assert job.reservation_token == ""
            assert job.next_run_at is not None
            offset = (job.next_run_at - BOOT).total_seconds()
            assert 120.0 <= offset < 120.0 + timer.CATCHUP_STAGGER_SECONDS
        for key in ("static_message", "script_run"):
            job = await store.get(f"job-{key}")
            assert job is not None
            assert job.status == JobStatus.RUNNING
            assert job.reservation_source == "startup"
        assert len(timer._catchup_tasks) == 2
        # Both groups are staggered over the same 10 s window.
        assert all(window == timer.CATCHUP_STAGGER_SECONDS for _, window in fixed_stagger)
        await _stop(timer)


@pytest.mark.asyncio
async def test_deferred_agent_job_survives_a_crash_and_runs_after_the_delay(
    fixed_stagger,
) -> None:
    ran: list[str] = []

    async def agent(job: CronJob) -> str:
        ran.append(job.id)
        return "ok"

    async with JobStore(":memory:") as store:
        await store.save(_missed("nightly", "agent_run"))
        first = SchedulerTimer(
            store, handlers={}, clock=_StillClock(BOOT), startup_agent_delay_seconds=120.0
        )
        await first.startup_catchup()
        deferred = await store.get("nightly")
        assert deferred is not None
        run_at = deferred.next_run_at
        await _stop(first)

        # The gateway crashes and boots again 30 s later: the job is no longer
        # "missed", so the new boot neither re-defers nor runs it early.
        clock = _StillClock(BOOT + timedelta(seconds=30))
        second = SchedulerTimer(
            store,
            handlers={"agent_run": agent},
            clock=clock,
            startup_agent_delay_seconds=120.0,
        )
        await second.startup_catchup()
        again = await store.get("nightly")
        assert again is not None and again.next_run_at == run_at
        await second._tick()
        assert ran == []

        # Once the deferred time arrives the ordinary tick runs it, once.
        assert run_at is not None
        clock.wall_now = run_at
        await second._tick()
        await asyncio.gather(*list(second._running.values()))
        await second._tick()
        assert ran == ["nightly"]
        await _stop(second)


@pytest.mark.asyncio
async def test_zero_delay_keeps_the_immediate_catch_up(fixed_stagger) -> None:
    async with JobStore(":memory:") as store:
        await store.save(_missed("nightly", "agent_run"))
        timer = SchedulerTimer(
            store, handlers={}, clock=_StillClock(BOOT), startup_agent_delay_seconds=0.0
        )

        await timer.startup_catchup()

        job = await store.get("nightly")
        assert job is not None and job.status == JobStatus.RUNNING
        assert len(timer._catchup_tasks) == 1
        await _stop(timer)


@pytest.mark.asyncio
async def test_catch_up_cap_counts_deferred_jobs_and_fast_forwards_the_rest(
    fixed_stagger,
) -> None:
    async with JobStore(":memory:") as store:
        # Seven missed jobs, oldest first: three agent turns, four reminders.
        keys = ["agent_run", "static_message"] * 3 + ["static_message"]
        for i, key in enumerate(keys):
            await store.save(_missed(f"job-{i}", key, minutes_late=60 - i))
        timer = SchedulerTimer(
            store,
            handlers={},
            clock=_StillClock(BOOT),
            max_catchup=5,
            startup_agent_delay_seconds=120.0,
        )

        await timer.startup_catchup()

        statuses = {f"job-{i}": await store.get(f"job-{i}") for i in range(len(keys))}
        caught_up = ["job-0", "job-1", "job-2", "job-3", "job-4"]
        overflow = ["job-5", "job-6"]
        deferred = [jid for jid in caught_up if keys[int(jid[-1])] == "agent_run"]
        immediate = [jid for jid in caught_up if keys[int(jid[-1])] != "agent_run"]
        assert deferred == ["job-0", "job-2", "job-4"]
        for jid in deferred:
            job = statuses[jid]
            assert job is not None and job.status == JobStatus.PENDING
            assert job.next_run_at is not None and job.next_run_at >= BOOT + timedelta(minutes=2)
        for jid in immediate:
            job = statuses[jid]
            assert job is not None and job.status == JobStatus.RUNNING
        for jid in overflow:
            job = statuses[jid]
            assert job is not None and job.status == JobStatus.PENDING
            # Fast-forwarded to the next hourly slot, not deferred and not run.
            assert job.next_run_at == BOOT + timedelta(hours=1)
        await _stop(timer)


@pytest.mark.asyncio
async def test_engine_defaults_to_a_two_minute_agent_delay() -> None:
    assert DEFAULT_STARTUP_AGENT_DELAY_SECONDS == 120.0
    async with JobStore(":memory:") as store:
        assert SchedulerEngine(store)._timer._startup_agent_delay == 120.0
        configured = SchedulerEngine(store, config={"startup_agent_delay_seconds": 15})
        assert configured._timer._startup_agent_delay == 15.0
