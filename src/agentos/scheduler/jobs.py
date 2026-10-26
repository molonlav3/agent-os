"""Job execution: timeout, backoff, and post-execution state machine."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from .parser import CronExpression, parse_cron
from .persistence import JobStore
from .types import (
    CronJob,
    HandlerResult,
    JobExecution,
    JobStatus,
    ScheduleKind,
    clamp_run_output,
    clear_reservation,
)

logger = logging.getLogger(__name__)


# Module-level notifier for schedule-compute auto-disable. The embedding
# process registers a callable via set_schedule_failure_notifier(); when a job
# is FAILED because its next-run computation crashed repeatedly, the notifier
# is invoked best-effort so the auto-disable is not silent.
ScheduleFailureNotifier = Callable[[CronJob, str], None]
_schedule_failure_notifier: ScheduleFailureNotifier | None = None


def set_schedule_failure_notifier(fn: ScheduleFailureNotifier | None) -> None:
    """Register (or clear) the notifier called when a job auto-disables on schedule error."""
    global _schedule_failure_notifier
    _schedule_failure_notifier = fn


# Module-level dispatcher for delivery.failure_destination alerts. The
# embedding process registers an awaitable callable via
# set_failure_dispatcher(); execute_with_timeout invokes it whenever a job
# execution fails (any handler, any error class) and the job configures a
# FailureDestination. The boot wiring typically connects this to
# DeliveryChain.dispatch_failure_alert so the alert lands on the FD target
# without going through the primary delivery path.
FailureDispatcher = Callable[[CronJob, str], Awaitable[None | str]]
_failure_dispatcher: FailureDispatcher | None = None


def set_failure_dispatcher(fn: FailureDispatcher | None) -> None:
    """Register (or clear) the async dispatcher for failure_destination alerts."""
    global _failure_dispatcher
    _failure_dispatcher = fn


# Error patterns indicating a transient failure (retry-eligible).
# Permanent errors disable the job immediately; transient errors retry with backoff.
_TRANSIENT_ERROR_PATTERNS = (
    "rate limit",
    "too many requests",
    "resource exhausted",
    "overload",
    "timeout",
    "timed out",
    "econnreset",
    "fetch failed",
    "socket",
    "network",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "cloudflare",
)
_PERMANENT_ERROR_PATTERNS = (
    "unauthorized",
    "invalid api key",
    "invalid_api_key",
    "forbidden",
    "validation",
    "invalid request",
    "no handler registered",
)
# Bare status codes are matched as whole numbers rather than by plain
# containment: "403" is a substring of the "4033" in "timed out after 4033ms",
# and a permanent classification disables the job on its first failure with no
# retry, so a duration that happens to contain an auth code must not read as
# one. \b is not enough — "." is a non-word character, so it still reads
# "12.403 seconds" as a 403 — hence an explicit digit-and-dot guard in front
# and a digit guard behind. A trailing "." is deliberately still allowed, so a
# code that ends a sentence ("got 403.") keeps matching, while a dot followed
# by a letter is rejected so a script named report-403.sh does not disable its
# own job (scripts.py interpolates path.name into the error text). Because the
# guard is about digits rather than word characters it also still matches a
# code glued to a name, like "http_403", which plain \b would have dropped.
_TRANSIENT_ERROR_CODES = ("429", "529", "502", "503", "504")
_PERMANENT_ERROR_CODES = ("401", "403")
_TRANSIENT_ERROR_CODE_RE = re.compile(
    rf"(?<![\d.])(?:{'|'.join(_TRANSIENT_ERROR_CODES)})(?!\d)(?!\.[A-Za-z])"
)
_PERMANENT_ERROR_CODE_RE = re.compile(
    rf"(?<![\d.])(?:{'|'.join(_PERMANENT_ERROR_CODES)})(?!\d)(?!\.[A-Za-z])"
)


def classify_error(error_text: str | None) -> str:
    """Return 'transient' or 'permanent' for a job error string.

    Defaults to 'transient' on ambiguous text so the existing recurring-job
    backoff schedule remains the safe fallback. Use 'permanent' only when
    the text matches a known auth/config-style signature.
    """
    if not error_text:
        return "transient"
    lowered = error_text.lower()
    if any(pattern in lowered for pattern in _PERMANENT_ERROR_PATTERNS):
        return "permanent"
    if _PERMANENT_ERROR_CODE_RE.search(lowered):
        return "permanent"
    # Both transient checks are redundant with the default below and are kept,
    # as the pattern loop already was before this change, so the taxonomy stays
    # explicit and symmetric with the permanent side.
    if any(pattern in lowered for pattern in _TRANSIENT_ERROR_PATTERNS):
        return "transient"
    if _TRANSIENT_ERROR_CODE_RE.search(lowered):
        return "transient"
    return "transient"


# Exponential backoff schedule for retryable jobs.
BACKOFF_SCHEDULE: list[int] = [30, 60, 300, 900, 3600]  # 30s, 1m, 5m, 15m, 60m
MAX_CONSECUTIVE_ERRORS: int = 5
_ONE_SHOT_MAX_RETRIES: int = 3

HandlerFn = Callable[[CronJob], Awaitable[HandlerResult | str | tuple | None]]


def compute_backoff(consecutive_errors: int) -> float:
    """Return backoff delay in seconds for the given consecutive error count.

    Returns 0.0 for zero errors, otherwise indexes into BACKOFF_SCHEDULE
    (capped at the last entry).
    """
    if consecutive_errors <= 0:
        return 0.0
    idx = min(consecutive_errors - 1, len(BACKOFF_SCHEDULE) - 1)
    return float(BACKOFF_SCHEDULE[idx])


async def execute_with_timeout(job: CronJob, handler: HandlerFn) -> JobExecution:
    """Wrap a handler call with asyncio timeout, returning a JobExecution record.

    The handler may return:
    - str: treated as summary text
    - (str, str): treated as (summary, session_key)
    - None: no summary
    """
    execution = JobExecution(job_id=job.id, started_at=datetime.now(UTC))
    try:
        task = handler(job)
        if job.timeout_seconds <= 0:
            result = await task
        else:
            result = await asyncio.wait_for(task, timeout=job.timeout_seconds)
        execution.success = True
        if isinstance(result, HandlerResult):
            execution.summary = clamp_run_output(result.summary)
            execution.session_key = result.session_key
            execution.delivery_status = result.delivery_status
        elif isinstance(result, tuple) and len(result) >= 2:
            execution.summary = clamp_run_output(result[0]) if isinstance(result[0], str) else None
            execution.session_key = result[1] or ""
            if len(result) >= 3:
                execution.delivery_status = result[2] or ""
        elif isinstance(result, str):
            execution.summary = clamp_run_output(result)
        else:
            execution.summary = None
    except TimeoutError:
        execution.success = False
        execution.error = f"Timeout after {job.timeout_seconds}s"
        logger.warning("job_timeout id=%s timeout=%.1f", job.id, job.timeout_seconds)
    except Exception as exc:
        execution.success = False
        execution.error = str(exc)
        logger.exception("job_error id=%s", job.id)
    finally:
        execution.finished_at = datetime.now(UTC)

    # Failure-destination alert: any failed handler run (agent_run raise,
    # system_event raise, timeout, generic exception) routes its error to the
    # job's configured FailureDestination, if any. This is the single dispatch
    # site so all failure paths reach the FD uniformly.
    if (
        not execution.success
        and job.delivery.failure_destination is not None
        and _failure_dispatcher is not None
    ):
        try:
            await _failure_dispatcher(job, execution.error or "")
        except Exception:  # noqa: BLE001 — dispatcher is best-effort
            logger.warning("failure_dispatcher_raised id=%s", job.id, exc_info=True)

    return execution


#: A DST gap is an hour at most in every zone tzdata ships, but the bound is
#: what stops a malformed zone turning one tick into an unbounded inner loop.
_MAX_GAP_MINUTES = 24 * 60


def _skipped_wall_minutes(
    previous_wall: datetime | None,
    wall: datetime,
) -> list[datetime]:
    """Local minutes that the clock jumped over between two UTC candidates.

    Returned naive, because these wall times do not exist in the zone -- that
    is the point of them. They are only ever fed to ``CronExpression.matches``,
    which reads calendar fields and never converts.

    Empty when the clock moved normally, and empty across a fall-back, where
    the local time goes backwards rather than forwards.
    """
    if previous_wall is None:
        return []
    start = previous_wall.replace(tzinfo=None)
    end = wall.replace(tzinfo=None)
    if end - start <= timedelta(minutes=1):
        return []
    missing: list[datetime] = []
    cursor = start + timedelta(minutes=1)
    while cursor < end and len(missing) < _MAX_GAP_MINUTES:
        missing.append(cursor)
        cursor += timedelta(minutes=1)
    return missing


def _next_run(job: CronJob, after: datetime) -> datetime:
    """Compute the next execution time for a recurring job after *after*.

    When ``job.tz`` is a valid IANA name, cron field matching evaluates the
    wall time in that zone instead of UTC. The returned datetime is in UTC so
    callers can compare it against other UTC instants directly.

    EVERY+interval schedules align to ``job.anchor_at`` when set:
    next_run = anchor + ceil((after-anchor)/interval) * interval. This keeps
    fire times stable across restarts and slow ticks.
    """
    if job.schedule_kind == ScheduleKind.EVERY and job.cron_expr.isdigit():
        interval_seconds = int(job.cron_expr)
        if job.anchor_at is not None:
            anchor = job.anchor_at
            if anchor.tzinfo is None:
                anchor = anchor.replace(tzinfo=UTC)
            elapsed = (after - anchor).total_seconds()
            if elapsed < 0:
                return anchor
            # Smallest positive integer k such that anchor + k*interval > after.
            steps = int(elapsed // interval_seconds) + 1
            return anchor + timedelta(seconds=steps * interval_seconds)
        return after + timedelta(seconds=interval_seconds)

    # Standard cron: jump field by field to the next matching wall time
    expr = parse_cron(job.cron_expr)
    tz_name = (job.tz or "").strip()
    tz = ZoneInfo(tz_name) if tz_name else None
    instant = _next_cron_instant(expr, after, tz)
    if instant is None:
        raise ValueError(f"No valid next run found for expression '{job.cron_expr}'")
    return instant + timedelta(seconds=job.jitter_seconds)


#: How far ahead a schedule is searched before it is declared to never fire.
#: Four years covers a leap-day job; it was also the horizon of the old
#: minute-by-minute scan, so what is refused does not change.
_NEXT_RUN_HORIZON_YEARS = 4


def _first_fire_in_a_repeated_hour(
    expr: CronExpression, after: datetime, zone: ZoneInfo
) -> datetime | None:
    """The first fire in a repeated hour that the wall-clock walk jumps over.

    On a fall-back night the same wall time happens twice, so the second pass
    lies *behind* the first on the naive clock :func:`_next_cron_instant`
    walks forward: from 01:45 EDT the walk goes to 02:00 EST and never looks
    at 01:00-01:45 EST, dropping every fire in between (review on #3105).
    Those minutes are bounded by the length of the repetition -- an hour in
    every zone tzdata ships -- so they are checked one at a time, and only
    when *after* itself sits in the first pass of a repeated span.

    Callers gate this on a wildcard hour: an interval schedule runs through
    the repeated hour, while one naming an hour fires once per scheduled
    local time (#2472).
    """
    wall = after.astimezone(zone).replace(second=0, microsecond=0, tzinfo=None)
    first = wall.replace(tzinfo=zone, fold=0).astimezone(UTC)
    second = wall.replace(tzinfo=zone, fold=1).astimezone(UTC)
    if second <= first or after >= second:
        # Not an ambiguous wall time, or the second pass is already behind us.
        return None
    candidate = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    while candidate <= second:
        if candidate > after and expr.matches(candidate.astimezone(zone)):
            return candidate
        candidate += timedelta(minutes=1)
    return None


def _next_cron_instant(
    expr: CronExpression, after: datetime, tz: ZoneInfo | None
) -> datetime | None:
    """The first UTC instant strictly after *after* at which *expr* fires.

    The search works on the wall clock in *tz* and jumps a whole field at a
    time: a non-matching month goes to the first day of the next matching
    month, a non-matching day to the next midnight, a non-matching hour or
    minute to the next matching one. Stepping one minute at a time instead
    cost O(minutes until the next fire) -- about a second for a yearly
    schedule and four for a leap-day one -- inside the synchronous add/
    reschedule path (#3099). The loop here runs a few dozen times at most.

    Each matching wall time is mapped back to an instant through the zone,
    which is what decides the daylight-saving cases: a wall time inside a
    spring-forward gap does not exist, so the job fires once at the instant
    the gap ends; a wall time that occurs twice on a fall-back night fires on
    its first occurrence, and again on its second only for a schedule whose
    hour is a wildcard (#2472).
    """
    zone = tz or UTC
    # The walk below only ever moves forward on the wall clock, so a repeated
    # hour's second pass has to be looked for separately.
    second_pass = (
        _first_fire_in_a_repeated_hour(expr, after, tz)
        if tz is not None and expr.hour.is_wildcard
        else None
    )
    wall = (after.astimezone(zone) + timedelta(minutes=1)).replace(
        second=0, microsecond=0, tzinfo=None
    )
    months = sorted(expr.month.values)
    hours = sorted(expr.hour.values)
    minutes = sorted(expr.minute.values)
    horizon_year = wall.year + _NEXT_RUN_HORIZON_YEARS

    while wall.year <= horizon_year:
        if wall.month not in expr.month.values:
            later = [m for m in months if m > wall.month]
            if later:
                wall = wall.replace(month=later[0], day=1, hour=0, minute=0)
            else:
                wall = wall.replace(year=wall.year + 1, month=months[0], day=1, hour=0, minute=0)
            continue
        if not expr.matches_day(wall):
            wall = wall.replace(hour=0, minute=0) + timedelta(days=1)
            continue
        if wall.hour not in expr.hour.values:
            later = [h for h in hours if h > wall.hour]
            if later:
                wall = wall.replace(hour=later[0], minute=0)
            else:
                wall = wall.replace(hour=0, minute=0) + timedelta(days=1)
            continue
        if wall.minute not in expr.minute.values:
            later = [m for m in minutes if m > wall.minute]
            if later:
                wall = wall.replace(minute=later[0])
            else:
                wall = wall.replace(minute=0) + timedelta(hours=1)
            continue

        instant = wall.replace(tzinfo=zone).astimezone(UTC)
        if instant.astimezone(zone).replace(tzinfo=None) != wall:
            # Spring forward: this wall time never happens. Rather than skip
            # the day, the job fires once at the first instant the clock does
            # show, which is where the gap ends (#2472).
            for _ in range(_MAX_GAP_MINUTES):
                wall += timedelta(minutes=1)
                instant = wall.replace(tzinfo=zone).astimezone(UTC)
                if instant.astimezone(zone).replace(tzinfo=None) == wall:
                    break
            else:  # pragma: no cover - no zone in tzdata skips a whole day
                continue
            if instant <= after:
                continue
            return min(instant, second_pass) if second_pass is not None else instant
        if instant <= after:
            # On a fall-back night the same wall time happens twice, and the
            # first pass is the one already behind us. ``fold=1`` names the
            # second, a later instant reading as this same wall time, so an
            # interval schedule keeps running through the repeated hour
            # (review on #3105); one naming an hour fires once per scheduled
            # local time and stops here (#2472).
            if expr.hour.is_wildcard:
                folded = wall.replace(tzinfo=zone, fold=1).astimezone(UTC)
                if folded > after and folded.astimezone(zone).replace(tzinfo=None) == wall:
                    return folded
            wall += timedelta(minutes=1)
            continue
        return min(instant, second_pass) if second_pass is not None else instant
    return second_pass


async def apply_result(job: CronJob, execution: JobExecution, store: JobStore) -> None:
    """Compatibility wrapper for tests and non-reserved state transitions.

    Production scheduler execution should use ``apply_reserved_result`` so the
    caller proves ownership of a persisted reservation token.
    """
    current = await store.get(job.id)
    if current is None:
        return
    if current.status in (JobStatus.PAUSED, JobStatus.DISABLED):
        return

    delete_job = _apply_result_state(current, execution, datetime.now(UTC))
    if delete_job:
        await store.delete(current.id)
    else:
        await store.save(current)


async def apply_reserved_result(
    job_id: str,
    reservation_token: str,
    execution: JobExecution,
    store: JobStore,
    *,
    now: datetime | None = None,
) -> bool:
    """Apply an execution result only when the reservation token still owns the job.

    ``now`` is the wall-clock instant the result is applied at (the scheduler
    timer passes its own clock); defaults to the current time.
    """
    current = await store.get(job_id)
    if current is None:
        return False
    if current.reservation_token != reservation_token:
        return False
    if current.status in (JobStatus.PAUSED, JobStatus.DISABLED):
        clear_reservation(current)
        await store.save(current)
        return True

    delete_job = _apply_result_state(current, execution, now or datetime.now(UTC))
    if delete_job:
        await store.delete(current.id)
    else:
        await store.save(current)
    return True


def _schedule_base(job: CronJob, now: datetime) -> datetime:
    """The instant a finished run's next slot is computed after.

    Normally ``now``. If the wall clock was set back while the run was in
    flight, ``now`` can sit *before* the slot that just fired, and computing
    "the next slot after now" would hand back that same slot -- the job would
    fire twice for one schedule tick. ``reserved_at`` is when this run took
    the job, so the next slot is never earlier than the run itself.
    """
    reserved = job.reserved_at
    if reserved is None:
        return now
    if reserved.tzinfo is None:
        reserved = reserved.replace(tzinfo=UTC)
    return max(now, reserved)


def _apply_result_state(job: CronJob, execution: JobExecution, now: datetime) -> bool:
    """Post-execution state machine: update job state based on execution outcome.

    Handles:
    - Deleted/paused while running (no-op)
    - Success: reset error counters, reschedule or disable/delete
    - Failure: increment counters, backoff, mark FAILED or DISABLED at thresholds

    Returns True when the job should be deleted.
    """
    if execution.success:
        job.consecutive_errors = 0
        job.backoff_until = None
        job.last_error = None
        job.run_count += 1
        job.updated_at = now

        if job.schedule_kind == ScheduleKind.AT:
            clear_reservation(job)
            if job.delete_after_run:
                return True
            else:
                job.status = JobStatus.DISABLED
                job.enabled = False
        else:
            try:
                job.next_run_at = _next_run(job, _schedule_base(job, now))
                job.status = JobStatus.PENDING
            except Exception as exc:
                _mark_schedule_compute_failed(job, exc, now, increment_error=True)
            else:
                clear_reservation(job)

    else:
        job.consecutive_errors += 1
        job.error_count += 1
        job.last_error = execution.error
        job.run_count += 1
        job.updated_at = now

        classification = classify_error(execution.error)
        if classification == "permanent":
            # Auth/config errors disable immediately rather than cycling through
            # transient-error backoff. One-shot AT jobs already short-circuit on
            # permanent errors via _ONE_SHOT_MAX_RETRIES; for recurring jobs we
            # eagerly disable to avoid burning retries on something only a human
            # can fix.
            job.status = JobStatus.DISABLED
            job.enabled = False
            job.next_run_at = None
            job.backoff_until = None
            clear_reservation(job)
            logger.error(
                "job_permanent_error_disabled id=%s reason=%s",
                job.id,
                (execution.error or "")[:120],
            )
            return False

        is_recurring = job.schedule_kind in (ScheduleKind.CRON, ScheduleKind.EVERY)
        if is_recurring:
            if job.consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                job.status = JobStatus.FAILED
                job.next_run_at = None
                job.backoff_until = None
                clear_reservation(job)
                logger.error(
                    "job_failed_permanently id=%s consecutive_errors=%d",
                    job.id,
                    job.consecutive_errors,
                )
            else:
                backoff_secs = compute_backoff(job.consecutive_errors)
                try:
                    job.next_run_at = _next_run(job, _schedule_base(job, now))
                except Exception as exc:
                    _mark_schedule_compute_failed(job, exc, now, increment_error=False)
                else:
                    job.backoff_until = now + timedelta(seconds=backoff_secs)
                    job.status = JobStatus.PENDING
                    clear_reservation(job)
        else:
            # One-shot AT job
            if job.consecutive_errors >= _ONE_SHOT_MAX_RETRIES:
                job.status = JobStatus.DISABLED
                job.enabled = False
                clear_reservation(job)
                logger.error(
                    "job_one_shot_disabled id=%s consecutive_errors=%d",
                    job.id,
                    job.consecutive_errors,
                )
            else:
                backoff_secs = compute_backoff(job.consecutive_errors)
                job.backoff_until = now + timedelta(seconds=backoff_secs)
                job.status = JobStatus.PENDING
                clear_reservation(job)

    return False


def _mark_schedule_compute_failed(
    job: CronJob,
    exc: Exception,
    now: datetime,
    *,
    increment_error: bool,
) -> None:
    if increment_error:
        job.error_count += 1
        job.consecutive_errors += 1
    job.status = JobStatus.FAILED
    job.last_error = f"schedule compute failed: {exc}"
    job.updated_at = now
    job.backoff_until = None
    job.next_run_at = None
    clear_reservation(job)
    notifier = _schedule_failure_notifier
    if notifier is not None:
        try:
            notifier(job, str(exc))
        except Exception:  # noqa: BLE001 — notifier is best-effort
            logger.warning("schedule_failure_notifier_raised id=%s", job.id, exc_info=True)
