"""Scheduler domain types."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class ScheduleKind(StrEnum):
    CRON = "cron"
    AT = "at"
    EVERY = "every"


class SessionTarget(StrEnum):
    MAIN = "main"
    ISOLATED = "isolated"
    CURRENT = "current"
    SESSION = "session"


class DeliveryMode(StrEnum):
    NONE = "none"
    ORIGIN = "origin"
    CHANNEL = "channel"
    WEBHOOK = "webhook"


class CronWakeMode(StrEnum):
    NEXT_HEARTBEAT = "next-heartbeat"
    NOW = "now"


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    FAILED = "failed"
    DISABLED = "disabled"
    DELETED = "deleted"


class ConcurrencyPolicy(StrEnum):
    """What the timer does when a job's next slot comes due while it still runs.

    ``forbid`` (default) skips that slot; the next one is computed from when
    the running run finishes. ``replace`` cancels the running run (when the
    scheduler's own loop started it) and starts the new one. ``allow`` starts
    the new run alongside; the newest run owns the job's schedule state and
    the earlier one is kept in run history only.
    """

    FORBID = "forbid"
    REPLACE = "replace"
    ALLOW = "allow"


CONCURRENCY_POLICIES: tuple[str, ...] = tuple(policy.value for policy in ConcurrencyPolicy)


def normalize_concurrency(value: object) -> ConcurrencyPolicy:
    """Coerce a wire/CLI value to a policy; ``None``/empty is the default ``forbid``."""
    if isinstance(value, ConcurrencyPolicy):
        return value
    if value is None or (isinstance(value, str) and not value.strip()):
        return ConcurrencyPolicy.FORBID
    raw = str(value).strip().lower()
    try:
        return ConcurrencyPolicy(raw)
    except ValueError:
        raise ValueError(
            f"concurrency must be one of {', '.join(CONCURRENCY_POLICIES)}; got {value!r}"
        ) from None


#: A job is reported ``degraded`` from this many failures in a row.
DEGRADED_FAILURE_THRESHOLD = 3


def job_health(consecutive_errors: object, last_error: object) -> dict[str, Any]:
    """The ``health`` block of a job's wire payload.

    ``consecutive_failures`` is the job's run-to-run failure streak (reset by
    a success). ``last_error`` is the error of the most recent *finished* run,
    ``None`` after a success; an in-progress run does not clear it.
    """
    failures = max(0, consecutive_errors) if isinstance(consecutive_errors, int) else 0
    return {
        "consecutive_failures": failures,
        "last_error": str(last_error) if last_error else None,
        "degraded": failures >= DEGRADED_FAILURE_THRESHOLD,
    }


class ReservationRejectionReason(StrEnum):
    NOT_FOUND = "not_found"
    BUSY = "busy"
    NOT_DUE = "not_due"
    DISABLED = "disabled"
    STATUS_CONFLICT = "status_conflict"
    BACKING_OFF = "backing_off"


class ManualRunStatus(StrEnum):
    ACCEPTED = "accepted"
    NOT_FOUND = "not_found"
    NO_HANDLER = "no_handler"
    BUSY = "busy"
    DISABLED = "disabled"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class ReplyTargetSnapshot:
    channel_name: str = ""
    channel_type: str = ""
    to: str = ""
    account_id: str = ""
    thread_id: str = ""
    request_id: str | None = None


@dataclass
class FailureDestination:
    """Separate destination for failure notifications.

    When a cron job's primary delivery is e.g. ``announce`` to a public
    channel, ``failure_destination`` lets operators receive errors out-of-band
    (different channel or a webhook). Mode must be ``channel`` or ``webhook``.
    """

    mode: DeliveryMode = DeliveryMode.NONE
    channel_name: str = ""
    channel_id: str = ""
    account_id: str = ""
    thread_id: str = ""
    webhook_url: str = ""
    webhook_token: str = ""


@dataclass
class DeliveryConfig:
    """Delivery routing for cron job results."""

    mode: DeliveryMode = DeliveryMode.NONE
    channel_name: str = ""  # adapter key: "slack", "discord", "telegram"
    channel_id: str = ""  # target channel/chat ID
    account_id: str = ""  # optional account binding for multi-account channels
    thread_id: str = ""  # optional thread ID
    ws_topic: str = ""  # WS targeted push topic, default "cron:{job_id}"
    originating_reply_target: ReplyTargetSnapshot | None = None
    webhook_url: str = ""  # http(s) endpoint for mode=WEBHOOK
    webhook_token: str = ""  # optional bearer token for webhook Authorization
    best_effort: bool = False  # when True, delivery failure does not fail the job
    failure_destination: FailureDestination | None = None


# Two different things used to share one 500-char cap, which meant a script job's
# stdout was destroyed at write time and the run-history drawer could never show it.
# They are separated now: what goes out over WS/webhook stays small, what goes into
# the run record keeps the whole output (up to a sanity ceiling).
CRON_SUMMARY_PREVIEW_CHARS = 500
CRON_RUN_OUTPUT_MAX_CHARS = 200_000


def clamp_run_output(text: str | None) -> str | None:
    """Cap output stored on a run record — generous, only a runaway-script backstop."""
    if not text:
        return text
    return text[:CRON_RUN_OUTPUT_MAX_CHARS]


def preview_summary(text: str | None) -> str | None:
    """Short preview for delivery payloads (WS broadcast, webhook POST, list rows)."""
    if not text:
        return text
    return text[:CRON_SUMMARY_PREVIEW_CHARS]


@dataclass
class HandlerResult:
    """Typed return from cron job handlers."""

    summary: str | None = None
    session_key: str = ""
    delivery_status: str = "skipped"  # "delivered" | "failed" | "skipped"


@dataclass
class CronJob:
    """A scheduled cron job definition."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    cron_expr: str = ""
    handler_key: str = ""
    payload: dict = field(default_factory=dict)
    status: JobStatus = JobStatus.PENDING
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None
    run_count: int = 0
    error_count: int = 0
    last_error: str | None = None
    max_retries: int = 3
    jitter_seconds: float = 0.0

    # New fields
    schedule_kind: ScheduleKind = ScheduleKind.CRON
    schedule_raw: str = ""
    tz: str = ""  # IANA timezone for CRON schedules; empty == UTC
    anchor_at: datetime | None = None  # EVERY+interval anchor (UTC)
    # Creation metadata is retained for delivery routing and "created from"
    # presentation. It does not scope list, remove, or run permissions.
    creator_session_key: str = ""
    creator_sender_id: str = ""
    session_target: SessionTarget = SessionTarget.ISOLATED
    session_key: str = ""
    origin_session_key: str = ""
    timeout_seconds: float = 600.0
    wake_mode: CronWakeMode = CronWakeMode.NOW
    delete_after_run: bool = False
    enabled: bool = True
    backoff_until: datetime | None = None
    consecutive_errors: int = 0
    delivery: DeliveryConfig = field(default_factory=DeliveryConfig)
    reservation_token: str = ""
    reserved_at: datetime | None = None
    reserved_by: str = ""
    reservation_source: str = ""
    scheduled_run_at: datetime | None = None
    tool_policy: dict[str, Any] = field(default_factory=dict)
    concurrency: ConcurrencyPolicy = ConcurrencyPolicy.FORBID


