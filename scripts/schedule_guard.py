"""GitHub Actions gate; claims a run only when the 4-hour interval is due."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Running this file directly sets sys.path[0] to scripts/, so add project root.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.job_tracker import reset_scheduled_run_after_failure, try_start_scheduled_run


def write_github_output(should_run: bool) -> None:
    output_path = os.getenv("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as output_file:
            output_file.write(f"run={'true' if should_run else 'false'}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="Claim a run even if 4 hours have not elapsed.")
    parser.add_argument("--reset", action="store_true", help="Clear the claim after a failed workflow.")
    args = parser.parse_args()

    if not os.getenv("DATABASE_URL", "").strip():
        raise SystemExit(
            "DATABASE_URL is missing. Add your Neon PostgreSQL connection string "
            "as a GitHub Actions secret named DATABASE_URL."
        )

    if args.reset:
        reset_scheduled_run_after_failure()
        print("Schedule claim cleared; the next scheduled check can retry.")
        write_github_output(False)
        return 0

    should_run = try_start_scheduled_run(interval_hours=4, force=args.force)
    if should_run:
        print("✅ Four-hour window elapsed (or run was forced); starting the agent.")
    else:
        print("⏭️ Last run started less than 4 hours ago; skipping this scheduled check.")
    write_github_output(should_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
