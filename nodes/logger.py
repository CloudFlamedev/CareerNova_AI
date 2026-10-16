from datetime import datetime, timezone

from tools.database import connect_db, execute
from tools.job_tracker import init_db, get_job_hash


async def logger_node(state: dict) -> dict:
    """Save completed evaluations; leave transient LLM failures retryable."""
    if state.get("llm_failed"):
        print("   ↩️  Not recording this job as processed; it will be retried on a later scan.")
        return state

    init_db()
    missing = ", ".join(state.get("missing_skills", []))
    matched = ", ".join(state.get("matched_skills", []))
    job_hash = get_job_hash(state.get("job_title", ""), state.get("company_name", ""))
    conn = connect_db()
    try:
        execute(conn, """
            INSERT INTO applications
                (timestamp, job_title, company, job_url, match_score,
                 apply_method, status, feedback, cover_letter,
                 missing_skills, matched_skills, job_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (job_hash) DO UPDATE SET
                timestamp      = excluded.timestamp,
                job_url        = excluded.job_url,
                match_score    = excluded.match_score,
                apply_method   = excluded.apply_method,
                status         = excluded.status,
                feedback       = excluded.feedback,
                cover_letter   = excluded.cover_letter,
                missing_skills = excluded.missing_skills,
                matched_skills = excluded.matched_skills
        """, (
            datetime.now(timezone.utc).isoformat(),
            state.get("job_title", ""),
            state.get("company_name", ""),
            state.get("job_url", ""),
            state.get("match_score", 0),
            state.get("apply_method", ""),
            state.get("application_status", ""),
            state.get("feedback", ""),
            state.get("cover_letter", ""),
            missing,
            matched,
            job_hash,
        ))
        conn.commit()
    except Exception as exc:
        conn.rollback()
        print(f"   ⚠️ Logger error: {exc}")
        raise
    finally:
        conn.close()

    print(f"\n💾 Logged: {state.get('job_title')} @ {state.get('company_name')}")
    print(f"   Status:  {state.get('application_status')}")
    print(f"   Score:   {state.get('match_score')}/100")
    print(f"   Missing: {missing[:80]}")
    return state
