"""V013 - scheduler job concurrency policy.

Adds the per-job overlap policy (``forbid`` | ``replace`` | ``allow``) to
cron jobs. JobStore also performs an idempotent in-process migration for
its own database (``scheduler.db``); this yoyo migration keeps a legacy
``scheduler_jobs`` table in the managed database in step, like V008.
"""

from __future__ import annotations

from yoyo import step

__depends__: set[str] = {"V012__projects_name_unique"}

TABLE = "scheduler_jobs"
COLUMN = "concurrency"
DDL = "TEXT NOT NULL DEFAULT 'forbid'"


def _table_exists(conn, table: str) -> bool:
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    return cur.fetchone() is not None


def _has_column(conn, table: str, column: str) -> bool:
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())


def apply_step(conn) -> None:
    if not _table_exists(conn, TABLE):
        return
    if not _has_column(conn, TABLE, COLUMN):
        conn.cursor().execute(f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN} {DDL}")


def rollback_step(conn) -> None:
    if not _table_exists(conn, TABLE):
        return
    if _has_column(conn, TABLE, COLUMN):
        conn.cursor().execute(f"ALTER TABLE {TABLE} DROP COLUMN {COLUMN}")


steps = [step(apply_step, rollback_step)]
