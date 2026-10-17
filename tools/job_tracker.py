"""Persistence for evaluated jobs and the GitHub Actions schedule guard."""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from tools.database import connect_db, execute, using_postgres

_SCHEMA_INITIALIZED = False


def init_db() -> None:
    """Create the application table and the one-row schedule-control table."""
    global _SCHEMA_INITIALIZED
    if _SCHEMA_INITIALIZED:
        return

    conn = connect_db()
    try:
        if using_postgres():
            execute(conn, """
                CREATE TABLE IF NOT EXISTS applications (
                    id BIGSERIAL PRIMARY KEY,
                    timestamp TEXT,
                    job_title TEXT,
                    company TEXT,
                    job_url TEXT,
                    match_score INTEGER,
                    apply_method TEXT,
                    status TEXT,
                    feedback TEXT,
                    cover_letter TEXT,
                    missing_skills TEXT,
                    matched_skills TEXT,
                    job_hash TEXT UNIQUE
                )
            """)
            execute(conn, """
                CREATE TABLE IF NOT EXISTS agent_run_control (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    last_started_at TIMESTAMPTZ NULL
                )
            """)
        else:
            execute(conn, """
                CREATE TABLE IF NOT EXISTS applications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    job_title TEXT,
                    company TEXT,
                    job_url TEXT,
                    match_score INTEGER,
                    apply_method TEXT,
                    status TEXT,
                    feedback TEXT,
                    cover_letter TEXT,
                    missing_skills TEXT,
                    matched_skills TEXT,
                    job_hash TEXT UNIQUE
                )
            """)
            execute(conn, """
                CREATE TABLE IF NOT EXISTS agent_run_control (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    last_started_at TEXT NULL
                )
            """)
        conn.commit()
        _SCHEMA_INITIALIZED = True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_job_hash(job_title: str, company: str) -> str:
    """Stable hash used to deduplicate the same job across search queries."""
    unique = f"{job_title.lower().strip()}{company.lower().strip()}"
    return hashlib.md5(unique.encode("utf-8")).hexdigest()


def is_already_processed(job_title: str, company: str) -> bool:
    init_db()
    conn = connect_db()
    try:
        job_hash = get_job_hash(job_title, company)
        row = execute(
            conn,
            "SELECT id FROM applications WHERE job_hash = ?",
            (job_hash,),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def mark_job_processed(job_title: str, company: str) -> None:
    """Insert a minimal record after a successful evaluation; retain existing rows."""
    init_db()
    conn = connect_db()
    job_hash = get_job_hash(job_title, company)
    try:
        if using_postgres():
            execute(conn, """
                INSERT INTO applications (job_title, company, job_hash, timestamp)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (job_hash) DO NOTHING
            """, (job_title, company, job_hash, datetime.now(timezone.utc).isoformat()))
        else:
            execute(conn, """
                INSERT OR IGNORE INTO applications (job_title, company, job_hash, timestamp)
                VALUES (?, ?, ?, ?)
            """, (job_title, company, job_hash, datetime.now(timezone.utc).isoformat()))
        conn.commit()
    except Exception as exc:
        conn.rollback()
        print(f"   ⚠️ Tracker error: {exc}")
        raise
    finally:
        conn.close()


def try_start_scheduled_run(interval_hours: int = 4, force: bool = False) -> bool:
    """Atomically claim a run if the previous start was at least N hours ago."""
    init_db()
    now = datetime.now(timezone.utc)
    conn = connect_db()
    try:
        if using_postgres():
            if force:
                row = execute(conn, """
                    INSERT INTO agent_run_control (id, last_started_at)
                    VALUES (1, ?)
                    ON CONFLICT (id) DO UPDATE SET last_started_at = EXCLUDED.last_started_at
                    RETURNING id
                """, (now,)).fetchone()
            else:
                row = execute(conn, """
                    INSERT INTO agent_run_control (id, last_started_at)
                    VALUES (1, ?)
                    ON CONFLICT (id) DO UPDATE
                    SET last_started_at = EXCLUDED.last_started_at
                    WHERE agent_run_control.last_started_at IS NULL
                       OR agent_run_control.last_started_at <= EXCLUDED.last_started_at - (? * INTERVAL '1 hour')
                    RETURNING id
                """, (now, interval_hours)).fetchone()
            conn.commit()
            return row is not None

        # BEGIN IMMEDIATE makes the read/check/write atomic for SQLite callers.
        execute(conn, "BEGIN IMMEDIATE")
        row = execute(
            conn,
            "SELECT last_started_at FROM agent_run_control WHERE id = 1",
        ).fetchone()
        previous = None
        if row and row[0]:
            previous = datetime.fromisoformat(row[0])
            if previous.tzinfo is None:
                previous = previous.replace(tzinfo=timezone.utc)

        due = force or previous is None or (now - previous >= timedelta(hours=interval_hours))
        if due:
            execute(conn, """
                INSERT INTO agent_run_control (id, last_started_at) VALUES (1, ?)
                ON CONFLICT (id) DO UPDATE SET last_started_at = excluded.last_started_at
            """, (now.isoformat(),))
        conn.commit()
        return due
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def reset_scheduled_run_after_failure() -> None:
    """Allow the next scheduled GitHub check to retry after a failed workflow run."""
    init_db()
    conn = connect_db()
    try:
        execute(conn, "UPDATE agent_run_control SET last_started_at = NULL WHERE id = 1")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
