"""One-time import of the existing SQLite job history into Neon PostgreSQL.

Run from the project root after setting DATABASE_URL in .env. The local SQLite
file is read only and is not deleted. Existing Neon rows are not overwritten.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from tools.database import connect_db, execute, using_postgres  # noqa: E402
from tools.job_tracker import init_db  # noqa: E402

SOURCE_DB = Path(os.getenv("SQLITE_DB_PATH", "logs/applications.db"))
COLUMNS = (
    "timestamp", "job_title", "company", "job_url", "match_score",
    "apply_method", "status", "feedback", "cover_letter",
    "missing_skills", "matched_skills", "job_hash",
)


def main() -> int:
    if not using_postgres():
        print("ERROR: DATABASE_URL must point to Neon PostgreSQL. No migration performed.", file=sys.stderr)
        return 2
    if not SOURCE_DB.is_file():
        print(f"ERROR: SQLite database not found: {SOURCE_DB}", file=sys.stderr)
        return 2

    source = sqlite3.connect(str(SOURCE_DB))
    source.row_factory = sqlite3.Row
    try:
        records = source.execute(
            f"SELECT {', '.join(COLUMNS)} FROM applications ORDER BY id"
        ).fetchall()
    finally:
        source.close()

    init_db()
    target = connect_db()
    inserted = 0
    try:
        placeholders = ", ".join("?" for _ in COLUMNS)
        sql = (
            f"INSERT INTO applications ({', '.join(COLUMNS)}) "
            f"VALUES ({placeholders}) ON CONFLICT (job_hash) DO NOTHING"
        )
        for record in records:
            cursor = execute(target, sql, tuple(record[column] for column in COLUMNS))
            inserted += max(cursor.rowcount, 0)
        target.commit()
    except Exception:
        target.rollback()
        raise
    finally:
        target.close()

    print(f"SQLite records found: {len(records)}")
    print(f"New records inserted into Neon: {inserted}")
    print(f"Existing records skipped (duplicate job hashes): {len(records) - inserted}")
    print("Migration finished. Your SQLite file was left unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
