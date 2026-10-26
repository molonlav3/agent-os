"""``cron.run.finished`` goes out only once the run is on record (#3699).

The event used to be sent from the delivery chain inside the handler, before
the scheduler saved the run and applied its result, so a client that re-read
``cron.list`` on it saw the previous health. These tests use the real
``JobStore``, ``SchedulerEngine`` and ``DeliveryChain``; the WS emitter reads
the store at the moment the event arrives.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from agentos.scheduler.delivery import DeliveryChain
from agentos.scheduler.engine import SchedulerEngine
from agentos.scheduler.persistence import JobStore
from agentos.scheduler.types import (
    CronJob,
    JobExecution,
    JobReservation,
    JobStatus,
    ScheduleKind,
    SessionTarget,
)

# A reminder payload is stored under this key (the store normalizes the contract).
HANDLER = "static_message"


class Seen:
    """What the store said when each event arrived, next to the event."""

    def __init__(self, store: JobStore) -> None:
        self.store = store
        self.events: list[dict[str, Any]] = []

    async def emit(self, topic: str, event: str, payload: dict[str, Any]) -> int:
        job = await self.store.get(payload["jobId"])
        runs = await self.store.list_executions(payload["jobId"])
        self.events.append(
            {
                "topic": topic,
                "event": event,
                "payload": payload,
                "stored_runs": [run.id for run in runs],
                "stored_failures": job.consecutive_errors if job else None,
                "stored_last_error": job.last_error if job else None,
                "stored_status": job.status.value if job else None,
            }
        )
        return 1


def _wire(store: JobStore, outcomes: list[str | None]) -> tuple[SchedulerEngine, Seen]:
    """An engine whose handler raises ``outcomes.pop(0)`` (``None`` = success)."""
    engine = SchedulerEngine(store=store, config={"startup_agent_delay_seconds": 0})
    seen = Seen(store)
    chain = DeliveryChain(ws_emitter=seen.emit, job_serializer=lambda job: {"id": job.id})

    async def handler(job: CronJob) -> str:
        outcome = outcomes.pop(0)
        if outcome is not None:
            raise RuntimeError(outcome)
        return "all good"

    engine.register_handler(HANDLER, handler)
    engine.add_run_listener(chain.publish_run_finished)
    return engine, seen


async def _add_job(store: JobStore, **overrides: Any) -> CronJob:
    fields: dict[str, Any] = {
        "id": "job-1",
        "name": "Flaky check",
        "cron_expr": "3600",
        "schedule_kind": ScheduleKind.EVERY,
        "handler_key": HANDLER,
        "payload": {"kind": "reminder", "text": "check"},
        "session_target": SessionTarget.ISOLATED,
        "next_run_at": datetime.now(UTC) - timedelta(seconds=5),
        "status": JobStatus.PENDING,
    }
    fields.update(overrides)
    job = CronJob(**fields)
    await store.save(job)
    return job


@pytest.mark.asyncio
async def test_the_event_arrives_after_the_run_is_readable_and_carries_health() -> None:
    async with JobStore(":memory:") as store:
        job = await _add_job(store)
        engine, seen = _wire(store, ["boom 1", "boom 2", "boom 3", None])

        for _ in range(4):
            result = await engine.run_job_now(job.id)
            assert result.execution is not None
            # A failure starts a backoff that would refuse the next manual
            # run; the backoff is not what this test is about.
            current = await store.get(job.id)
            assert current is not None
            current.backoff_until = None
            await store.save(current, write_reservation=False)

    assert [e["event"] for e in seen.events] == ["cron.run.finished"] * 4
    assert {e["topic"] for e in seen.events} == {"cron:job-1"}
    for index, event in enumerate(seen.events[:3], start=1):
        payload = event["payload"]
        # On arrival, the store already holds this run and its result...
        assert payload["runId"] in event["stored_runs"]
        assert len(event["stored_runs"]) == index
        assert event["stored_failures"] == index
        assert event["stored_last_error"] == f"boom {index}"
        # ...and the event says the same, so no re-read is needed.
        assert payload["health"] == {
            "consecutive_failures": index,
            "last_error": f"boom {index}",
            "degraded": index >= 3,
        }
        assert payload["success"] is False
        assert payload["status"] == "error"
        assert payload["error"] == f"boom {index}"
        assert payload["lastResult"] == f"boom {index}"
        assert payload["lastStatus"] == "error"
        assert payload["job"] == {"id": "job-1"}

    recovered = seen.events[3]["payload"]
    assert recovered["success"] is True and recovered["status"] == "ok"
    assert recovered["health"] == {
        "consecutive_failures": 0,
        "last_error": None,
        "degraded": False,
    }
    assert recovered["lastStatus"] == "ok"
    assert recovered["summary"] == "all good"
    assert seen.events[3]["stored_failures"] == 0
    assert recovered["nextRun"] is not None


@pytest.mark.asyncio
async def test_timer_runs_publish_after_the_record_too() -> None:
    async with JobStore(":memory:") as store:
        job = await _add_job(store)
        engine, seen = _wire(store, ["boom"])
        reservation = await store.reserve_due_job(
            job.id, datetime.now(UTC), source="timer", owner="test"
        )
        assert isinstance(reservation, JobReservation)
        await engine._timer._run_single(reservation)

    (event,) = seen.events
    assert event["payload"]["runId"] in event["stored_runs"]
    assert event["stored_failures"] == 1
    assert event["stored_status"] == "pending"  # the reservation is released
    assert event["payload"]["health"]["consecutive_failures"] == 1
    assert event["payload"]["jobStatus"] == "pending"


@pytest.mark.asyncio
async def test_a_one_shot_job_the_run_deleted_still_gets_its_event() -> None:
    async with JobStore(":memory:") as store:
        job = await _add_job(
            store,
            id="once",
            cron_expr="",
            schedule_kind=ScheduleKind.AT,
            delete_after_run=True,
        )
        engine, seen = _wire(store, [None])
        await engine.run_job_now(job.id)

    (event,) = seen.events
    payload = event["payload"]
    assert payload["jobId"] == "once"
    assert payload["jobDeleted"] is True
    assert payload["health"] is None
    assert payload["success"] is True


@pytest.mark.asyncio
async def test_a_failing_listener_never_breaks_the_run() -> None:
    async with JobStore(":memory:") as store:
        job = await _add_job(store)
        engine, seen = _wire(store, [None])

        async def broken(_job: CronJob | None, _execution: JobExecution) -> None:
            raise RuntimeError("listener bug")

        engine._timer._run_listeners.insert(0, broken)
        result = await engine.run_job_now(job.id)
        stored = await store.get(job.id)

    assert result.success is True
    assert stored is not None and stored.run_count == 1
    assert len(seen.events) == 1  # the next listener still ran


@pytest.mark.asyncio
async def test_delivery_only_queues_the_event() -> None:
    sent: list[tuple[str, str, dict[str, Any]]] = []

    async def emit(topic: str, event: str, payload: dict[str, Any]) -> int:
        sent.append((topic, event, payload))
        return 1

    chain = DeliveryChain(ws_emitter=emit)
    job = CronJob(id="j", name="J", handler_key=HANDLER)
    report = await chain.deliver(
        job, result_text="done", success=True, summary="done", session_key="cron:j"
    )
    assert report.ws_status == "queued"
    assert sent == []
    assert (await DeliveryChain().deliver(job, "done", True, "done", "cron:j")).ws_status == (
        "skipped"
    )
