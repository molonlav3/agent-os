"""Stop a session's run and every child run it spawned, and wait for the end.

``sessions.abort`` used to flip cancel flags and answer at once. The client
was told "aborted" while the run was still unwinding, the terminal frame was
whatever the cancel happened to leave behind, and coding-harness / subagent
children the run had spawned kept going. This module does the whole job:

1. capture the exact runs (task ids) of the session and of its descendants;
2. hold their terminal publication (:mod:`agentos.gateway.terminal_events`) so
   nothing the stream emits while the cancel lands is published on its own;
3. cancel the session's runs, then the children's (and their harness turns),
   and withdraw the approval prompts they were waiting on;
4. wait, bounded, until each run's terminal status is persisted -- the runtime
   then publishes ``session.event.done {state: "aborted", partial_text}``,
   ``task.cancelled`` and ``sessions.changed``;
5. report what was stopped, and which children are still running if the bound
   ran out.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import structlog

from agentos.session.keys import canonicalize_session_key
from agentos.session.models import SessionStatus

log = structlog.get_logger(__name__)

#: How long an abort waits for every stopped run to reach a persisted terminal.
ABORT_WAIT_TIMEOUT_S = 10.0
#: Cancel source recorded on child runs stopped because their parent was.
PARENT_ABORT_SOURCE = "parent_abort"
_MAX_DESCENDANT_DEPTH = 4
_MAX_DESCENDANTS = 64
_CHILD_PAGE_SIZE = 100


@dataclass
class AbortOutcome:
    """What an abort stopped."""

    #: Run (task) ids of the aborted session itself.
    aborted: list[str] = field(default_factory=list)
    #: Child session keys whose runs were stopped.
    children_aborted: list[str] = field(default_factory=list)
    #: Session keys (the aborted one or a child) still running when the wait
    #: bound ran out.
    still_running: list[str] = field(default_factory=list)
    #: Pending approval ids withdrawn because their run was stopped.
    approvals_cancelled: list[str] = field(default_factory=list)


def _cancel_pending_approvals(session_keys: list[str]) -> list[str]:
    """Withdraw the prompts the stopped runs were waiting on.

    Nothing will consume their answer, and with no poll left to notice, the
    prompt would stay on every console until its lifespan ran out.
    """
    try:
        from agentos.application.approval_queue import get_approval_queue

        queue = get_approval_queue()
    except Exception:
        return []
    cancelled: list[str] = []
    for key in session_keys:
        try:
            cancelled.extend(queue.cancel_pending_for_session(key, resolved_by="abort"))
        except Exception:
            log.warning("sessions.abort.approval_cancel_failed", session_key=key, exc_info=True)
    return cancelled


def _acp_manager() -> Any:
    try:
        from agentos.acp.manager import get_acp_manager
    except Exception:  # pragma: no cover - the acp package ships with the engine
        return None
    return get_acp_manager()


def _acp_turn_active(manager: Any, session_key: str) -> bool:
    probe = getattr(manager, "is_turn_active", None)
    if not callable(probe):
        return False
    try:
        return bool(probe(session_key))
    except Exception:
        return False


async def _spawned_children(session_manager: Any, parent_key: str) -> list[str]:
    """Running child session rows recorded as spawned by ``parent_key``."""
    storage = getattr(session_manager, "_storage", None) or session_manager
    list_sessions = getattr(storage, "list_sessions", None)
    if not callable(list_sessions):
        return []
    keys: list[str] = []
    offset = 0
    while len(keys) < _MAX_DESCENDANTS:
        try:
            batch = await list_sessions(
                status=str(SessionStatus.RUNNING),
                spawned_by=parent_key,
                limit=_CHILD_PAGE_SIZE,
                offset=offset,
            )
        except TypeError:
            return keys
        except Exception:
            log.debug("sessions.abort.child_rows_failed", parent=parent_key, exc_info=True)
            return keys
        for row in batch or []:
            key = getattr(row, "session_key", None)
            if isinstance(key, str) and key and key not in keys:
                keys.append(key)
        if not batch or len(batch) < _CHILD_PAGE_SIZE:
            break
        offset += _CHILD_PAGE_SIZE
    return keys


async def find_live_descendants(
    task_runtime: Any,
    session_manager: Any,
    session_key: str,
) -> list[str]:
    """Child sessions (any depth) of ``session_key`` that have a live run.

    Two sources, because neither sees every child: the runtime knows the
    queued/running child tasks it was handed with a ``parent_session_key``;
    the session rows know children whose current turn the user typed into the
    child directly (an ACP harness session), which carries no parent link.
    """
    acp = _acp_manager()
    found: list[str] = []
    frontier = [canonicalize_session_key(session_key)]
    seen = set(frontier)
    for _depth in range(_MAX_DESCENDANT_DEPTH):
        next_frontier: list[str] = []
        for parent in frontier:
            candidates: list[str] = []
            child_keys = getattr(task_runtime, "active_child_session_keys", None)
            if callable(child_keys):
                candidates.extend(child_keys(parent))
            for key in await _spawned_children(session_manager, parent):
                if key not in candidates:
                    candidates.append(key)
            for child in candidates:
                child = canonicalize_session_key(child)
                if child in seen:
                    continue
                seen.add(child)
                live = bool(task_runtime.active_task_ids(child)) or _acp_turn_active(acp, child)
                if not live:
                    continue
                found.append(child)
                next_frontier.append(child)
                if len(found) >= _MAX_DESCENDANTS:
                    return found
        if not next_frontier:
            break
        frontier = next_frontier
    return found


async def abort_session_runs(
    task_runtime: Any,
    session_key: str,
    *,
    session_manager: Any,
    source: str,
    reason: str = "user_abort",
    wait_timeout_s: float | None = None,
) -> AbortOutcome:
    """Abort ``session_key``'s runs and its descendants' runs; see the module doc."""
    session_key = canonicalize_session_key(session_key)
    publications = task_runtime.terminal_publications
    outcome = AbortOutcome()

    own_runs = list(task_runtime.active_task_ids(session_key))
    children = await find_live_descendants(task_runtime, session_manager, session_key)
    child_runs: dict[str, list[str]] = {
        child: list(task_runtime.active_task_ids(child)) for child in children
    }

    for run_id in own_runs:
        publications.hold(run_id, by="abort")
    for runs in child_runs.values():
        for run_id in runs:
            publications.hold(run_id, by="abort")

    await task_runtime.cancel(session_key=session_key, source=source, reason=reason)
    acp = _acp_manager()
    # Deepest first: a child stopped before its own children could otherwise
    # spawn or wake one of them on the way down.
    for child in reversed(children):
        if acp is not None and _acp_turn_active(acp, child):
            note_stop = getattr(acp, "note_stop", None)
            if callable(note_stop):
                note_stop(child, "user")
        await task_runtime.cancel(
            session_key=child, source=PARENT_ABORT_SOURCE, reason=PARENT_ABORT_SOURCE
        )
        # A runtime-driven harness turn stops with its task (the dispatch
        # cancels the harness on the way out); only a harness turn with no
        # task behind it needs the manager's own cancel.
        if not child_runs.get(child) and acp is not None and _acp_turn_active(acp, child):
            try:
                await acp.cancel(child, reason=PARENT_ABORT_SOURCE)
            except Exception:
                log.warning("sessions.abort.acp_cancel_failed", child=child, exc_info=True)
    # After the cancels: a waiter woken by the denial must not get to act on it
    # before its cancellation lands.
    outcome.approvals_cancelled = _cancel_pending_approvals([session_key, *children])

    timeout = ABORT_WAIT_TIMEOUT_S if wait_timeout_s is None else max(0.0, wait_timeout_s)
    deadline = time.monotonic() + timeout

    async def _settle(run_id: str) -> bool:
        remaining = deadline - time.monotonic()
        if not task_runtime.is_task_active(run_id):
            return True
        if remaining <= 0:
            return False
        try:
            await task_runtime.wait(run_id, timeout=remaining)
        except TimeoutError:
            return False
        except KeyError:
            return True
        return not task_runtime.is_task_active(run_id)

    for run_id in own_runs:
        if await _settle(run_id):
            outcome.aborted.append(run_id)
        else:
            log.warning("sessions.abort.run_still_active", session_key=session_key, run=run_id)
            if session_key not in outcome.still_running:
                outcome.still_running.append(session_key)
    for child in children:
        settled = True
        for run_id in child_runs.get(child, []):
            settled = await _settle(run_id) and settled
        if settled and acp is not None and _acp_turn_active(acp, child):
            # A harness turn with no runtime task (or one that outlived it).
            remaining = deadline - time.monotonic()
            while remaining > 0 and _acp_turn_active(acp, child):
                await asyncio.sleep(min(0.05, remaining))
                remaining = deadline - time.monotonic()
            settled = not _acp_turn_active(acp, child)
        if settled:
            outcome.children_aborted.append(child)
        else:
            outcome.still_running.append(child)
    return outcome
