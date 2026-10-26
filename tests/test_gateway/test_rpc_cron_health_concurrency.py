"""cron.* RPC carries job health and the concurrency policy (console contract).

The field names are shared with the web console and the desktop schedule
view: ``health.consecutive_failures``, ``health.last_error``,
``health.degraded`` and ``concurrency``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from agentos.gateway.rpc import RpcContext
from agentos.gateway.rpc_cron import (
    _handle_cron_add,
    _handle_cron_list,
    _handle_cron_status,
    _handle_cron_update,
    _job_to_wire,
)
from agentos.scheduler.engine import SchedulerEngine
from agentos.scheduler.persistence import JobStore
from agentos.scheduler.types import ConcurrencyPolicy, CronJob

_ADD = {
    "name": "nightly digest",
    "schedule": {"kind": "cron", "expr": "0 2 * * *"},
    "payloadKind": "reminder",
    "text": "digest",
    "delivery": {"mode": "none"},
}


@pytest.fixture
async def ctx() -> AsyncIterator[RpcContext]:
    store = JobStore(":memory:")
    await store.open()
    engine = SchedulerEngine(store)
    try:
        yield RpcContext(conn_id="test", cron_scheduler=engine)
    finally:
        await store.close()


def test_wire_health_block_and_default_concurrency() -> None:
    wire = _job_to_wire(CronJob(id="j", consecutive_errors=3, last_error="HTTP 503"))

    assert wire["health"] == {
        "consecutive_failures": 3,
        "last_error": "HTTP 503",
        "degraded": True,
    }
    assert wire["concurrency"] == "forbid"


def test_wire_healthy_job() -> None:
    wire = _job_to_wire(CronJob(id="j", concurrency=ConcurrencyPolicy.ALLOW))

    assert wire["health"] == {"consecutive_failures": 0, "last_error": None, "degraded": False}
    assert wire["concurrency"] == "allow"


@pytest.mark.asyncio
async def test_cron_add_persists_concurrency_and_list_reports_it(ctx: RpcContext) -> None:
    created = await _handle_cron_add({**_ADD, "concurrency": "replace"}, ctx)

    assert created["concurrency"] == "replace"
    listed = await _handle_cron_list(None, ctx)
    assert [row["concurrency"] for row in listed] == ["replace"]
    assert listed[0]["health"] == {
        "consecutive_failures": 0,
        "last_error": None,
        "degraded": False,
    }


@pytest.mark.asyncio
async def test_cron_add_defaults_to_forbid(ctx: RpcContext) -> None:
    created = await _handle_cron_add(dict(_ADD), ctx)

    assert created["concurrency"] == "forbid"


@pytest.mark.asyncio
async def test_cron_add_rejects_an_unknown_policy(ctx: RpcContext) -> None:
    with pytest.raises(ValueError, match="concurrency must be one of forbid, replace, allow"):
        await _handle_cron_add({**_ADD, "concurrency": "parallel"}, ctx)
    assert await _handle_cron_list(None, ctx) == []


@pytest.mark.asyncio
async def test_cron_update_changes_only_the_policy(ctx: RpcContext) -> None:
    created = await _handle_cron_add(dict(_ADD), ctx)

    updated = await _handle_cron_update({"id": created["id"], "concurrency": "allow"}, ctx)

    assert updated["concurrency"] == "allow"
    assert updated["expression"] == created["expression"]
    status = await _handle_cron_status({"id": created["id"]}, ctx)
    assert status["concurrency"] == "allow"
    with pytest.raises(ValueError, match="concurrency must be one of"):
        await _handle_cron_update({"id": created["id"], "concurrency": "maybe"}, ctx)


@pytest.mark.asyncio
async def test_list_reports_a_degraded_job(ctx: RpcContext) -> None:
    created = await _handle_cron_add(dict(_ADD), ctx)
    scheduler = ctx.cron_scheduler
    job = await scheduler.get_job(created["id"])
    job.consecutive_errors = 4
    job.last_error = "Timeout after 600.0s"
    await scheduler._store.save(job)

    listed = await _handle_cron_list(None, ctx)

    assert listed[0]["health"] == {
        "consecutive_failures": 4,
        "last_error": "Timeout after 600.0s",
        "degraded": True,
    }
