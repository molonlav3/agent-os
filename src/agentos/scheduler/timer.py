"""Scheduler tick loop — burst protection, startup catchup, sleep-aware timing, nudge.

Timing model
------------
Due-ness is always decided on the wall clock: a job's ``next_run_at`` is a
wall-clock instant ("every day at 09:00"). How long the loop sleeps until it
next looks is armed against *both* clocks: from the arming instant the loop
waits until either the wall clock or the monotonic clock says the planned
delay has passed, whichever comes first, in slices of at most
:attr:`SchedulerTimer.MAX_TIMER_DELAY`.

* Machine sleep: the monotonic clock stops while the host is suspended, the
  wall clock does not. On the first wake after a thaw the wall clock is
  already past the target, so the loop ticks at once; every job that came due
  while the lid was closed runs once (missed ticks coalesce into one run, and
  the next slot is computed after "now").
* Backward clock correction: the wall target moves away, the monotonic one
  does not, so the loop wakes on time, finds nothing due by the wall clock and
  re-arms. Nothing fires early and nothing fires twice: a run's next slot is
  computed after the later of "now" and the moment the run took the job
  (``jobs._schedule_base``).

Each wake compares wall and monotonic elapsed time and logs a forward gap
(sleep/thaw or a clock set forward), a backward jump, and a late wake (woke
after more than twice the planned delay).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

from .jobs import HandlerFn, _next_run, apply_reserved_result, execute_with_timeout
from .persistence import JobStore
from .stagger import spread_jobs
from .types import (
    ConcurrencyPolicy,
    CronJob,
    JobExecution,
    JobReservation,
    JobStatus,
    ScheduleKind,
    clear_reservation,
)

logger = logging.getLogger(__name__)

#: ``listener(job, execution)``: see :meth:`SchedulerTimer.add_run_listener`.
RunListener = Callable[[CronJob | None, JobExecution], Awaitable[Any]]

MIN_REFIRE_GAP_SECONDS = 2.0

#: Handler keys whose run drives a model turn. ``agent_run`` runs an agent
#: turn; ``system_event`` either runs a heartbeat turn now or requests the next
#: heartbeat, which is a model turn too. ``static_message`` (reminders) and
#: ``script_run`` never call a model, so they are caught up at boot at once.
AGENT_HANDLER_KEYS: frozenset[str] = frozenset({"agent_run", "system_event"})

#: Wall and monotonic elapsed time may disagree by this much before a wake is
#: reported as a clock jump (timer resolution, NTP slew).
CLOCK_JUMP_TOLERANCE_SECONDS = 5.0
#: A wake is late when it took more than twice the planned delay *and* at
#: least this much longer than planned (a 0.1 s floor wait is not "late" at 0.3 s).
LATE_WAKE_FLOOR_SECONDS = 1.0
#: Remaining time below this is treated as due (asyncio may wake a hair early).
_DUE_EPSILON_SECONDS = 0.005

REPLACED_RUN_ERROR = "replaced by a newer run (concurrency: replace)"
REPLACED_RUN_STATUS = "replaced"


def _refire_gap_seconds(job: CronJob) -> float:
    """Effective refire-gap floor for *job*.

    MIN_REFIRE_GAP_SECONDS debounces a nudge landing right after an ordinary
    tick already ran the job -- it is not meant to cap how often a job's own
    schedule says it should fire. An EVERY job's configured interval is
    always the tighter bound when it undercuts the flat floor.
    """
    if job.schedule_kind == ScheduleKind.EVERY and job.cron_expr.isdigit():
        return min(MIN_REFIRE_GAP_SECONDS, float(job.cron_expr))
    return MIN_REFIRE_GAP_SECONDS


class SchedulerClock:
    """Wall time, monotonic time and the nudgeable wait the loop sleeps on.

    Injected so tests can drive sleep gaps and clock jumps deterministically.
    """

    def wall(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    async def wait(self, event: asyncio.Event, timeout: float) -> bool:
        """Wait up to *timeout* seconds; ``True`` when *event* was set (a nudge)."""
        if event.is_set():
            return True
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except TimeoutError:
            return False
        return True


@dataclass(frozen=True)
class ArmedTimer:
    """One planned wait: *delay* seconds from the arming instant, on both clocks."""

    armed_wall: datetime
    armed_mono: float
    delay: float

    def remaining(self, wall_now: datetime, mono_now: float) -> float:
        wall_remaining = self.delay - (wall_now - self.armed_wall).total_seconds()
        mono_remaining = self.delay - (mono_now - self.armed_mono)
        return min(wall_remaining, mono_remaining)


@dataclass(frozen=True)
class WakeReport:
    """What one wait looked like from both clocks."""

    expected_seconds: float
    wall_elapsed: float
    mono_elapsed: float
    nudged: bool

    @property
    def forward_gap(self) -> float:
        """Wall time that passed without monotonic time: sleep, thaw, clock set forward."""
        return max(0.0, self.wall_elapsed - self.mono_elapsed)

    @property
    def backward_jump(self) -> float:
        """How far the wall clock was set back during the wait."""
        return max(0.0, self.mono_elapsed - self.wall_elapsed)

    @property
    def late(self) -> bool:
        return (
            self.wall_elapsed > 2 * self.expected_seconds
            and self.wall_elapsed - self.expected_seconds > LATE_WAKE_FLOOR_SECONDS
        )


class SchedulerTimer:
    """The scheduler's tick loop — heart of execution orchestration.

    Handles burst protection (max_concurrent), startup catchup with stagger,
    sleep-aware sleep-to-next-due timing, per-job overlap policy and
    nudge-based wake.
    """

    MAX_TIMER_DELAY = 60.0
    CATCHUP_STAGGER_SECONDS = 10.0

    def __init__(
        self,
        store: JobStore,
        handlers: dict[str, HandlerFn],
        max_concurrent: int = 3,
        max_catchup: int = 5,
        reaper: object | None = None,
        *,
        clock: SchedulerClock | None = None,
        startup_agent_delay_seconds: float = 0.0,
    ) -> None:
        self._store = store
        self._handlers = handlers
        self._max_concurrent = max_concurrent
        self._max_catchup = max_catchup
        self._reaper = reaper
        self._clock = clock or SchedulerClock()
        self._startup_agent_delay = max(0.0, float(startup_agent_delay_seconds))
        self._running: dict[str, asyncio.Task] = {}
        self._catchup_tasks: set[asyncio.Task[None]] = set()
        # Earlier runs (task -> job id) still executing after a newer run took
        # the job over; tracked so stop() and cancel_running() reach them.
        self._superseded: dict[asyncio.Task[None], str] = {}
        # Every run this timer launched, by reservation token, so ``replace``
        # can cancel the exact run it supersedes.
        self._task_by_token: dict[str, asyncio.Task[None]] = {}
        self._replaced_tokens: set[str] = set()
        # job id -> the overlap slot a ``forbid`` skip was last logged for.
        self._forbid_logged: dict[str, datetime] = {}
        self._loop_task: asyncio.Task | None = None
        self._started = False
        self._nudge_event = asyncio.Event()
        self.last_wake: WakeReport | None = None
        self._run_listeners: list[RunListener] = []

    # ------------------------------------------------------------------
    # Handler registry
    # ------------------------------------------------------------------

    def register_handler(self, key: str, fn: HandlerFn) -> None:
        """Add a handler to the handlers dict."""
        self._handlers[key] = fn

    # ------------------------------------------------------------------
    # Run listeners: told once a run is on record
    # ------------------------------------------------------------------

    def add_run_listener(self, listener: RunListener) -> None:
        """Call ``listener(job, execution)`` after each run is recorded.

        "Recorded" means the run's execution row is saved and the job's
        post-run state (status, failure streak, last error, next run) is
        applied, so anything the listener publishes can be re-read and
        matches. ``job`` is the row as stored then, or ``None`` when the run
        deleted it (a one-shot job). A listener's failure is logged, never
        raised into the scheduler.
        """
        self._run_listeners.append(listener)

    async def publish_run_recorded(self, job_id: str, execution: JobExecution) -> None:
        if not self._run_listeners:
            return
        try:
            job = await self._store.get(job_id)
        except Exception:
            logger.exception("run_listener_job_read_failed id=%s", job_id)
            job = None
        for listener in list(self._run_listeners):
            try:
                await listener(job, execution)
            except Exception:
                logger.exception("run_listener_failed id=%s", job_id)

    # ------------------------------------------------------------------
    # Startup catchup
    # ------------------------------------------------------------------

    async def startup_catchup(self) -> None:
        """Called once at start to recover from downtime.

        1. Clear stale RUNNING jobs -> reset to PENDING
        2. Collect missed (overdue) jobs
        3. First max_catchup: agent jobs are deferred by
           ``startup_agent_delay_seconds`` (their ``next_run_at`` is moved, so
           the delay is visible and survives a crash); the rest run now with
           stagger
        4. Remaining: fast-forward next_run_at (except AT one-shots)
        """
        now = self._clock.wall()

        # Step 1: clear stale RUNNING jobs
        stale = await self._store.list_by_status(JobStatus.RUNNING)
        recovered_ids: set[str] = set()
        for job in stale:
            recovered_ids.add(job.id)
            job.status = JobStatus.PENDING
            job.updated_at = now
            clear_reservation(job)
            await self._store.save(job)
            logger.info("startup_reset_stale id=%s name=%s", job.id, job.name)

        # Step 2: collect missed jobs
        missed: list[CronJob] = []
        async for job in self._store.iter_due(now):
            if job.id in recovered_ids:
                logger.info("startup_skip_recovered_running id=%s", job.id)
                continue
            missed.append(job)

        if not missed:
            return

        # Step 3: first max_catchup jobs — defer agent turns, run the rest staggered
        catchup_jobs = missed[: self._max_catchup]
        remaining_jobs = missed[self._max_catchup :]
        deferred = [job for job in catchup_jobs if self._defers_at_startup(job)]
        immediate = [job for job in catchup_jobs if not self._defers_at_startup(job)]

        if immediate:
            reservations: list[JobReservation] = []
            for job in immediate:
                reservation = await self._store.reserve_due_job(
                    job.id,
                    now,
                    source="startup",
                    owner="scheduler-startup",
                )
                if isinstance(reservation, JobReservation):
                    reservations.append(reservation)
                else:
                    logger.info(
                        "startup_reservation_skipped id=%s reason=%s",
                        job.id,
                        reservation.reason,
                    )

            delays = spread_jobs(
                [r.job.id for r in reservations],
                window_seconds=self.CATCHUP_STAGGER_SECONDS,
            )

            for reservation in reservations:
                delay = delays.get(reservation.job.id, 0.0)
                task = asyncio.create_task(self._staggered_run(reservation, delay))
                self._catchup_tasks.add(task)
                task.add_done_callback(self._catchup_tasks.discard)
                self._track_token(reservation.token, task)

        if deferred:
            await self._defer_agent_jobs(deferred, now)

        # Step 4: fast-forward remaining (except AT one-shots). Delegates to
        # the shared next-run helper so EVERY+anchor jobs use the interval-grid
        # ceil math and CRON jobs use the field-matching scan with tz support.
        for job in remaining_jobs:
            if job.schedule_kind == ScheduleKind.AT:
                # Leave AT jobs as-is — they'll be picked up on next tick
                continue
            try:
                job.next_run_at = _next_run(job, now)
                job.updated_at = now
                await self._store.save(job)
                logger.info("startup_fast_forward id=%s next_run=%s", job.id, job.next_run_at)
            except Exception:
                logger.exception("startup_fast_forward_error id=%s", job.id)

    def _defers_at_startup(self, job: CronJob) -> bool:
        return self._startup_agent_delay > 0 and job.handler_key in AGENT_HANDLER_KEYS

    async def _defer_agent_jobs(self, jobs: list[CronJob], now: datetime) -> None:
        """Move missed agent jobs to ``now + delay + stagger`` instead of running them.

        Agent turns right after boot compete with channel start-up for the
        model budget. Persisting the new ``next_run_at`` (rather than holding
        an in-memory timer) keeps ``cron.list`` honest about when they will
        run and means a crash in between does not lose them.
        """
        delays = spread_jobs(
            [job.id for job in jobs],
            window_seconds=self.CATCHUP_STAGGER_SECONDS,
        )
        for job in jobs:
            offset = self._startup_agent_delay + delays.get(job.id, 0.0)
            job.next_run_at = now + timedelta(seconds=offset)
            job.updated_at = now
            try:
                await self._store.save(job, write_reservation=False)
            except Exception:
                logger.exception("startup_defer_error id=%s", job.id)
                continue
            logger.info(
                "startup_deferred_agent_job id=%s run_at=%s delay=%.1fs",
                job.id,
                job.next_run_at,
                offset,
            )

    async def _staggered_run(self, reservation: JobReservation, delay: float) -> None:
        """Run a catchup job after a stagger delay."""
        if not isinstance(reservation, JobReservation):
            raise TypeError("_staggered_run requires a JobReservation")
        if delay > 0:
            await asyncio.sleep(delay)
        await self._run_single(reservation)

    # ------------------------------------------------------------------
    # Tick loop
    # ------------------------------------------------------------------

    def _prune_finished(self) -> None:
        finished_ids = [jid for jid, task in self._running.items() if task.done()]
        for jid in finished_ids:
            del self._running[jid]

    def _active_count(self) -> int:
        return len(self._running) + sum(1 for task in self._superseded if not task.done())

    def _track_token(self, token: str, task: asyncio.Task[None]) -> None:
        self._task_by_token[token] = task

        def _forget(_task: asyncio.Task[None], token: str = token) -> None:
            if self._task_by_token.get(token) is _task:
                del self._task_by_token[token]

        task.add_done_callback(_forget)

    def _launch(self, reservation: JobReservation) -> asyncio.Task[None]:
        task = asyncio.create_task(self._run_single(reservation))
        job_id = reservation.job.id
        previous = self._running.get(job_id)
        if previous is not None and not previous.done():
            self._superseded[previous] = job_id
            previous.add_done_callback(lambda done: self._superseded.pop(done, None))
        self._running[job_id] = task
        self._track_token(reservation.token, task)
        return task

    async def _tick(self) -> None:
        """Main tick: collect due jobs, enforce burst limit, launch tasks."""
        now = self._clock.wall()

        # Collect due jobs
        due_jobs: list[CronJob] = []
        async for job in self._store.iter_due(now):
            due_jobs.append(job)

        # Refire gap guard — skip jobs that ran within MIN_REFIRE_GAP_SECONDS
        # (primarily protects against nudge-triggered rapid re-execution).
        # The floor is capped at the job's own configured EVERY interval, or
        # an EVERY job whose interval is under MIN_REFIRE_GAP_SECONDS (only
        # every_seconds=1, since ops.py rejects anything below 1) would be
        # silently throttled to the floor's cadence on every ordinary tick,
        # not just a nudge race (#3345).
        filtered = []
        for job in due_jobs:
            gap = _refire_gap_seconds(job)
            if job.last_run_at and job.last_run_at > now - timedelta(seconds=gap):
                logger.warning("refire_gap_skip id=%s last_run=%s", job.id, job.last_run_at)
                continue
            filtered.append(job)
        due_jobs = filtered

        # Clean up finished tasks from _running
        self._prune_finished()

        # Calculate available slots
        active_count = self._active_count()
        available_slots = self._max_concurrent - active_count
        if available_slots <= 0:
            if due_jobs:
                logger.warning(
                    "tick_no_slots due=%d running=%d max=%d",
                    len(due_jobs),
                    active_count,
                    self._max_concurrent,
                )
            due_jobs = []

        # Take first available_slots jobs, skip rest
        to_run = due_jobs[: max(available_slots, 0)]
        throttled = due_jobs[max(available_slots, 0) :]

        if throttled:
            logger.warning(
                "tick_throttled count=%d (max_concurrent=%d)",
                len(throttled),
                self._max_concurrent,
            )

        launched = 0
        for job in to_run:
            reservation = await self._store.reserve_due_job(
                job.id,
                now,
                source="timer",
                owner="scheduler-timer",
            )
            if not isinstance(reservation, JobReservation):
                logger.info(
                    "tick_reservation_skipped id=%s reason=%s",
                    job.id,
                    reservation.reason,
                )
                continue
            self._launch(reservation)
            launched += 1

        await self._handle_overlaps(now, free_slots=available_slots - launched)

    # ------------------------------------------------------------------
    # Overlap (concurrency policy)
    # ------------------------------------------------------------------

    @staticmethod
    def _pending_slot_while_running(job: CronJob) -> datetime | None:
        """The next slot of a job whose previous run is still active.

        A timer run consumed the slot it fired for (``next_run_at`` is that
        slot, at or before the reservation), so the next one is computed after
        the moment the run took the job -- coalescing whatever a late start
        skipped. A manual run did not consume anything: its ``next_run_at`` is
        still the pending slot. One-shot jobs have no next slot.
        """
        if job.schedule_kind == ScheduleKind.AT:
            return None
        reserved = job.reserved_at or job.last_run_at
        if reserved is None:
            return None
        if job.next_run_at is not None and job.next_run_at > reserved:
            return job.next_run_at
        try:
            return _next_run(job, reserved)
        except Exception:  # noqa: BLE001 - a broken schedule is reported on completion
            return None

    async def _running_jobs(self) -> list[CronJob]:
        return [
            job
            for job in await self._store.list_by_status(JobStatus.RUNNING)
            if job.reservation_token and job.enabled
        ]

    async def _next_overlap_due(self) -> datetime | None:
        """Earliest *future* next slot of a running ``replace``/``allow`` job.

        A slot already due was handled by the tick that just ran, or could not
        be (no free slot for ``allow``); waking for it again at the 0.1 s floor
        would only spin, so it waits for the ordinary cadence instead.
        """
        now = self._clock.wall()
        slots = [
            slot
            for job in await self._running_jobs()
            if job.concurrency != ConcurrencyPolicy.FORBID
            and (slot := self._pending_slot_while_running(job)) is not None
            and slot > now
        ]
        return min(slots) if slots else None

    async def _handle_overlaps(self, now: datetime, *, free_slots: int) -> None:
        """Apply each running job's concurrency policy once its next slot is due."""
        for job in await self._running_jobs():
            slot = self._pending_slot_while_running(job)
            if slot is None or slot > now:
                continue
            policy = job.concurrency
            if policy == ConcurrencyPolicy.FORBID:
                if self._forbid_logged.get(job.id) != slot:
                    self._forbid_logged[job.id] = slot
                    logger.info(
                        "concurrency_skip id=%s slot=%s policy=forbid (previous run still active)",
                        job.id,
                        slot.isoformat(),
                    )
                continue
            if policy == ConcurrencyPolicy.ALLOW and free_slots <= 0:
                logger.warning(
                    "concurrency_no_slot id=%s slot=%s policy=allow max_concurrent=%d",
                    job.id,
                    slot.isoformat(),
                    self._max_concurrent,
                )
                continue
            old_token = job.reservation_token
            reservation = await self._store.takeover_running_job(
                job.id,
                old_token,
                now,
                slot=slot,
                source=f"overlap-{policy.value}",
                owner="scheduler-timer",
            )
            if not isinstance(reservation, JobReservation):
                logger.info(
                    "concurrency_takeover_skipped id=%s reason=%s",
                    job.id,
                    reservation.reason,
                )
                continue
            previous = self._task_by_token.get(old_token)
            if policy == ConcurrencyPolicy.REPLACE:
                if previous is not None and not previous.done():
                    self._replaced_tokens.add(old_token)
                    previous.add_done_callback(partial(self._forget_replaced, old_token))
                    previous.cancel()
            else:
                free_slots -= 1
            self._launch(reservation)
            logger.info(
                "concurrency_%s id=%s slot=%s previous_owned=%s",
                policy.value,
                job.id,
                slot.isoformat(),
                previous is not None,
            )

    def _forget_replaced(self, token: str, _task: asyncio.Task[None]) -> None:
        self._replaced_tokens.discard(token)

    async def _run_single(self, reservation: JobReservation) -> None:
        """Execute one job: handler lookup, timeout, persist execution, apply result."""
        if not isinstance(reservation, JobReservation):
            raise TypeError("_run_single requires a JobReservation")
        job = reservation.job
        handler = self._handlers.get(job.handler_key)

        if handler is None:
            await self._store.finalize_reserved_missing_handler(
                job.id,
                reservation.token,
                error=f"No handler registered for key '{job.handler_key}'",
            )
            return

        try:
            exe = await execute_with_timeout(job, handler)
        except asyncio.CancelledError:
            if reservation.token not in self._replaced_tokens:
                raise
            # ``replace`` cancelled this run on purpose: record it in history,
            # do not count it as a failure, and let the task end normally.
            self._replaced_tokens.discard(reservation.token)
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
            await self._record_replaced_run(reservation)
            return
        await self._store.save_execution(exe)
        await apply_reserved_result(
            job.id, reservation.token, exe, self._store, now=self._clock.wall()
        )
        # Only now: a client that re-reads the job on this news sees the run.
        await self.publish_run_recorded(job.id, exe)

    async def _record_replaced_run(self, reservation: JobReservation) -> None:
        execution = JobExecution(
            job_id=reservation.job.id,
            started_at=reservation.reserved_at,
            finished_at=self._clock.wall(),
            success=False,
            error=REPLACED_RUN_ERROR,
            delivery_status=REPLACED_RUN_STATUS,
        )
        try:
            await self._store.save_execution(execution)
        except Exception:
            logger.exception("concurrency_replaced_record_failed id=%s", reservation.job.id)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def _next_wake_at(self) -> datetime | None:
        candidates = [
            instant
            for instant in (await self._store.next_due_at(), await self._next_overlap_due())
            if instant is not None
        ]
        return min(candidates) if candidates else None

    def _arm(self, next_due: datetime | None) -> ArmedTimer:
        wall_now = self._clock.wall()
        mono_now = self._clock.monotonic()
        if next_due is None:
            delay = self.MAX_TIMER_DELAY
        else:
            delay = (next_due - wall_now).total_seconds()
            delay = max(0.1, min(delay, self.MAX_TIMER_DELAY))
        return ArmedTimer(armed_wall=wall_now, armed_mono=mono_now, delay=delay)

    async def _wait_for_next_due(self) -> WakeReport:
        """Sleep until the next due instant on either clock, or a nudge."""
        armed = self._arm(await self._next_wake_at())
        nudged = False
        while True:
            remaining = armed.remaining(self._clock.wall(), self._clock.monotonic())
            if remaining <= _DUE_EPSILON_SECONDS:
                break
            if await self._clock.wait(self._nudge_event, min(remaining, self.MAX_TIMER_DELAY)):
                nudged = True
                break
        self._nudge_event.clear()
        report = WakeReport(
            expected_seconds=armed.delay,
            wall_elapsed=(self._clock.wall() - armed.armed_wall).total_seconds(),
            mono_elapsed=self._clock.monotonic() - armed.armed_mono,
            nudged=nudged,
        )
        self._log_wake(report)
        self.last_wake = report
        return report

    @staticmethod
    def _log_wake(report: WakeReport) -> None:
        if report.forward_gap > CLOCK_JUMP_TOLERANCE_SECONDS:
            logger.warning(
                "scheduler_wake_gap gap=%.1fs wall_elapsed=%.1fs mono_elapsed=%.1fs "
                "(machine slept or the clock moved forward)",
                report.forward_gap,
                report.wall_elapsed,
                report.mono_elapsed,
            )
        if report.backward_jump > CLOCK_JUMP_TOLERANCE_SECONDS:
            logger.warning(
                "scheduler_clock_back jump=%.1fs wall_elapsed=%.1fs mono_elapsed=%.1fs",
                report.backward_jump,
                report.wall_elapsed,
                report.mono_elapsed,
            )
        if report.late:
            logger.warning(
                "scheduler_late_wake expected=%.1fs actual=%.1fs nudged=%s",
                report.expected_seconds,
                report.wall_elapsed,
                report.nudged,
            )

    async def _loop(self) -> None:
        """Main loop: sleep until next due, tick, reap, repeat."""
        while self._started:
            try:
                await self._wait_for_next_due()

                await self._tick()

                # Reap if reaper exists
                if self._reaper is not None and hasattr(self._reaper, "maybe_reap"):
                    await self._reaper.maybe_reap()

            except Exception:
                logger.exception("scheduler_loop_error")
                await asyncio.sleep(5.0)

    # ------------------------------------------------------------------
    # Nudge
    # ------------------------------------------------------------------

    def nudge(self) -> None:
        """Wake the loop immediately (e.g. after adding a new job, or a thaw)."""
        self._nudge_event.set()

    # ------------------------------------------------------------------
    # Cancel
    # ------------------------------------------------------------------

    def cancel_running(self, job_id: str) -> None:
        """Cancel a specific running task (and any earlier overlapping run of it)."""
        task = self._running.pop(job_id, None)
        if task is not None and not task.done():
            task.cancel()
        for other, other_job_id in list(self._superseded.items()):
            if other_job_id == job_id and not other.done():
                other.cancel()

    # ------------------------------------------------------------------
    # Start / stop
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the timer loop."""
        if self._started:
            return
        self._started = True
        self._loop_task = asyncio.create_task(self._loop())
        logger.info("scheduler_timer_started")

    async def stop(self) -> None:
        """Stop the timer loop and await cancellation of all owned job tasks."""
        self._started = False
        self._nudge_event.set()
        if self._loop_task is not None and not self._loop_task.done():
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
        job_tasks = {*self._running.values(), *self._catchup_tasks, *self._superseded}
        for task in job_tasks:
            if not task.done():
                task.cancel()
        if job_tasks:
            await asyncio.gather(*job_tasks, return_exceptions=True)
        self._running.clear()
        self._catchup_tasks.clear()
        self._superseded.clear()
        self._task_by_token.clear()
        logger.info("scheduler_timer_stopped")
