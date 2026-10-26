"""Synchronous, read-only lookups of session lineage and delivery routing.

The approval queue writes synchronously and channel callbacks need an answer
before they touch the queue, so both read the sessions table directly through
a read-only SQLite connection rather than the async session manager. Only two
facts are read: who spawned a session (subagent / coding-agent lineage) and
where it was last delivered (channel account + chat).
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SessionRoute:
    """Where a session was last delivered (``SessionNode.last_*``)."""

    channel: str = ""
    to: str = ""
    account_id: str = ""
    delivery_context: dict[str, Any] = field(default_factory=dict)


class SessionIndex:
    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def _connection(self) -> sqlite3.Connection | None:
        if self._conn is not None:
            return self._conn
        if not self._path.exists():
            return None
        try:
            conn = sqlite3.connect(
                f"file:{self._path}?mode=ro",
                uri=True,
                check_same_thread=False,
                timeout=1.0,
            )
        except sqlite3.Error:
            log.debug("session_index.open_failed", path=str(self._path), exc_info=True)
            return None
        conn.row_factory = sqlite3.Row
        self._conn = conn
        return conn

    def _row(self, session_key: str, columns: str) -> sqlite3.Row | None:
        with self._lock:
            conn = self._connection()
            if conn is None:
                return None
            try:
                row: sqlite3.Row | None = conn.execute(
                    f"SELECT {columns} FROM sessions WHERE session_key = ?",  # noqa: S608
                    (session_key,),
                ).fetchone()
                return row
            except sqlite3.Error:
                log.debug("session_index.query_failed", exc_info=True)
                return None

    def parents(self, session_key: str) -> list[str]:
        """The session that spawned ``session_key``, if it is a child."""
        row = self._row(session_key, "parent_session_key, spawned_by")
        if row is None:
            return []
        parent = row["parent_session_key"] or row["spawned_by"]
        return [str(parent)] if parent else []

    def route(self, session_key: str) -> SessionRoute | None:
        row = self._row(session_key, "last_channel, last_to, last_account_id, delivery_context")
        if row is None:
            return None
        raw_context = row["delivery_context"]
        context: dict[str, Any] = {}
        if raw_context:
            import json

            try:
                parsed = json.loads(raw_context)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, dict):
                context = parsed
        return SessionRoute(
            channel=str(row["last_channel"] or ""),
            to=str(row["last_to"] or ""),
            account_id=str(row["last_account_id"] or ""),
            delivery_context=context,
        )

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None


_index: SessionIndex | None = None


def set_session_index(index: SessionIndex | None) -> None:
    global _index
    previous, _index = _index, index
    if previous is not None and previous is not index:
        previous.close()


def get_session_index() -> SessionIndex | None:
    return _index


__all__ = ["SessionIndex", "SessionRoute", "get_session_index", "set_session_index"]
