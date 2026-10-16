"""Database adapter: Neon PostgreSQL when DATABASE_URL is set, SQLite otherwise."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any


def using_postgres() -> bool:
    return bool(os.getenv("DATABASE_URL", "").strip())


def connect_db():
    """Open a fresh DB connection. DATABASE_URL should be a Neon Postgres URL."""
    database_url = os.getenv("DATABASE_URL", "").strip()
    if database_url:
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - error path
            raise RuntimeError(
                "DATABASE_URL is set, but psycopg is not installed. "
                "Install the project requirements first."
            ) from exc

        return psycopg.connect(database_url, connect_timeout=20)

    db_path = Path(os.getenv("SQLITE_DB_PATH", "logs/applications.db"))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(str(db_path), timeout=30)


def execute(conn, sql: str, params: tuple | list = ()):
    """Execute SQL written with '?' placeholders on either supported database."""
    if using_postgres():
        sql = sql.replace("?", "%s")
    return conn.execute(sql, params)
