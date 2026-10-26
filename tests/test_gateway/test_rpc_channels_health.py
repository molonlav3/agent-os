"""channels.status health fields and channels.start (#3690, #3691)."""

from __future__ import annotations

import time
from typing import Any

import pytest

import agentos.gateway.rpc_channels  # noqa: F401  ensures registration
from agentos.channels.types import ChannelHealth
from agentos.gateway.maintenance import GatewayMaintenance, set_maintenance
from agentos.gateway.rpc import RpcContext, get_dispatcher


class _Manager:
    def __init__(self, runtime: dict[str, dict[str, Any]]) -> None:
        self.runtime = runtime
        self.started: list[str] = []
        self._channel_types = {name: "telegram" for name in runtime}

    async def health(self) -> dict[str, ChannelHealth]:
        return {
            name: ChannelHealth(connected=status["state"] == "connected", extra={})
            for name, status in self.runtime.items()
        }

    async def runtime_status(self) -> dict[str, dict[str, Any]]:
        return self.runtime

    def get(self, name: str) -> Any:
        return object() if name in self.runtime else None

    def items(self) -> Any:
        return {name: object() for name in self.runtime}.items()

    async def start_stopped(self) -> dict[str, bool]:
        names = [n for n, s in self.runtime.items() if s["state"] == "stopped"]
        self.started.extend(names)
        return {name: True for name in names}

    async def start_channel(self, name: str) -> None:
        self.started.append(name)


@pytest.mark.asyncio
async def test_channels_status_surfaces_runtime_state_and_the_health_policy() -> None:
    now = time.time()
    manager = _Manager(
        {
            "tg": {
                "state": "connected",
                "transport": "socket",
                "started_at": now - 3 * 3600,
                "last_event_at": now - 2 * 3600,
                "terminal_reason": None,
            },
            "bad": {
                "state": "terminal",
                "transport": "socket",
                "started_at": now,
                "last_event_at": None,
                "terminal_reason": "auth: RuntimeError: 401",
            },
        }
    )
    maintenance = GatewayMaintenance(channel_manager_ref=lambda: manager)
    await maintenance.sweep()
    set_maintenance(maintenance)
    try:
        res = await get_dispatcher().dispatch(
            "r1", "channels.status", {}, RpcContext(conn_id="t", channel_manager=manager)
        )
    finally:
        set_maintenance(None)
    assert res.error is None, res.error
    rows = {row["name"]: row for row in res.payload["channels"]}
    assert rows["tg"]["state"] == "stale-socket"
    assert rows["tg"]["last_event_at"].endswith("Z")
    assert rows["bad"]["state"] == "terminal"
    assert rows["bad"]["terminal_reason"] == "auth: RuntimeError: 401"
    policy = res.payload["health_policy"]
    assert policy["active"] is True
    assert policy["policy"]["never_auto_restart"] == ["blocked", "terminal"]
    # The fake manager cannot restart: the sweep says so instead of failing.
    assert policy["last_sweep"]["restarted"] == []
    assert policy["last_sweep"]["skipped"] == {"tg": "unsupported"}
    assert policy["last_thaw"] is None


@pytest.mark.asyncio
async def test_channels_start_without_safe_mode_starts_only_stopped_channels() -> None:
    manager = _Manager(
        {
            "tg": {"state": "connected"},
            "off": {"state": "stopped"},
        }
    )
    ctx = RpcContext(conn_id="t", channel_manager=manager)
    res = await get_dispatcher().dispatch("s1", "channels.start", {}, ctx)
    assert res.error is None, res.error
    assert res.payload["channels"] == {"off": True}
    assert res.payload["safe_mode_cleared"] is False
    assert manager.started == ["off"]

    one = await get_dispatcher().dispatch("s2", "channels.start", {"channel": "tg"}, ctx)
    assert one.payload == {"status": "started", "channel": "tg", "safe_mode": {"active": False}}
    missing = await get_dispatcher().dispatch("s3", "channels.start", {"name": "nope"}, ctx)
    assert missing.error is not None
