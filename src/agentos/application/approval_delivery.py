"""Where an approval goes once the queue has persisted it.

The queue (``application.approval_queue``) calls :func:`on_requested` after a
row is written and :func:`on_resolved` after one is resolved -- by a human, a
timeout, the expiry sweep or an elevation. Listeners registered here do the
delivery (Web Push today); they run synchronously inside the queue's write
path, so they must hand anything slow off to their own task and never raise.

The queue also asks :func:`audience_for` which sessions should see a request:
the session that asked plus every ancestor in its subagent / coding-agent
lineage, so a parent session's viewers hear about a child's prompt. The
resolver is registered by the gateway, which knows where sessions live; the
application layer stays free of gateway imports.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from agentos.application.approval_queue import PendingApproval

log = logging.getLogger(__name__)

#: ``resolver(session_key) -> ancestors`` nearest first, the key itself excluded.
LineageResolver = Callable[[str], Sequence[str]]

#: Bound on the lineage walk: a cycle or a runaway spawn chain stops here.
MAX_LINEAGE_DEPTH = 8


class ApprovalListener(Protocol):
    def on_requested(self, entry: PendingApproval) -> None: ...

    def on_resolved(self, entry: PendingApproval) -> None: ...


_listeners: list[ApprovalListener] = []
_lineage_resolver: LineageResolver | None = None
_lock = threading.Lock()


def register_listener(listener: ApprovalListener) -> None:
    with _lock:
        if listener not in _listeners:
            _listeners.append(listener)


def unregister_listener(listener: ApprovalListener) -> None:
    with _lock:
        if listener in _listeners:
            _listeners.remove(listener)


def set_lineage_resolver(resolver: LineageResolver | None) -> None:
    global _lineage_resolver
    _lineage_resolver = resolver


def _key_shape_parent(session_key: str) -> str | None:
    """The parent a legacy ``subagent:<parent key>`` key names in itself."""
    if session_key.startswith("subagent:"):
        parent = session_key[len("subagent:") :]
        return parent or None
    return None


def audience_for(session_key: str | None) -> tuple[str, ...]:
    """``session_key`` followed by its ancestors, deduplicated, bounded."""
    key = str(session_key or "").strip()
    if not key:
        return ()
    audience = [key]
    resolver = _lineage_resolver
    current = key
    for _ in range(MAX_LINEAGE_DEPTH):
        parents: Sequence[str] = ()
        if resolver is not None:
            try:
                parents = resolver(current)
            except Exception:  # noqa: BLE001 - a lineage fault must not block a prompt
                log.debug("approval_delivery.lineage_failed", exc_info=True)
                parents = ()
        parent = next((p for p in parents if p), None) or _key_shape_parent(current)
        if not parent or parent in audience:
            break
        audience.append(parent)
        current = parent
    return tuple(audience)


def _fan_out(method: str, entry: PendingApproval) -> None:
    with _lock:
        listeners = list(_listeners)
    for listener in listeners:
        try:
            getattr(listener, method)(entry)
        except Exception:  # noqa: BLE001 - delivery never breaks the queue write
            log.warning("approval_delivery.listener_failed method=%s", method, exc_info=True)


def on_requested(entry: PendingApproval) -> None:
    _fan_out("on_requested", entry)


def on_resolved(entry: PendingApproval) -> None:
    _fan_out("on_resolved", entry)


def reset() -> None:
    """Drop every listener and the resolver (tests, gateway shutdown)."""
    with _lock:
        _listeners.clear()
    set_lineage_resolver(None)


__all__ = [
    "MAX_LINEAGE_DEPTH",
    "ApprovalListener",
    "LineageResolver",
    "audience_for",
    "on_requested",
    "on_resolved",
    "register_listener",
    "reset",
    "set_lineage_resolver",
    "unregister_listener",
]
