"""A gateway-shaped turn rig: real ``TaskRuntime`` + dispatch + ``EventBridge``.

Every frame lands in the production session replay buffer and on a recording
connection; only the model is scripted. Shared by the terminal, abort,
in-flight and idempotency tests.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from agentos.engine.types import DoneEvent, TextDeltaEvent
from agentos.gateway.boot import _make_task_session_lifecycle_listener, dispatch_task_runtime_turn
from agentos.gateway.config import GatewayConfig
from agentos.gateway.event_bridge import EventBridge
from agentos.gateway.routing import RouteEnvelope, SourceKind
from agentos.gateway.rpc import RpcContext
from agentos.gateway.task_runtime import TaskRuntime
from agentos.gateway.websocket import SubscriptionManager, get_registry
from agentos.session.manager import SessionManager
from agentos.session.storage import SessionStorage


class RecordingConn:
    def __init__(self, conn_id: str) -> None:
        self.conn_id = conn_id
        self.events: list[tuple[str, dict[str, Any], dict[str, Any] | None]] = []

    async def send_event(
        self, event: str, payload: dict | None = None, meta: dict | None = None
    ) -> None:
        self.events.append((event, dict(payload or {}), meta))

    def names(self) -> list[str]:
        return [name for name, _, _ in self.events]


class ScriptedRunner:
    """Streams ``chunks`` then blocks until released (or cancelled)."""

    def __init__(self, chunks: list[str], *, block: bool = True, finish: bool = True) -> None:
        self.chunks = chunks
        self.block = block
        self.finish = finish
        self.release = asyncio.Event()
        self.streamed = asyncio.Event()

    async def run(self, message: str, session_key: str, **kwargs: Any) -> AsyncIterator[Any]:
        for chunk in self.chunks:
            yield TextDeltaEvent(text=chunk)
        self.streamed.set()
        if self.block:
            await self.release.wait()
        if self.finish:
            yield DoneEvent(text="".join(self.chunks))


class TurnRig:
    def __init__(self, tmp: Path, runner: Any) -> None:
        self.key = f"agent:main:webchat:{uuid.uuid4().hex[:8]}"
        self.runner = runner
        self.config = GatewayConfig(
            workspace_dir=str(tmp / "ws"),
            state_dir=str(tmp / "state"),
            agent_stream_heartbeat_interval_seconds=0.0,
        )
        self.subs = SubscriptionManager()
        self.live = RecordingConn(f"live-{uuid.uuid4().hex[:6]}")
        self.sessions: SessionManager
        self.runtime: TaskRuntime

    def ctx(self, conn_id: str) -> RpcContext:
        return RpcContext(
            conn_id=conn_id,
            config=self.config,
            session_manager=self.sessions,
            subscription_manager=self.subs,
            task_runtime=self.runtime,
        )

    def envelope(self, key: str | None = None, **metadata: Any) -> RouteEnvelope:
        return RouteEnvelope(
            source_kind=SourceKind.WEB,
            source_name="test",
            agent_id="main",
            session_key=key or self.key,
            input_provenance={"kind": "web_message"},
            metadata=dict(metadata),
        )

    async def enqueue(self, key: str | None = None, **kwargs: Any) -> str:
        handle = await self.runtime.enqueue(self.envelope(key), "go", **kwargs)
        return handle.task_id


@asynccontextmanager
async def turn_rig(tmp_path: Path, runner: Any) -> AsyncIterator[TurnRig]:
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    rig = TurnRig(tmp_path, runner)
    store = SessionStorage(":memory:")
    await store.connect()
    rig.sessions = SessionManager(store, inject_time_prefix=False)
    await rig.sessions.create(rig.key, agent_id="main")
    registry = get_registry()
    registry.register(rig.live)  # type: ignore[arg-type]
    rig.subs.subscribe_messages(rig.live.conn_id, rig.key)
    bridge = EventBridge(subscription_manager=rig.subs, connection_registry=registry)

    async def _handler(run: Any) -> None:
        await dispatch_task_runtime_turn(
            run,
            config=rig.config,
            session_manager=rig.sessions,
            turn_runner=rig.runner,
            event_emitter=bridge.emit,
        )

    rig.runtime = TaskRuntime(
        storage=store,
        turn_handler=_handler,
        event_emitter=bridge.emit,
        lifecycle_listener=_make_task_session_lifecycle_listener(
            session_manager=rig.sessions, event_emitter=bridge.emit
        ),
        running_heartbeat_interval_s=None,
    )
    try:
        yield rig
    finally:
        registry.unregister(rig.live.conn_id)
        await rig.runtime.shutdown(timeout=1.0)
        await store.close()


async def wait_for(predicate: Any, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)
