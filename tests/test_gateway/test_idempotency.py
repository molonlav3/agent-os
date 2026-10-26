"""``idempotency_key`` makes a retried side-effecting request safe.

A console that lost its socket mid-``chat.send`` could not tell whether the
message went through; resending started a second turn. The ``DEDUPE_*``
constants existed and nothing used them.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest
from gateway_turn_rig import ScriptedRunner, turn_rig

from agentos.gateway import idempotency as idem
from agentos.gateway.idempotency import IdempotencyCache, request_fingerprint
from agentos.gateway.rpc import RpcContext, RpcHandlerError, get_dispatcher
from agentos.gateway.rpc_cron import _handle_cron_add
from agentos.scheduler.payloads import AGENT_TURN_KIND
from agentos.scheduler.types import CronJob, DeliveryConfig, ScheduleKind

# ── the cache ────────────────────────────────────────────────────────────


class _Calls:
    def __init__(self) -> None:
        self.count = 0

    async def __call__(self) -> dict[str, Any]:
        self.count += 1
        return {"n": self.count}


async def test_same_key_and_body_runs_once_and_replays_the_answer() -> None:
    cache = IdempotencyCache()
    call = _Calls()

    first = await cache.run("m", "k1", "fp", call)
    second = await cache.run("m", "k1", "fp", call)

    assert call.count == 1
    assert first == second == {"n": 1}
    # The replay is a copy: a caller mutating it cannot change the next one.
    second["n"] = 99
    assert await cache.run("m", "k1", "fp", call) == {"n": 1}


async def test_same_key_different_body_is_a_conflict() -> None:
    cache = IdempotencyCache()
    await cache.run("m", "k1", "fp-a", _Calls())

    with pytest.raises(RpcHandlerError) as exc_info:
        await cache.run("m", "k1", "fp-b", _Calls())

    assert exc_info.value.code == "INVALID_REQUEST"
    assert exc_info.value.details["code"] == "REQUEST_CONFLICT"


async def test_keys_are_scoped_by_method() -> None:
    cache = IdempotencyCache()
    call = _Calls()
    await cache.run("a", "k1", "fp", call)
    await cache.run("b", "k1", "fp", call)
    assert call.count == 2


async def test_concurrent_duplicates_share_the_first_call() -> None:
    cache = IdempotencyCache()
    gate = asyncio.Event()
    count = 0

    async def _slow() -> dict[str, Any]:
        nonlocal count
        count += 1
        await gate.wait()
        return {"done": True}

    first = asyncio.create_task(cache.run("m", "k", "fp", _slow))
    await asyncio.sleep(0)
    second = asyncio.create_task(cache.run("m", "k", "fp", _slow))
    await asyncio.sleep(0)
    gate.set()

    assert await first == await second == {"done": True}
    assert count == 1


async def test_a_failed_call_is_not_remembered() -> None:
    cache = IdempotencyCache()
    attempts = 0

    async def _flaky() -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient")
        return {"ok": True}

    with pytest.raises(RuntimeError):
        await cache.run("m", "k", "fp", _flaky)
    assert await cache.run("m", "k", "fp", _flaky) == {"ok": True}
    assert attempts == 2


async def test_entries_expire_after_the_ttl_unless_their_run_is_live() -> None:
    now = [0.0]
    cache = IdempotencyCache(ttl_ms=1000, clock=lambda: now[0])
    call = _Calls()
    live = {"task-live"}

    await cache.run("m", "plain", "fp", call)
    await cache.run(
        "m", "pinned", "fp", lambda: _answer("task-live"), pinned=lambda r: r["task_id"] in live
    )
    now[0] = 5.0

    await cache.run("m", "plain", "fp", call)
    assert call.count == 2  # expired: ran again
    replay = await cache.run("m", "pinned", "fp", lambda: _answer("other"))
    assert replay["task_id"] == "task-live"  # its turn still runs: kept


async def _answer(task_id: str) -> dict[str, Any]:
    return {"task_id": task_id}


async def test_the_size_cap_evicts_the_oldest_unpinned_entry() -> None:
    cache = IdempotencyCache(max_entries=2)
    await cache.run("m", "pinned", "fp", lambda: _answer("live"), pinned=lambda r: True)
    await cache.run("m", "a", "fp", lambda: _answer("a"))
    await cache.run("m", "b", "fp", lambda: _answer("b"))

    assert len(cache) == 2
    assert (await cache.run("m", "pinned", "fp", lambda: _answer("x")))["task_id"] == "live"
    assert (await cache.run("m", "a", "fp", lambda: _answer("again")))["task_id"] == "again"


def test_the_fingerprint_ignores_the_key_itself() -> None:
    assert request_fingerprint({"a": 1, "idempotency_key": "x"}) == request_fingerprint(
        {"idempotencyKey": "y", "a": 1}
    )
    assert request_fingerprint({"a": 1}) != request_fingerprint({"a": 2})


def test_a_malformed_key_is_rejected() -> None:
    with pytest.raises(ValueError):
        idem.idempotency_key({"idempotency_key": ""})
    with pytest.raises(ValueError):
        idem.idempotency_key({"idempotency_key": "x" * 300})
    assert idem.idempotency_key({}) is None


# ── methods ──────────────────────────────────────────────────────────────


@pytest.fixture
def fresh_cache(monkeypatch: pytest.MonkeyPatch) -> IdempotencyCache:
    cache = IdempotencyCache()
    monkeypatch.setattr(idem, "_cache", cache)
    return cache


async def test_chat_send_with_a_retried_key_starts_one_turn(
    tmp_path: Path, fresh_cache: IdempotencyCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = ScriptedRunner(["ok"], block=False)
    async with turn_rig(tmp_path, runner) as rig:
        ctx = rig.ctx(rig.live.conn_id)
        params = {"sessionKey": rig.key, "message": "hello", "idempotency_key": "send-1"}
        first = await get_dispatcher().dispatch("1", "chat.send", dict(params), ctx)
        second = await get_dispatcher().dispatch("2", "chat.send", dict(params), ctx)
        conflict = await get_dispatcher().dispatch(
            "3", "chat.send", {**params, "message": "something else"}, ctx
        )
        await rig.runtime.wait(first.payload["task_id"], timeout=3)

        # A restart loses the in-memory cache; the key on the task survives.
        monkeypatch.setattr(idem, "_cache", IdempotencyCache())
        after_restart = await get_dispatcher().dispatch("4", "chat.send", dict(params), ctx)
        tasks = await rig.runtime.list(session_key=rig.key)
        transcript = await rig.sessions.get_transcript(rig.key)

    assert first.ok, first.error
    assert second.payload == first.payload
    assert after_restart.ok, after_restart.error
    assert after_restart.payload["task_id"] == first.payload["task_id"]
    assert len(tasks) == 1
    assert [e.content for e in transcript if e.role == "user"] == ["hello"]
    assert conflict.ok is False
    assert conflict.error is not None
    assert conflict.error.code == "INVALID_REQUEST"
    assert conflict.error.details["code"] == "REQUEST_CONFLICT"


async def test_sessions_send_dedupes_by_key(tmp_path: Path, fresh_cache: IdempotencyCache) -> None:
    async with turn_rig(tmp_path, ScriptedRunner(["ok"], block=False)) as rig:
        ctx = rig.ctx(rig.live.conn_id)
        params = {"key": rig.key, "message": "hi", "idempotency_key": f"s-{uuid.uuid4()}"}
        first = await get_dispatcher().dispatch("1", "sessions.send", dict(params), ctx)
        second = await get_dispatcher().dispatch("2", "sessions.send", dict(params), ctx)
        await rig.runtime.wait(first.payload["task_id"], timeout=3)
        tasks = await rig.runtime.list(session_key=rig.key)

    assert first.ok, first.error
    assert second.payload == first.payload
    assert len(tasks) == 1


async def test_sessions_create_with_a_retried_key_creates_one_session(
    tmp_path: Path, fresh_cache: IdempotencyCache
) -> None:
    async with turn_rig(tmp_path, ScriptedRunner([])) as rig:
        ctx = rig.ctx(rig.live.conn_id)
        params = {"agentId": "main", "idempotency_key": "create-1"}
        first = await get_dispatcher().dispatch("1", "sessions.create", dict(params), ctx)
        second = await get_dispatcher().dispatch("2", "sessions.create", dict(params), ctx)
        without = await get_dispatcher().dispatch("3", "sessions.create", {"agentId": "main"}, ctx)

    assert first.ok, first.error
    assert second.payload == first.payload
    assert without.payload["key"] != first.payload["key"]


class _Scheduler:
    def __init__(self) -> None:
        self.added = 0

    async def add_job(self, **kwargs: Any) -> CronJob:
        self.added += 1
        value = kwargs.get("schedule_value", "")
        return CronJob(
            id=f"job-{self.added}",
            name=kwargs["name"],
            cron_expr=value,
            schedule_raw=value,
            schedule_kind=kwargs.get("schedule_kind") or ScheduleKind.CRON,
            handler_key=kwargs["handler_key"],
            payload=kwargs["payload"],
            session_target=kwargs["session_target"],
            session_key=kwargs.get("session_key", ""),
            origin_session_key=kwargs.get("origin_session_key", ""),
            delivery=kwargs.get("delivery") or DeliveryConfig(),
            tz="",
        )


async def test_cron_add_with_a_retried_key_adds_one_job(fresh_cache: IdempotencyCache) -> None:
    scheduler = _Scheduler()
    ctx = RpcContext(conn_id="test", cron_scheduler=scheduler)
    params = {
        "name": "five",
        "schedule": {"kind": "cron", "expr": "*/5 * * * *"},
        "payloadKind": AGENT_TURN_KIND,
        "text": "ping",
        "agentId": "main",
        "idempotency_key": "cron-1",
    }

    first = await _handle_cron_add(dict(params), ctx)
    second = await _handle_cron_add(dict(params), ctx)

    assert scheduler.added == 1
    assert second == first


async def test_approval_resolve_accepts_a_key(
    tmp_path: Path, fresh_cache: IdempotencyCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentos.application import approval_queue as queue_mod

    queue = queue_mod.ApprovalQueue(db_path=str(tmp_path / "q.sqlite"))
    monkeypatch.setattr(queue_mod, "_queue", queue)
    try:
        approval_id = queue.request("exec", {"toolName": "bash", "command": "ls"})
        ctx = RpcContext(conn_id="test")
        params = {"id": approval_id, "approved": True, "idempotency_key": "r-1"}
        first = await get_dispatcher().dispatch("1", "exec.approval.resolve", dict(params), ctx)
        second = await get_dispatcher().dispatch("2", "exec.approval.resolve", dict(params), ctx)
        flipped = await get_dispatcher().dispatch(
            "3", "exec.approval.resolve", {**params, "approved": False}, ctx
        )
    finally:
        queue.close()

    assert first.ok and second.ok
    assert second.payload == first.payload
    assert flipped.ok is False
    assert flipped.error is not None and flipped.error.details["code"] == "REQUEST_CONFLICT"
