"""Boot outcomes and the crash-loop breaker (safe mode).

Keep-alive without a breaker turns one bad channel config into a restart
storm: launchd or systemd respawns the gateway, the adapter crashes it again,
forever. So every boot is recorded in the gateway database (``gateway_boots``
in ``sessions.db``), and a boot that finds :data:`UNCLEAN_BOOT_THRESHOLD`
unclean boots inside the last :data:`UNCLEAN_WINDOW_S` seconds starts in
**safe mode**:

* the control plane (WebSocket, HTTP, doctor, logs, config) starts as usual;
* channel autostart, the scheduler and the heartbeat loop are held back --
  no automatic turn runs until safe mode ends;
* ``/ready`` reports ``state: "safe_mode"`` and doctor reports a finding with
  the way out;
* ``channels.start`` (``agentos channels start``) is the manual override:
  without a channel name it leaves safe mode and acknowledges the recorded
  crashes; with one it starts just that channel, to find the bad one;
* safe mode clears by itself once the window drains (re-evaluated on the
  maintenance tick), and the held-back producers start then.

A boot is *clean* when the process shut down through ``GatewayServer.close``
or uvicorn's graceful shutdown (``agentos gateway stop``, a supervisor's
SIGTERM, an in-place restart). One that never recorded an end -- killed,
crashed, OOM -- is marked unclean by the next boot. A boot that raised
before it served (a bad config, a failed migration) is unclean too but not
counted: nothing safe mode holds back had started, so it could not help,
and counting it would hold back the channels of the repaired boot. The
ledger fails open: an unreadable table means "no safe mode", never a
refused start.
"""

from __future__ import annotations

import os
import sqlite3
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

UNCLEAN_BOOT_THRESHOLD = 3
UNCLEAN_WINDOW_S = 300.0
#: Rows kept in ``gateway_boots``; older ones are pruned at boot.
KEEP_BOOT_ROWS = 200
REASON_NO_SHUTDOWN = "ended without a clean shutdown"