@dataclass
class JobExecution:
    """Record of a single job execution."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    job_id: str = ""
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    success: bool = False
    error: str | None = None
    summary: str | None = None
    session_key: str = ""
    delivery_status: str = ""


@dataclass(frozen=True)
class JobReservation:
    job: CronJob
    token: str
    reserved_at: datetime
    reserved_by: str
    reservation_source: str
    scheduled_run_at: datetime | None = None


@dataclass(frozen=True)
class JobReservationRejected:
    job_id: str
    reason: ReservationRejectionReason
    job: CronJob | None = None
    message: str = ""


@dataclass(frozen=True)
class ManualRunResult:
    status: ManualRunStatus
    execution: JobExecution | None = None
    reason: str = ""
    error: str | None = None
    current_status: str = ""
    backoff_until: datetime | None = None

    @property
    def success(self) -> bool:
        return self.status == ManualRunStatus.ACCEPTED and bool(
            self.execution and self.execution.success
        )

    @property
    def summary(self) -> str | None:
        return self.execution.summary if self.execution else None

    @property
    def started_at(self) -> datetime | None:
        return self.execution.started_at if self.execution else None

    @property
    def finished_at(self) -> datetime | None:
        return self.execution.finished_at if self.execution else None


def clear_reservation(job: CronJob) -> None:
    job.reservation_token = ""
    job.reserved_at = None
    job.reserved_by = ""
    job.reservation_source = ""
    job.scheduled_run_at = None
