"""V014 - gateway boot ledger for the crash-loop breaker.

One row per ``agentos gateway run`` boot: when it started, when and how it
ended. ``clean`` is NULL while the boot runs, 1 after a graceful shutdown, 0
for a boot that ended without one (the next boot marks those). ``counted``
says whether an unclean row counts toward safe mode: an operator override
and a boot that failed before serving do not.

``agentos.gateway.boot_lifecycle`` also creates the table on its own
(``CREATE TABLE IF NOT EXISTS``, same DDL): a boot is recorded before
migrations run, so a crash inside the migration step still counts.
"""

from __future__ import annotations

from yoyo import step

__depends__: set[str] = {"V013__scheduler_job_concurrency"}

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS gateway_boots (
    id TEXT PRIMARY KEY,
    pid INTEGER NOT NULL,
    started_at REAL NOT NULL,
    ended_at REAL,
    clean INTEGER,
    reason TEXT NOT NULL DEFAULT '',
    counted INTEGER NOT NULL DEFAULT 1
)
"""
CREATE_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_gateway_boots_started_at ON gateway_boots(started_at)"
)


def apply_step(conn) -> None:
    cur = conn.cursor()
    cur.execute(CREATE_TABLE)
    cur.execute(CREATE_INDEX)


def rollback_step(conn) -> None:
    cur = conn.cursor()
    cur.execute("DROP INDEX IF EXISTS idx_gateway_boots_started_at")
    cur.execute("DROP TABLE IF EXISTS gateway_boots")


steps = [step(apply_step, rollback_step)]
