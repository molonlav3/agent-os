"""Gateway maintenance tick: sleep/thaw detection and the channel health sweep.

Lid-close is the dominant failure mode of a laptop gateway: after wake every
channel socket is stale, and the per-channel retry loop backs off until it
gives up (``dead``). One 30 s tick in the gateway handles it:

* **Thaw.** The tick measures the gap since the previous tick on a clock
  that keeps counting while the machine sleeps. A gap over
  :attr:`ChannelHealthPolicy.thaw_gap_s` during which this process used
  little CPU (a frozen process, not a blocked event loop) is a thaw: log
  ``gateway.thaw``, restart every running channel that is idle, wake the
  scheduler, and broadcast a ``health`` event so clients refresh.
* **Health sweep.** Every :attr:`~ChannelHealthPolicy.sweep_interval_s`
  (after a start-up grace) each channel is classified; ``stale-socket`` (no
  inbound event for :attr:`~ChannelHealthPolicy.stale_after_s` on a
  socket/polling transport), ``disconnected``, ``stuck`` and ``start_failed``
  channels are restarted, at most once per
  :attr:`~ChannelHealthPolicy.restart_cooldown_sweeps` sweeps each.
  ``terminal`` (bad credentials) and ``blocked`` channels are never restarted
  automatically.
* **Failed restarts.** A restart whose start raises (the network is not back
  yet after a wake) is never left stopped: the channel manager retries it with
  backoff (``retrying``), and when it gives up the channel reads ``stuck``,
  which the next eligible sweep restarts again.
* **Safe mode.** The same tick re-evaluates the crash-loop breaker; while
  safe mode holds, no channel is touched.

The policy and the last sweep/thaw are surfaced in ``channels.status``.
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog

log = structlog.get_logger(__name__)

#: States the sweep restarts; everything else is left alone.
RESTARTABLE_STATES = frozenset({"stale-socket", "disconnected", "stuck", "start_failed"})
#: States that are never restarted automatically, only by an operator.
NEVER_AUTO_RESTART = frozenset({"terminal", "blocked"})
#: States a thaw leaves alone besides the terminal ones: stopped by the
#: operator, or already in the manager's own start-retry loop.
_THAW_SKIP_STATES = NEVER_AUTO_RESTART | {"stopped", "retrying"}
#: Transports that are push-only: a quiet webhook is not a stale socket.
_PASSIVE_TRANSPORTS = frozenset({"webhook"})


def suspend_aware_monotonic() -> float:
    """A monotonic clock that keeps counting while the machine sleeps.

    ``time.monotonic()`` (and so asyncio's timers) stops during system
    sleep on macOS (``mach_absolute_time``) and Linux (``CLOCK_MONOTONIC``),
    which is exactly what makes a thaw invisible to it. macOS's
    ``CLOCK_MONOTONIC`` and Linux's ``CLOCK_BOOTTIME`` include sleep.
    """
    clock_id: int | None = None
    if sys.platform == "darwin":
        clock_id = getattr(time, "CLOCK_MONOTONIC", None)
    elif sys.platform.startswith("linux"):
        clock_id = getattr(time, "CLOCK_BOOTTIME", None)
    if clock_id is not None:
        try:
            return time.clock_gettime(clock_id)
        except OSError:
            pass
    return time.time()


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class ChannelHealthPolicy:
    tick_s: float = 30.0
    thaw_gap_s: float = 45.0
    #: Process CPU seconds per wall second over the gap below which the gap
    #: reads as a frozen process (sleep, SIGSTOP) rather than a busy loop.
    thaw_cpu_ratio: float = 0.25
    sweep_interval_s: float = 300.0
    startup_grace_s: float = 60.0
    stale_after_s: float = 1800.0
    restart_cooldown_sweeps: int = 2

    def to_payload(self) -> dict[str, Any]:
        return {
            "tick_s": self.tick_s,
            "thaw_gap_s": self.thaw_gap_s,
            "thaw_cpu_ratio": self.thaw_cpu_ratio,
            "sweep_interval_s": self.sweep_interval_s,
            "startup_grace_s": self.startup_grace_s,
            "stale_after_s": self.stale_after_s,
            "restart_cooldown_sweeps": self.restart_cooldown_sweeps,
            "restart_states": sorted(RESTARTABLE_STATES),
            "never_auto_restart": sorted(NEVER_AUTO_RESTART),
        }


@dataclass
class MaintenanceClock:
    """Everything the tick reads from the outside world; replaced in tests."""

    monotonic: Callable[[], float] = suspend_aware_monotonic
    process_time: Callable[[], float] = time.process_time
    wall: Callable[[], float] = time.time
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep


@dataclass
class SweepReport:
    at: float
    index: int
    states: dict[str, str] = field(default_factory=dict)
    restarted: list[str] = field(default_factory=list)
    #: Restarts that raised; the manager's retry loop owns them now.
    failed: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "at": _iso(self.at),
            "index": self.index,
            "states": dict(self.states),
            "restarted": list(self.restarted),
            "failed": list(self.failed),
            "skipped": dict(self.skipped),
        }


@dataclass
class ThawReport:
    at: float
    gap_s: float
    cpu_ratio: float
    restarted: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "at": _iso(self.at),
            "gap_s": round(self.gap_s, 1),
            "cpu_ratio": round(self.cpu_ratio, 4),
            "restarted": list(self.restarted),
            "failed": list(self.failed),
            "skipped": dict(self.skipped),
        }


def _outcome(raw: Any) -> str:
    """Normalise a ``restart_if_idle`` result (an outcome string, or a legacy bool)."""
    if raw is True:
        return "restarted"
    if raw is False or raw is None:
        return "busy"
    return str(raw)


def classify(status: dict[str, Any], *, now: float, policy: ChannelHealthPolicy) -> str:
    """The sweep's view of one channel: the manager's state, plus ``stale-socket``."""
    state = str(status.get("state") or "stopped")
    if state != "connected":
        return state
    if status.get("transport") in _PASSIVE_TRANSPORTS:
        return state
    last_activity = max(
        float(status.get("last_event_at") or 0.0),
        float(status.get("started_at") or 0.0),
    )
    if last_activity and now - last_activity > policy.stale_after_s:
        return "stale-socket"
    return state


class GatewayMaintenance:
    """One background task per gateway; see the module docstring."""

    def __init__(
        self,
        *,
        channel_manager_ref: Callable[[], Any],
        scheduler_ref: Callable[[], Any] = lambda: None,
        safe_mode_active: Callable[[], bool] = lambda: False,
        on_tick: list[Callable[[], Awaitable[Any]]] | None = None,
        broadcast: Callable[[str, Any], Awaitable[Any]] | None = None,
        policy: ChannelHealthPolicy | None = None,
        clock: MaintenanceClock | None = None,
    ) -> None:
        self._channel_manager_ref = channel_manager_ref
        self._scheduler_ref = scheduler_ref
        self._safe_mode_active = safe_mode_active
        self._on_tick = list(on_tick or [])
        self._broadcast = broadcast
        self.policy = policy or ChannelHealthPolicy()
        self._clock = clock or MaintenanceClock()
        self._started_mono = self._clock.monotonic()
        self._last_mono = self._started_mono
        self._last_cpu = self._clock.process_time()
        self._last_sweep_mono: float | None = None
        self._sweep_index = 0
        self._last_restart_sweep: dict[str, int] = {}
        self.last_sweep: SweepReport | None = None
        self.last_thaw: ThawReport | None = None
        self._task: asyncio.Task[None] | None = None

    # ── lifecycle ───────────────────────────────────────────────────

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="gateway:maintenance")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while True:
            await self._clock.sleep(self.policy.tick_s)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad tick must not end maintenance
                log.exception("gateway.maintenance_tick_failed")

    # ── the tick ────────────────────────────────────────────────────

    async def tick(self) -> None:
        now_mono = self._clock.monotonic()
        now_cpu = self._clock.process_time()
        gap = now_mono - self._last_mono
        cpu_used = max(0.0, now_cpu - self._last_cpu)
        self._last_mono, self._last_cpu = now_mono, now_cpu

        if gap > self.policy.thaw_gap_s:
            ratio = cpu_used / gap if gap > 0 else 0.0
            if ratio < self.policy.thaw_cpu_ratio:
                await self._on_thaw(gap, ratio)
            else:
                log.warning(
                    "gateway.event_loop_stalled",
                    gap_s=round(gap, 1),
                    cpu_ratio=round(ratio, 3),
                )

        for hook in self._on_tick:
            try:
                await hook()
            except Exception:  # noqa: BLE001
                log.exception("gateway.maintenance_hook_failed")

        if now_mono - self._started_mono < self.policy.startup_grace_s:
            return
        if (
            self._last_sweep_mono is None
            or now_mono - self._last_sweep_mono >= self.policy.sweep_interval_s
        ):
            self._last_sweep_mono = now_mono
            await self.sweep()

    async def _on_thaw(self, gap: float, ratio: float) -> None:
        report = ThawReport(at=self._clock.wall(), gap_s=gap, cpu_ratio=ratio)
        self.last_thaw = report
        log.warning("gateway.thaw", gap_s=round(gap, 1), cpu_ratio=round(ratio, 4))
        manager = self._channel_manager_ref()
        restart = getattr(manager, "restart_if_idle", None)
        if callable(restart) and not self._safe_mode_active():
            statuses = await _runtime_status(manager)
            for name, status in statuses.items():
                if status.get("state") in _THAW_SKIP_STATES:
                    continue
                outcome = _outcome(await restart(name))
                if outcome == "restarted":
                    report.restarted.append(name)
                elif outcome == "failed":
                    report.failed.append(name)
                else:
                    report.skipped[name] = outcome
                    continue
                self._last_restart_sweep[name] = self._sweep_index
        scheduler = self._scheduler_ref()
        nudge = getattr(scheduler, "nudge", None)
        if callable(nudge):
            try:
                nudge()
            except Exception:  # noqa: BLE001
                log.debug("gateway.thaw_scheduler_nudge_failed", exc_info=True)
        if self._broadcast is not None:
            try:
                await self._broadcast("health", {"reason": "thaw", "gap_ms": int(gap * 1000)})
            except Exception:  # noqa: BLE001
                log.debug("gateway.thaw_broadcast_failed", exc_info=True)

    async def sweep(self) -> SweepReport:
        self._sweep_index += 1
        report = SweepReport(at=self._clock.wall(), index=self._sweep_index)
        self.last_sweep = report
        manager = self._channel_manager_ref()
        if manager is None:
            return report
        statuses = await _runtime_status(manager)
        now = self._clock.wall()
        safe_mode = self._safe_mode_active()
        restart = getattr(manager, "restart_if_idle", None)
        for name, status in statuses.items():
            state = classify(status, now=now, policy=self.policy)
            report.states[name] = state
            if state not in RESTARTABLE_STATES:
                continue
            if safe_mode:
                report.skipped[name] = "safe_mode"
                continue
            last = self._last_restart_sweep.get(name)
            if last is not None and self._sweep_index - last < self.policy.restart_cooldown_sweeps:
                report.skipped[name] = "cooldown"
                continue
            if not callable(restart):
                report.skipped[name] = "unsupported"
                continue
            outcome = _outcome(await restart(name))
            if outcome == "restarted":
                report.restarted.append(name)
                self._last_restart_sweep[name] = self._sweep_index
                log.warning("gateway.channel_auto_restarted", channel=name, state=state)
            elif outcome == "failed":
                # Counted as an attempt: the cooldown applies, and the
                # manager keeps retrying in the meantime.
                report.failed.append(name)
                self._last_restart_sweep[name] = self._sweep_index
                log.warning("gateway.channel_auto_restart_failed", channel=name, state=state)
            else:
                report.skipped[name] = outcome
        return report

    # ── surface ─────────────────────────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        return {
            "policy": self.policy.to_payload(),
            "last_sweep": self.last_sweep.to_payload() if self.last_sweep else None,
            "last_thaw": self.last_thaw.to_payload() if self.last_thaw else None,
        }


async def _runtime_status(manager: Any) -> dict[str, dict[str, Any]]:
    probe = getattr(manager, "runtime_status", None)
    if not callable(probe):
        return {}
    try:
        result = await probe()
    except Exception:  # noqa: BLE001
        log.exception("gateway.channel_runtime_status_failed")
        return {}
    return result if isinstance(result, dict) else {}


# ── process holder, for channels.status ──────────────────────────────

_current: GatewayMaintenance | None = None


def set_maintenance(maintenance: GatewayMaintenance | None) -> None:
    global _current
    _current = maintenance


def get_maintenance() -> GatewayMaintenance | None:
    return _current


__all__ = [
    "NEVER_AUTO_RESTART",
    "RESTARTABLE_STATES",
    "ChannelHealthPolicy",
    "GatewayMaintenance",
    "MaintenanceClock",
    "SweepReport",
    "ThawReport",
    "classify",
    "get_maintenance",
    "set_maintenance",
    "suspend_aware_monotonic",
]
