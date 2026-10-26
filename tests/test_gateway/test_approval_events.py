"""Approval queue changes are pushed to Control connections as events.

Consoles used to learn about a prompt by polling ``GET /api/approvals`` every
1.5s backing off to 30s, so a prompt could surface half a minute late.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from agentos.application.approval_events import (
    APPROVAL_REQUESTED,
    APPROVAL_RESOLVED,
    add_approval_listener,
)
from agentos.application.approval_queue import ApprovalQueue
from agentos.gateway.access import ConnectionSurface
from agentos.gateway.agent_surface import AgentBinding
from agentos.gateway.approval_broadcast import ApprovalBroadcaster
from agentos.gateway.auth import AccessContext
from agentos.gateway.protocol import CAPABILITIES
from agentos.gateway.websocket import ConnectionRegistry, _build_features


@pytest.fixture
def queue(tmp_path: Path) -> Iterator[ApprovalQueue]:
    q = ApprovalQueue(db_path=str(tmp_path / "approvals.sqlite"), default_timeout=60.0)
    try:
        yield q
    finally:
        q.close()


@pytest.fixture
def events() -> Iterator[list[tuple[str, dict[str, Any]]]]:
    seen: list[tuple[str, dict[str, Any]]] = []
    remove = add_approval_listener(lambda name, payload: seen.append((name, payload)))
    try:
        yield seen
    finally:
        remove()


EXEC_PARAMS = {
    "toolName": "bash",
    "command": "rm -rf build",
    "sessionKey": "agent:main:webchat:abc",
}


def test_queueing_an_approval_emits_requested(queue: ApprovalQueue, events: list) -> None:
    approval_id = queue.request("exec", dict(EXEC_PARAMS), ttl=30)

    assert len(events) == 1
    name, payload = events[0]
    assert name == APPROVAL_REQUESTED
    assert payload["id"] == approval_id
    assert payload["namespace"] == "exec"
    assert payload["session_key"] == "agent:main:webchat:abc"
    assert payload["tool"] == "bash"
    assert payload["command"] == "rm -rf build"
    assert payload["summary"] == "bash: rm -rf build"
    assert payload["expires_at"] == pytest.approx(payload["created_at"] + 30)
    # The same item GET /api/approvals lists, so a console can render it as is.
    item = payload["approval"]
    assert item["id"] == approval_id
    assert item["toolName"] == "bash"
    assert item["sessionKey"] == "agent:main:webchat:abc"
    assert item["title"] == "bash: rm -rf build"


def test_unknown_fields_are_omitted(queue: ApprovalQueue, events: list) -> None:
    queue.request("plugin", {"pluginId": "weather", "version": "1", "permissions": ["net"]})

    _, payload = events[0]
    assert "session_key" not in payload
    assert "command" not in payload
    assert payload["tool"] == "weather"
    assert payload["summary"] == "Plugin weather requests permissions"


@pytest.mark.parametrize("namespace", ["exec", "plugin", "acp", "acp_elicitation"])
def test_every_namespace_emits_both_events(
    queue: ApprovalQueue, events: list, namespace: str
) -> None:
    approval_id = queue.request(namespace, {"sessionKey": "agent:main:main"})
    queue.resolve(approval_id, False)

    assert [name for name, _ in events] == [APPROVAL_REQUESTED, APPROVAL_RESOLVED]
    assert all(payload["namespace"] == namespace for _, payload in events)


def test_resolving_emits_resolved_with_the_decision(queue: ApprovalQueue, events: list) -> None:
    approved = queue.request("exec", dict(EXEC_PARAMS))
    denied = queue.request("exec", dict(EXEC_PARAMS))
    events.clear()

    queue.resolve(approved, True)
    queue.resolve(denied, False, resolved_by="policy")
    # Resolving again with the same answer is a no-op, not a second event.
    queue.resolve(approved, True)

    assert [(name, p["id"], p["decision"], p["resolved_by"]) for name, p in events] == [
        (APPROVAL_RESOLVED, approved, "approved", "operator"),
        (APPROVAL_RESOLVED, denied, "denied", "policy"),
    ]
    assert all(isinstance(p["resolved_at"], float) for _, p in events)


def test_session_elevation_resolves_are_attributed(queue: ApprovalQueue, events: list) -> None:
    queue.request("exec", dict(EXEC_PARAMS))
    events.clear()

    assert queue.resolve_pending_for_session("agent:main:webchat:abc", approved=True) == 1

    assert events[0][1]["resolved_by"] == "session_elevation"


async def test_an_approval_that_times_out_emits_expired(tmp_path: Path, events: list) -> None:
    q = ApprovalQueue(
        db_path=str(tmp_path / "expiry.sqlite"), default_timeout=0.05, poll_interval=0.01
    )
    try:
        approval_id = q.request("exec", dict(EXEC_PARAMS))
        assert await q.wait(approval_id, timeout=0.2) is False
    finally:
        q.close()

    resolved = [p for name, p in events if name == APPROVAL_RESOLVED]
    assert resolved == [
        {
            "id": approval_id,
            "namespace": "exec",
            "decision": "expired",
            "resolved_by": "timeout",
            "resolved_at": resolved[0]["resolved_at"],
        }
    ]


# ── delivery ─────────────────────────────────────────────────────────────


class _Sent:
    def __init__(self) -> None:
        self.frames: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, event_name: str, payload: dict[str, Any]) -> None:
        self.frames.append((event_name, payload))


async def test_broadcaster_pushes_requested_then_resolved(queue: ApprovalQueue) -> None:
    sent = _Sent()
    broadcaster = ApprovalBroadcaster(asyncio.get_running_loop(), sent).install()
    try:
        approval_id = queue.request("exec", dict(EXEC_PARAMS))
        await broadcaster.drain()
        queue.resolve(approval_id, True)
        await broadcaster.drain()
    finally:
        broadcaster.uninstall()

    assert [(name, p["id"]) for name, p in sent.frames] == [
        (APPROVAL_REQUESTED, approval_id),
        (APPROVAL_RESOLVED, approval_id),
    ]


async def test_a_policy_decision_never_flashes_a_prompt(queue: ApprovalQueue) -> None:
    """auto-approve resolves in the same call that queued: nothing is announced."""
    sent = _Sent()
    broadcaster = ApprovalBroadcaster(asyncio.get_running_loop(), sent).install()
    try:
        approval_id = queue.request("exec", dict(EXEC_PARAMS))
        queue.resolve(approval_id, True, resolved_by="policy")
        await broadcaster.drain()
    finally:
        broadcaster.uninstall()

    assert sent.frames == []


async def test_a_write_from_a_worker_thread_reaches_the_loop(queue: ApprovalQueue) -> None:
    sent = _Sent()
    broadcaster = ApprovalBroadcaster(asyncio.get_running_loop(), sent).install()
    try:
        worker = threading.Thread(target=lambda: queue.request("exec", dict(EXEC_PARAMS)))
        worker.start()
        await asyncio.to_thread(worker.join)
        for _ in range(20):
            await broadcaster.drain()
            if sent.frames:
                break
    finally:
        broadcaster.uninstall()

    assert [name for name, _ in sent.frames] == [APPROVAL_REQUESTED]


class _FakeConn:
    def __init__(self, conn_id: str, access: AccessContext) -> None:
        self.conn_id = conn_id
        self.access = access
        self.events: list[str] = []

    @property
    def authenticated(self) -> bool:
        return self.access.admitted

    async def send_event(self, event: str, payload: Any = None, meta: Any = None) -> None:
        self.events.append(event)


async def test_control_connections_receive_approval_frames_but_agent_shells_do_not() -> None:
    def access(surface: ConnectionSurface, *, admitted: bool = True, agent: Any = None):
        return AccessContext(
            surface=surface, admitted=admitted, credential_verified=True, agent=agent
        )

    registry = ConnectionRegistry()
    conns = {
        "operator": _FakeConn("operator", access(ConnectionSurface.CONTROL)),
        # The web console presents no operator secret: bound "unbound".
        "console": _FakeConn(
            "console", access(ConnectionSurface.CONTROL, agent=AgentBinding(via="unbound"))
        ),
        "agent": _FakeConn(
            "agent",
            access(
                ConnectionSurface.CONTROL,
                agent=AgentBinding(via="token", session_key="agent:main:main", agent_id="main"),
            ),
        ),
        "channel": _FakeConn("channel", access(ConnectionSurface.CHANNEL)),
        "node": _FakeConn("node", access(ConnectionSurface.NODE)),
        "anonymous": _FakeConn("anonymous", access(ConnectionSurface.CONTROL, admitted=False)),
    }
    for conn in conns.values():
        registry.register(conn)  # type: ignore[arg-type]

    await registry.broadcast_control(APPROVAL_REQUESTED, {"id": "a1"})

    assert {name: conn.events for name, conn in conns.items()} == {
        "operator": [APPROVAL_REQUESTED],
        "console": [APPROVAL_REQUESTED],
        "agent": [],
        "channel": [],
        "node": [],
        "anonymous": [],
    }


def test_hello_advertises_the_events_and_capabilities() -> None:
    class _Dispatcher:
        def list_methods(self) -> list[str]:
            return ["chat.send"]

    features = _build_features(_Dispatcher())  # type: ignore[arg-type]

    assert APPROVAL_REQUESTED in features.events
    assert APPROVAL_RESOLVED in features.events
    assert features.capabilities == sorted(CAPABILITIES)
    assert {"approval-events", "terminal-replay", "inflight-snapshot", "idempotency-keys"} <= set(
        features.capabilities
    )


def test_a_row_without_its_own_lifespan_still_reports_when_it_expires(
    queue: ApprovalQueue, events: list
) -> None:
    queue.request("exec", dict(EXEC_PARAMS))

    _, payload = events[0]
    # The fixture queue's default lifespan is 60 s.
    assert payload["expires_at"] == pytest.approx(payload["created_at"] + 60.0)


def test_stopping_a_run_withdraws_its_pending_prompts(queue: ApprovalQueue, events: list) -> None:
    mine = queue.request("exec", dict(EXEC_PARAMS))
    harness = queue.request("acp", {"sessionKey": "agent:main:webchat:abc", "displayTitle": "x"})
    other = queue.request("exec", {**EXEC_PARAMS, "sessionKey": "agent:main:webchat:other"})
    events.clear()

    cancelled = queue.cancel_pending_for_session("agent:main:webchat:abc")

    assert sorted(cancelled) == sorted([mine, harness])
    assert [(p["id"], p["decision"], p["resolved_by"]) for _, p in events] == [
        (approval_id, "cancelled", "abort") for approval_id in cancelled
    ]
    assert queue.get(mine).resolved and not queue.get(mine).approved
    assert queue.get(other).resolved is False


async def test_the_timer_sweep_announces_an_expiry_nobody_waits_for(
    tmp_path: Path, events: list
) -> None:
    q = ApprovalQueue(db_path=str(tmp_path / "sweep.sqlite"), default_timeout=0.05)
    try:
        approval_id = q.request("exec", dict(EXEC_PARAMS))
        sent = _Sent()
        broadcaster = ApprovalBroadcaster(asyncio.get_running_loop(), sent).install()
        broadcaster.start_sweeper(q.sweep, interval_s=0.02)
        try:
            for _ in range(50):
                await asyncio.sleep(0.02)
                if any(name == APPROVAL_RESOLVED for name, _ in sent.frames):
                    break
            await broadcaster.drain()
        finally:
            broadcaster.uninstall()
    finally:
        q.close()

    resolved = [p for name, p in sent.frames if name == APPROVAL_RESOLVED]
    assert resolved and resolved[0]["id"] == approval_id
    assert resolved[0]["decision"] == "expired"