TABLE = "gateway_boots"
# Kept in step with migrations/V014__gateway_boots.py. The ledger creates the
# table itself as well, because a boot is recorded before migrations run: a
# crash inside the migration step must still count.
CREATE_TABLE = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    id TEXT PRIMARY KEY,
    pid INTEGER NOT NULL,
    started_at REAL NOT NULL,
    ended_at REAL,
    clean INTEGER,
    reason TEXT NOT NULL DEFAULT '',
    counted INTEGER NOT NULL DEFAULT 1,
    last_seen_at REAL
)
"""
CREATE_INDEX = f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_started_at ON {TABLE}(started_at)"
#: When an unclean boot ended: its last heartbeat, else its start (it died
#: before the first maintenance tick). The breaker window counts by this, so
#: a gateway that ran for an hour and then crashed counts as crashing now.
_END_TIME = "COALESCE(ended_at, last_seen_at, started_at)"


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class BootRow:
    id: str
    pid: int
    started_at: float
    ended_at: float | None
    clean: bool | None
    reason: str
    #: Whether the row counts toward the breaker. An operator override
    #: (``channels.start``) and a boot that failed before serving (which safe
    #: mode cannot help: nothing it holds back had started) do not.
    counted: bool = True

    @property
    def end_time(self) -> float:
        return self.ended_at if self.ended_at is not None else self.started_at


class BootLedger:
    """``gateway_boots`` over plain ``sqlite3``: a few tiny writes per boot.

    Each call opens its own short-lived connection, so the ledger never holds
    the session database open and works before the session storage exists.
    Every public method raises ``sqlite3.Error``/``OSError`` to the caller;
    :class:`BootTracker` is the fail-open layer.
    """

    def __init__(self, db_path: str | Path, *, timeout_s: float = 2.0) -> None:
        self._db_path = str(db_path)
        self._timeout_s = timeout_s

    @property
    def db_path(self) -> str:
        return self._db_path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """One transaction on a fresh connection, closed on the way out."""
        Path(self._db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path, timeout=self._timeout_s)
        try:
            with conn:
                conn.execute(CREATE_TABLE)
                conn.execute(CREATE_INDEX)
                columns = {row[1] for row in conn.execute(f"PRAGMA table_info({TABLE})")}
                if "last_seen_at" not in columns:
                    conn.execute(f"ALTER TABLE {TABLE} ADD COLUMN last_seen_at REAL")
                yield conn
        finally:
            conn.close()

    def begin(self, *, boot_id: str, pid: int, now: float) -> int:
        """Close out boots that never ended, record this one; returns rows closed."""
        with self._connect() as conn:
            # A boot that never recorded its end died around its last heartbeat.
            closed = conn.execute(
                f"UPDATE {TABLE} SET clean = 0, ended_at = {_END_TIME}, reason = ? "
                "WHERE clean IS NULL AND id != ?",
                (REASON_NO_SHUTDOWN, boot_id),
            ).rowcount
            conn.execute(
                f"INSERT INTO {TABLE} (id, pid, started_at) VALUES (?, ?, ?)",
                (boot_id, pid, now),
            )
            conn.execute(
                f"DELETE FROM {TABLE} WHERE id NOT IN "
                f"(SELECT id FROM {TABLE} ORDER BY started_at DESC LIMIT ?)",
                (KEEP_BOOT_ROWS,),
            )
        return int(closed or 0)

    def end(
        self, *, boot_id: str, clean: bool, reason: str, now: float, counted: bool = True
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                f"UPDATE {TABLE} SET clean = ?, ended_at = ?, reason = ?, counted = ? "
                "WHERE id = ? AND clean IS NULL",
                (1 if clean else 0, now, reason, 1 if counted else 0, boot_id),
            )

    def touch(self, *, boot_id: str, now: float) -> None:
        """Heartbeat: this boot was alive at ``now``."""
        with self._connect() as conn:
            conn.execute(
                f"UPDATE {TABLE} SET last_seen_at = ? WHERE id = ? AND clean IS NULL",
                (now, boot_id),
            )

    def unclean_since(self, since: float, *, exclude: str | None = None) -> list[BootRow]:
        """Counted unclean boots that *ended* at or after ``since``."""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT id, pid, started_at, {_END_TIME}, clean, reason, counted FROM {TABLE} "
                f"WHERE clean = 0 AND counted = 1 AND {_END_TIME} >= ? AND id != ? "
                f"ORDER BY {_END_TIME} DESC",
                (since, exclude or ""),
            ).fetchall()
        return [
            BootRow(
                id=str(r[0]),
                pid=int(r[1]),
                started_at=float(r[2]),
                ended_at=None if r[3] is None else float(r[3]),
                clean=None if r[4] is None else bool(r[4]),
                reason=str(r[5] or ""),
                counted=bool(r[6]),
            )
            for r in rows
        ]

    def acknowledge_unclean(self) -> int:
        """Stop counting every recorded unclean boot (the operator override)."""
        with self._connect() as conn:
            cur = conn.execute(f"UPDATE {TABLE} SET counted = 0 WHERE clean = 0 AND counted = 1")
        return int(cur.rowcount or 0)


@dataclass
class SafeModeDecision:
    active: bool
    reason: str = ""
    unclean_boots: int = 0
    threshold: int = UNCLEAN_BOOT_THRESHOLD
    window_s: float = UNCLEAN_WINDOW_S
    since: float | None = None
    clears_at: float | None = None
    recent: list[BootRow] = field(default_factory=list)
    error: str | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "active": self.active,
            "unclean_boots": self.unclean_boots,
            "threshold": self.threshold,
            "window_s": int(self.window_s),
        }
        if self.active:
            payload.update(
                {
                    "reason": self.reason,
                    "since": _iso(self.since),
                    "clears_at": _iso(self.clears_at),
                    "recent": [
                        {
                            "started_at": _iso(row.started_at),
                            "ended_at": _iso(row.end_time),
                            "reason": row.reason,
                        }
                        for row in self.recent
                    ],
                }
            )
        if self.error:
            payload["error"] = self.error
        return payload


def evaluate_unclean_boots(
    recent: list[BootRow],
    *,
    now: float,
    threshold: int = UNCLEAN_BOOT_THRESHOLD,
    window_s: float = UNCLEAN_WINDOW_S,
) -> SafeModeDecision:
    """Safe mode iff ``threshold`` unclean boots *ended* inside the window.

    Counting by end time (the last heartbeat) rather than start time means a
    gateway that ran for a while before each crash still trips the breaker.
    ``clears_at`` is when the ``threshold``-th newest of them leaves the
    window, the moment the count drops below the threshold.
    """
    in_window = sorted(
        (row for row in recent if row.end_time >= now - window_s and row.counted),
        key=lambda row: row.end_time,
        reverse=True,
    )
    count = len(in_window)
    if count < threshold:
        return SafeModeDecision(
            active=False,
            unclean_boots=count,
            threshold=threshold,
            window_s=window_s,
        )
    pivot = in_window[threshold - 1]
    return SafeModeDecision(
        active=True,
        reason=(
            f"{count} unclean gateway boots in the last {int(window_s)}s (threshold {threshold})"
        ),
        unclean_boots=count,
        threshold=threshold,
        window_s=window_s,
        since=now,
        clears_at=pivot.end_time + window_s,
        recent=in_window,
    )


ResumeHook = Callable[[str], Awaitable[None]]


class BootTracker:
    """This process's boot record and the safe-mode state it implies."""

    def __init__(
        self,
        ledger: BootLedger | None,
        *,
        clock: Callable[[], float] = time.time,
        threshold: int = UNCLEAN_BOOT_THRESHOLD,
        window_s: float = UNCLEAN_WINDOW_S,
        pid: int | None = None,
    ) -> None:
        self._ledger = ledger
        self._clock = clock
        self._threshold = threshold
        self._window_s = window_s
        self._pid = os.getpid() if pid is None else pid
        self.boot_id = uuid.uuid4().hex
        self._recorded = False
        self._ended = False
        self._stopping_reason: str | None = None
        self._decision = SafeModeDecision(active=False, threshold=threshold, window_s=window_s)
        self._ledger_error: str | None = None
        self._resume_hook: ResumeHook | None = None
        self._cleared_at: float | None = None
        self._cleared_by: str | None = None

    @classmethod
    def for_state_dir(cls, state_dir: str | Path, **kwargs: Any) -> BootTracker:
        return cls(BootLedger(Path(state_dir) / "sessions.db"), **kwargs)

    # ── boot ────────────────────────────────────────────────────────

    def start(self) -> SafeModeDecision:
        """Record this boot and decide safe mode. Never raises."""
        now = self._clock()
        if self._ledger is None:
            return self._decision
        try:
            closed = self._ledger.begin(boot_id=self.boot_id, pid=self._pid, now=now)
            self._recorded = True
            if closed:
                log.warning("gateway.unclean_boots_recorded", count=closed)
            recent = self._ledger.unclean_since(now - self._window_s, exclude=self.boot_id)
        except (sqlite3.Error, OSError) as exc:
            self._ledger_error = f"{type(exc).__name__}: {exc}"
            log.warning(
                "gateway.boot_ledger_unavailable",
                error=self._ledger_error,
                hint="crash-loop breaker disabled for this boot (fail open)",
            )
            self._decision = SafeModeDecision(
                active=False,
                threshold=self._threshold,
                window_s=self._window_s,
                error=self._ledger_error,
            )
            return self._decision
        self._decision = evaluate_unclean_boots(
            recent, now=now, threshold=self._threshold, window_s=self._window_s
        )
        if self._decision.active:
            log.warning(
                "gateway.safe_mode",
                reason=self._decision.reason,
                clears_at=_iso(self._decision.clears_at),
                hint="channels, cron and heartbeat are held back; "
                "run 'agentos channels start --yes' to override",
            )
        return self._decision

    # ── safe mode ───────────────────────────────────────────────────

    @property
    def safe_mode_active(self) -> bool:
        return self._decision.active

    @property
    def decision(self) -> SafeModeDecision:
        return self._decision

    def set_resume_hook(self, hook: ResumeHook | None) -> None:
        """``hook(reason)`` starts what safe mode held back; awaited once on exit."""
        self._resume_hook = hook

    async def reevaluate(self) -> bool:
        """Re-read the window; returns ``True`` when safe mode just cleared."""
        if not self._decision.active:
            return False
        now = self._clock()
        if self._ledger is None:
            return False
        try:
            recent = self._ledger.unclean_since(now - self._window_s, exclude=self.boot_id)
        except (sqlite3.Error, OSError) as exc:
            log.warning("gateway.boot_ledger_unavailable", error=f"{type(exc).__name__}: {exc}")
            recent = []
        decision = evaluate_unclean_boots(
            recent, now=now, threshold=self._threshold, window_s=self._window_s
        )
        if decision.active:
            # Same episode: keep when it first tripped, refresh the rest.
            decision.since = self._decision.since or decision.since
            self._decision = decision
            return False
        await self._clear("window_drained")
        return True

    async def heartbeat(self) -> None:
        """Record that this boot is alive (maintenance tick). Never raises."""
        if self._ledger is None or not self._recorded or self._ended:
            return
        try:
            self._ledger.touch(boot_id=self.boot_id, now=self._clock())
        except (sqlite3.Error, OSError) as exc:
            log.debug("gateway.boot_ledger_unavailable", error=f"{type(exc).__name__}: {exc}")

    async def exit_safe_mode(self, *, reason: str = "operator") -> bool:
        """Leave safe mode now and stop counting the crashes that caused it."""
        if not self._decision.active:
            return False
        if self._ledger is not None:
            try:
                self._ledger.acknowledge_unclean()
            except (sqlite3.Error, OSError) as exc:
                log.warning("gateway.boot_ledger_unavailable", error=f"{type(exc).__name__}: {exc}")
        await self._clear(reason)
        return True

    async def _clear(self, reason: str) -> None:
        self._decision = SafeModeDecision(
            active=False, threshold=self._threshold, window_s=self._window_s
        )
        self._cleared_at = self._clock()
        self._cleared_by = reason
        log.info("gateway.safe_mode_cleared", reason=reason)
        hook = self._resume_hook
        if hook is not None:
            try:
                await hook(reason)
            except Exception:  # noqa: BLE001 - a failed resume is logged, not raised
                log.exception("gateway.safe_mode_resume_failed", reason=reason)

    def snapshot(self) -> dict[str, Any]:
        payload = self._decision.to_payload()
        if self._ledger_error and "error" not in payload:
            payload["error"] = self._ledger_error
        if self._cleared_at is not None and not self._decision.active:
            payload["cleared_at"] = _iso(self._cleared_at)
            payload["cleared_by"] = self._cleared_by
        return payload

    # ── end of boot ─────────────────────────────────────────────────

    def note_stopping(self, reason: str) -> None:
        """Remember why the process is going down, for :meth:`mark_clean`."""
        if self._stopping_reason is None:
            self._stopping_reason = reason

    def mark_clean(self, reason: str | None = None) -> None:
        """Record a clean end. Idempotent: the first call's reason wins."""
        self._end(clean=True, reason=reason or self._stopping_reason or "server_shutdown")

    def mark_startup_failed(self, reason: str) -> None:
        """Boot raised before serving: unclean, but not counted toward safe mode.

        Nothing safe mode holds back (channels, cron, heartbeat) had started,
        so entering it could not stop the loop -- it would only hold the
        channels back on the next, repaired boot.
        """
        self._end(clean=False, reason=f"startup_failed: {reason}"[:500], counted=False)

    def _end(self, *, clean: bool, reason: str, counted: bool = True) -> None:
        if self._ended:
            return
        self._ended = True
        if self._ledger is None or not self._recorded:
            return
        try:
            self._ledger.end(
                boot_id=self.boot_id,
                clean=clean,
                reason=reason,
                now=self._clock(),
                counted=counted,
            )
        except (sqlite3.Error, OSError) as exc:
            log.warning("gateway.boot_ledger_unavailable", error=f"{type(exc).__name__}: {exc}")


# ── process holder: the channels.start RPC and doctor reach it here ──

_tracker: BootTracker | None = None


def set_boot_tracker(tracker: BootTracker | None) -> None:
    global _tracker
    _tracker = tracker


def get_boot_tracker() -> BootTracker | None:
    return _tracker


__all__ = [
    "KEEP_BOOT_ROWS",
    "UNCLEAN_BOOT_THRESHOLD",
    "UNCLEAN_WINDOW_S",
    "BootLedger",
    "BootRow",
    "BootTracker",
    "SafeModeDecision",
    "evaluate_unclean_boots",
    "get_boot_tracker",
    "set_boot_tracker",
]
