"""Aborting a parent session stops the coding-harness children it spawned.

``sessions.abort`` used to stop only the parent's own run: a Claude Code child
kept working, and its card kept reading "Working" after the parent was done.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from acp_runtime_helpers import wait_until
from acp_s_fakes import FakeRuntime, Script
from acp_s_harness import PARENT_KEY, acp_rig

from agentos.acp.types import AcpEvent
from agentos.gateway.rpc import get_dispatcher
from agentos.gateway.rpc.registry import RpcContext
from agentos.tools.builtin import sessions as sessions_tool


def _blocked_script() -> Script:
    return Script(
        events=[
            AcpEvent(kind="tool_call", tool_call_id="t1", tool_kind="read", title="Read a"),
            AcpEvent(kind="tool_call_update", tool_call_id="t1", status="completed"),
            AcpEvent(kind="text_delta", text="halfway"),
        ],
        block=True,
    )


async def test_parent_abort_stops_the_acp_child_and_does_not_wake_the_parent(tmp_path) -> None:
    runtime = FakeRuntime([_blocked_script()])
    async with acp_rig(tmp_path, runtime) as rig:
        woken: list[str] = []

        async def _capture(session_key, message, provenance=None, stream_event_sink=None):
            if session_key == PARENT_KEY:
                woken.append(message)
            return SimpleNamespace(task_id="wake")

        rig.task_runtime.send = _capture  # type: ignore[method-assign]
        out = json.loads(
            await sessions_tool.sessions_spawn(runtime="acp", task="long", acp_agent="claude")
        )
        child = out["session_key"]
        await asyncio.wait_for(runtime.turn_started.wait(), timeout=5)
        await asyncio.sleep(0.02)

        ctx = RpcContext(
            conn_id="desk",
            config=rig.config,
            session_manager=rig.sessions,
            task_runtime=rig.task_runtime,
        )
        res = await get_dispatcher().dispatch("1", "sessions.abort", {"key": PARENT_KEY}, ctx)

        assert res.ok, res.error
        assert res.payload["children_aborted"] == [child]
        assert runtime.turn_cancels == 1
        record = await rig.task_runtime.status(out["task_id"])
        assert str(getattr(record.status, "value", record.status)) == "cancelled"

        # The child's own stream ends with exactly one "stopped" frame...
        terminals = [
            (name, payload)
            for key, name, payload in rig.events
            if key == child and name in ("session.event.done", "session.event.error")
        ]
        assert len(terminals) == 1
        assert terminals[0][0] == "session.event.done"
        assert terminals[0][1]["state"] == "aborted"
        # ...and its run state reaches clients, scoped to the child.
        changed = [p for key, n, p in rig.events if key == child and n == "sessions.changed"]
        assert changed and changed[-1]["run_status"] == "cancelled"

        # The completion is recorded on the parent, which is not restarted.
        assert await wait_until(
            lambda: any(n == "session.event.subagent_completion" for _, n, _ in rig.events)
        )
        await asyncio.sleep(0.05)
        assert woken == []
