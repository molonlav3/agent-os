"""Persistence of the concurrency policy and the inputs of job health."""

from __future__ import annotations

import importlib.util
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from agentos.scheduler.jobs import apply_reserved_result
from agentos.scheduler.ops import SchedulerOps
from agentos.scheduler.persistence import JobStore
from agentos.scheduler.types import (
    ConcurrencyPolicy,
    CronJob,
    JobExecution,
    JobReservation,
    JobStatus,
    ScheduleKind,
    SessionTarget,
    job_health,
)

_LEGACY_JOBS_TABLE = """
CREATE TABLE scheduler_jobs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    cron_expr TEXT NOT NULL,
    handler_key TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_run_at TEXT,
    next_run_at TEXT,
    run_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    max_retries INTEGER NOT NULL DEFAULT 3,
    jitter_seconds REAL NOT NULL DEFAULT 0.0
)
"""


def _job(**overrides) -> CronJob:
    fields = {
        "id": "job-1",
        "name": "job-1",
        "cron_expr": "* * * * *",
        "handler_key": "static_message",
        "payload": {"kind": "reminder", "text": "ping"},
        "session_target": SessionTarget.ISOLATED,
        "schedule_kind": ScheduleKind.CRON,
        "next_run_at": datetime.now(UTC) - timedelta(seconds=5),
        "status": JobStatus.PENDING,
    }
    fields.update(overrides)
    return CronJob(**fields)


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", list(ConcurrencyPolicy))
async def test_concurrency_round_trips_through_the_store(policy: ConcurrencyPolicy) -> None:
    async with JobStore(":memory:") as store:
        await store.save(_job(concurrency=policy))
        loaded = await store.get("job-1")
    assert loaded is not None and loaded.concurrency is policy


@pytest.mark.asyncio
async def test_a_legacy_store_gains_the_column_with_the_forbid_default(tmp_path: Path) -> None:
    db_path = tmp_path / "scheduler.db"
    conn = sqlite3.connect(db_path)
    conn.execute(_LEGACY_JOBS_TABLE)
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT INTO scheduler_jobs (id, name, cron_expr, handler_key, payload, created_at,"
        " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("legacy", "legacy", "* * * * *", "static_message", '{"text": "hi"}', now, now),
    )
    conn.commit()
    conn.close()

    async with JobStore(str(db_path)) as store:
        loaded = await store.get("legacy")
    assert loaded is not None and loaded.concurrency is ConcurrencyPolicy.FORBID


@pytest.mark.asyncio
async def test_an_unknown_stored_policy_reads_back_as_forbid() -> None:
    async with JobStore(":memory:") as store:
        await store.save(_job())
        await store._db().execute("UPDATE scheduler_jobs SET concurrency = 'bogus'")
        loaded = await store.get("job-1")
    assert loaded is not None and loaded.concurrency is ConcurrencyPolicy.FORBID


@pytest.mark.asyncio
async def test_ops_add_and_update_validate_and_persist_the_policy() -> None:
    async with JobStore(":memory:") as store:
        ops = SchedulerOps(store, max_jitter=0.0)
        job = await ops.add(
            name="nightly",
            schedule_kind=ScheduleKind.CRON,
            schedule_value="0 2 * * *",
            handler_key="static_message",
            payload={"kind": "reminder", "text": "ping"},
            concurrency="replace",
        )
        assert job.concurrency is ConcurrencyPolicy.REPLACE
        updated = await ops.update(job.id, concurrency="allow")
        assert updated is not None and updated.concurrency is ConcurrencyPolicy.ALLOW
        assert (await store.get(job.id)).concurrency is ConcurrencyPolicy.ALLOW  # type: ignore[union-attr]
        with pytest.raises(ValueError, match="concurrency must be one of"):
            await ops.update(job.id, concurrency="sometimes")
        with pytest.raises(ValueError, match="concurrency must be one of"):
            await ops.add(
                name="bad",
                schedule_kind=ScheduleKind.CRON,
                schedule_value="0 2 * * *",
                handler_key="static_message",
                payload={"kind": "reminder", "text": "ping"},
                concurrency="sometimes",
            )


@pytest.mark.asyncio
async def test_failure_streak_and_last_error_drive_health() -> None:
    async with JobStore(":memory:") as store:
        await store.save(_job())
        base = datetime.now(UTC)
        for attempt in range(3):
            # An hour apart, so each failure's retry backoff has expired.
            at = base + timedelta(hours=attempt)
            reservation = await store.reserve_manual_job("job-1", at)
            assert isinstance(reservation, JobReservation)
            failed = JobExecution(job_id="job-1", success=False, error=f"boom {attempt}")
            assert await apply_reserved_result("job-1", reservation.token, failed, store, now=at)
        job = await store.get("job-1")
        assert job is not None
        assert job_health(job.consecutive_errors, job.last_error) == {
            "consecutive_failures": 3,
            "last_error": "boom 2",
            "degraded": True,
        }

        # A run in progress keeps showing why the job is degraded.
        running = await store.reserve_manual_job("job-1", base + timedelta(hours=3))
        assert isinstance(running, JobReservation)
        job = await store.get("job-1")
        assert job is not None and job.last_error == "boom 2"

        # A success clears the streak and the error.
        ok = JobExecution(job_id="job-1", success=True)
        assert await apply_reserved_result("job-1", running.token, ok, store)
        job = await store.get("job-1")
        assert job is not None
        assert job_health(job.consecutive_errors, job.last_error) == {
            "consecutive_failures": 0,
            "last_error": None,
            "degraded": False,
        }


def test_job_health_degrades_from_three_failures() -> None:
    assert job_health(2, "x")["degraded"] is False
    assert job_health(3, "x")["degraded"] is True
    assert job_health(None, None) == {
        "consecutive_failures": 0,
        "last_error": None,
        "degraded": False,
    }


# ── yoyo migration V013 ─────────────────────────────────────────────────────


def _load_v013():
    path = Path(__file__).resolve().parents[2] / "migrations" / "V013__scheduler_job_concurrency.py"
    spec = importlib.util.spec_from_file_location("v013_concurrency", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with patch("yoyo.step", lambda apply, rollback: (apply, rollback)):
        spec.loader.exec_module(module)
    return module


def test_v013_adds_the_column_idempotently_and_rolls_back() -> None:
    migration = _load_v013()
    assert migration.__depends__ == {"V012__projects_name_unique"}
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(_LEGACY_JOBS_TABLE)
        migration.apply_step(conn)
        migration.apply_step(conn)
        columns = {row[1]: row for row in conn.execute("PRAGMA table_info(scheduler_jobs)")}
        assert "concurrency" in columns
        assert columns["concurrency"][4] == "'forbid'"
        migration.rollback_step(conn)
        columns_after = {row[1] for row in conn.execute("PRAGMA table_info(scheduler_jobs)")}
        assert "concurrency" not in columns_after
    finally:
        conn.close()


def test_v013_is_a_no_op_without_a_scheduler_table() -> None:
    migration = _load_v013()
    conn = sqlite3.connect(":memory:")
    try:
        migration.apply_step(conn)
        migration.rollback_step(conn)
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        assert "scheduler_jobs" not in tables
    finally:
        conn.close()
