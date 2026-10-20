import argparse
import asyncio
import os
import re
from datetime import datetime, timedelta
from jobspy import scrape_jobs
from tools.resume_parser import parse_resume
from tools.telegram_notifier import send_job_alert, send_daily_summary
from tools.job_tracker import (
    is_already_processed,
    mark_job_processed,
    try_start_scheduled_run,
    reset_scheduled_run_after_failure,
)
from graph import app
from dotenv import load_dotenv

load_dotenv()

# ── Config ─────────────────────────────────────────────────

MIN_MATCH_SCORE    = 75
MIN_FEEDBACK_SCORE  = 50  # Keep in sync with graph.py
# Keep each search smaller to reduce the total runtime in GitHub Actions.
RESULTS_PER_SEARCH = 15
MAX_RETRIES        = 3
# Conservative delays: faster than the original 30s/job while allowing time
# between Groq requests to reduce the likelihood of rate-limit errors.
JOB_DELAY_SECONDS    = 20
SEARCH_DELAY_SECONDS = 3

# Search only India-based openings and remote opportunities.
# The India query covers roles across India; Remote can surface remote roles
# worldwide. We also validate results from the Remote query below to avoid
# accepting ordinary onsite jobs outside India.
LOCATIONS = ["India", "Remote"]


def is_remote_job(job) -> bool:
    """Return True only when the scraped fields identify the role as remote."""
    remote_flag = job.get("is_remote", False)
    if isinstance(remote_flag, bool) and remote_flag:
        return True
    if str(remote_flag).strip().lower() in {"true", "1", "yes", "y"}:
        return True

    # Use structured/short fields rather than the full description, since a
    # description can mention remote work without the job itself being remote.
    fields = (
        job.get("location", ""),
        job.get("workplace_type", ""),
        job.get("title", ""),
    )
    text = " ".join(str(value) for value in fields if value is not None).lower()
    remote_markers = (
        "remote", "work from anywhere", "anywhere in the world", "worldwide remote"
    )
    return any(marker in text for marker in remote_markers)


def is_india_based_job(job) -> bool:
    """Identify roles explicitly listed in India, including common Indian cities."""
    fields = (
        job.get("location", ""),
        job.get("country", ""),
        job.get("state", ""),
        job.get("city", ""),
    )
    text = " ".join(str(value) for value in fields if value is not None).lower()
    if "india" in text:
        return True

    # Some LinkedIn results list only a city/state instead of the country.
    indian_places = (
        "bengaluru", "bangalore", "hyderabad", "pune", "mumbai", "delhi",
        "new delhi", "noida", "gurugram", "gurgaon", "chennai", "kolkata",
        "ahmedabad", "jaipur", "kochi", "cochin", "chandigarh", "indore",
        "bhopal", "lucknow", "kanpur", "nagpur", "surat", "vadodara",
        "visakhapatnam", "vizag", "coimbatore", "thiruvananthapuram",
        "trivandrum", "mohali", "greater noida", "navi mumbai",
        "uttar pradesh", "maharashtra", "karnataka", "telangana", "tamil nadu",
        "west bengal", "gujarat", "rajasthan", "kerala", "punjab", "haryana",
        "madhya pradesh", "andhra pradesh", "odisha", "bihar", "jharkhand",
        "assam", "uttarakhand", "himachal pradesh",
    )
    return any(place in text for place in indian_places)


def is_internship_job(job) -> bool:
    """Return True when title, structured type, or clear description text marks an internship."""
    def clean(value) -> str:
        if value is None:
            return ""
        text = str(value).strip().lower()
        return "" if text in {"", "nan", "none", "null", "<na>"} else text

    title = clean(job.get("title", ""))
    if re.search(r"\b(internship|intern)\b", title):
        return True

    # Prefer structured fields when the scraper provides employment type.
    employment_type = " ".join(
        clean(job.get(field, ""))
        for field in ("job_type", "employment_type", "employmentType")
    )
    if re.search(r"\b(internship|intern)\b", employment_type):
        return True

    # Description fallback catches cases where the title omits the role type,
    # while avoiding a blanket search for the word "intern" anywhere in prose.
    description = clean(job.get("description", ""))
    description_patterns = (
        r"\bthis (?:is|will be) an? internship\b",
        r"\b(?:an? )?internship (?:position|role|opportunity|program|programme|opening)\b",
        r"\b(?:summer|winter|spring|fall) intern(?:ship)?\b",
        r"\b(?:job type|employment type)\s*[:\-]\s*internship\b",
        r"\babout the internship\b",
        r"\b(?:seeking|looking for|hiring|recruiting)\s+(?:an?\s+)?interns?\b",
    )
    return any(re.search(pattern, description) for pattern in description_patterns)

# Candidate experience — used by the evaluator to gate jobs.
# A job is only skipped if it asks for MORE than the acceptable max.
CANDIDATE_YEARS_EXPERIENCE     = 3
MAX_ACCEPTABLE_EXPERIENCE_YEARS = 5

JOB_SEARCH_TERMS = [
    # Explicitly junior/mid level
    "Python Developer 3 years experience",
    "Associate Python Developer",
    "Junior Python Developer",
    "Mid Level Python Developer",
    # Core Python roles
    "Python Developer",
    "Python Backend Engineer",
    "Backend Developer Python",
    # Gen AI
    "Gen AI Engineer",
    "Generative AI Engineer",
    "Junior Gen AI Engineer",
    # AI Engineer
    "AI Engineer",
    "Junior AI Engineer",
    "Machine Learning Engineer Python",
    # Agentic AI
    "Agentic AI Engineer",
    "AI Agent Engineer",
    "LLM Engineer",
]

# ── Date filter ────────────────────────────────────────────

def is_posted_recently(job, hours: int = 12) -> bool:
    """Returns True if job was posted within the last X hours."""
    try:
        date_posted = job.get("date_posted")
        if date_posted is None:
            return True

        if hasattr(date_posted, 'hour'):
            posted_dt = date_posted
        else:
            posted_dt = datetime.combine(date_posted, datetime.min.time())

        cutoff = datetime.now() - timedelta(hours=hours)
        return posted_dt >= cutoff

    except Exception:
        return True


# ── Scraper with retry logic ───────────────────────────────

async def scrape_with_retry(search_term: str, location: str) -> object:
    """
    Scrapes LinkedIn jobs with simple retry logic.
    LinkedIn only — Indeed/Glassdoor unreliable outside a few markets.
    Waits 10s → 20s → 30s between retries.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            print(f"   🔄 Attempt {attempt}/{MAX_RETRIES}...")

            jobs = scrape_jobs(
                site_name=["linkedin"],
                search_term=search_term,
                location=location,
                results_wanted=RESULTS_PER_SEARCH,
                hours_old=12,
                linkedin_fetch_description=True,
            )

            if jobs is not None and not jobs.empty:
                print(f"   ✅ Found {len(jobs)} jobs")
                return jobs
            else:
                print(f"   ⚠️  No results on attempt {attempt}")

        except Exception as e:
            print(f"   ❌ Attempt {attempt} failed: {str(e)[:80]}")

        if attempt < MAX_RETRIES:
            wait_time = attempt * 10
            print(f"   ⏳ Waiting {wait_time}s before retry...")
            await asyncio.sleep(wait_time)

    print(f"   ❌ All {MAX_RETRIES} attempts failed for: {search_term} ({location})")
    return None


# ── Main agent run ─────────────────────────────────────────

async def run_agent():
    """Runs the full job agent pipeline."""

    total_combos = len(JOB_SEARCH_TERMS) * len(LOCATIONS)

    print(f"\n{'='*55}")
    print(f"🤖 LinkedIn Job Agent Started")
    print(f"⏰ Time: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"📊 Telegram alert score: {MIN_MATCH_SCORE}+")
    print(f"🪙 Feedback score range: {MIN_FEEDBACK_SCORE}–{MIN_MATCH_SCORE - 1}; lower scores skip feedback")
    print("✉️ Cover-letter generation: DISABLED")
    print(f"📈 Experience gate:   candidate {CANDIDATE_YEARS_EXPERIENCE} yrs, accept up to {MAX_ACCEPTABLE_EXPERIENCE_YEARS} yrs required")
    print(f"🔍 Search terms:      {len(JOB_SEARCH_TERMS)}")
    print(f"🌍 Locations:         {len(LOCATIONS)} ({', '.join(LOCATIONS)})")
    print(f"🔁 Total searches:    {total_combos} (terms × locations)")
    print(f"📦 Results per search: {RESULTS_PER_SEARCH}")
    print(f"⏱️ Job delay:          {JOB_DELAY_SECONDS}s; search delay: {SEARCH_DELAY_SECONDS}s")
    print(f"{'='*55}")

    # Parse resume once
    resume = parse_resume("data/resume.pdf")

    total_jobs     = 0
    filtered_jobs  = 0
    high_matches   = 0
    low_matches    = 0
    skipped_dupes  = 0
    skipped_exp    = 0
    failed_scrapes = 0
    skipped_empty_desc = 0
    skipped_internships = 0

    for location in LOCATIONS:
        for search_term in JOB_SEARCH_TERMS:
            print(f"\n🔍 Searching: {search_term} in {location}")

            jobs = await scrape_with_retry(search_term, location)

            if jobs is None or jobs.empty:
                failed_scrapes += 1
                print(f"   ⚠️  Skipping — no results after {MAX_RETRIES} attempts")
                continue

            print(f"   ✅ Found {len(jobs)} jobs — applying filters...")

            for _, job in jobs.iterrows():
                total_jobs += 1

                title       = str(job.get("title", ""))
                company     = str(job.get("company", ""))
                description = str(job.get("description", ""))
                job_url     = str(job.get("job_url", ""))

                # ── Filter 1: Skip internship roles before evaluation ──
                if is_internship_job(job):
                    skipped_internships += 1
                    print(f"   ⏭️  Skipping (internship role): {title}")
                    continue

                # ── Filter 2: Restrict results to India or remote roles ──
                # The India search is country-scoped by LinkedIn. The Remote
                # search is broader, so require an explicit remote signal or an
                # India location before evaluating the job.
                if location == "Remote" and not (
                    is_remote_job(job) or is_india_based_job(job)
                ):
                    print(f"   ⏭️  Skipping (not confirmed India/remote): {title}")
                    continue

                # ── Filter 3: Skip already processed jobs ──
                if is_already_processed(title, company):
                    skipped_dupes += 1
                    print(f"   ⏭️  Already processed: {title} @ {company}")
                    continue

                # ── Filter 4: Posted in last 12 hours ──
                if not is_posted_recently(job, hours=12):
                    print(f"   ⏭️  Skipping (too old): {title}")
                    continue

                # ── Filter 5: Description must actually be present ──
                if len(description.strip()) < 50:
                    skipped_empty_desc += 1
                    print(f"   ⏭️  Skipping (no description scraped): {title}")
                    continue

                filtered_jobs += 1
                print(f"\n{'─'*50}")
                print(f"   📋 {title} @ {company} ({location})")

                # ── Build state — include ALL required fields ──
                state = {
                    "job_url":                      job_url,
                    "job_title":                     title,
                    "company_name":                  company,
                    "job_description":               description,
                    "required_skills":               [],
                    "external_url":                  job_url,
                    "resume_text":                   resume["full_text"],
                    "resume_data":                   resume,
                    "resume_pdf_path":                "data/resume.pdf",
                    "match_score":                    None,
                    "gap_analysis":                   "",
                    "missing_skills":                 [],
                    "matched_skills":                 [],
                    "experience_match":               True,   # ← LLM will update this
                    "experience_required":            0,      # ← LLM will update this
                    "candidate_years_experience":      CANDIDATE_YEARS_EXPERIENCE,
                    "max_acceptable_experience_years": MAX_ACCEPTABLE_EXPERIENCE_YEARS,
                    # Legacy DB/state field retained as None; generation is disabled.
                    "cover_letter":                   None,
                    "apply_method":                   None,
                    "application_status":             None,
                    "feedback":                       None,
                    "llm_failed":                     False,
                    "llm_error":                      "",
                }

                # ── Run the graph ──
                try:
                    result = await app.ainvoke(state)

                    # Check failure flags before reading or comparing the score.
                    # Failed evaluations must remain eligible for a later retry.
                    if result.get("llm_failed"):
                        reason = result.get("llm_error", "Unknown LLM error")
                        print(f"   ↩️  LLM evaluation failed: {reason[:180]}")
                        print("   This job was not marked processed and can retry on a later scan.")
                        low_matches += 1
                        await asyncio.sleep(JOB_DELAY_SECONDS)
                        continue

                    score = result.get("match_score")
                    if not isinstance(score, (int, float)) or isinstance(score, bool):
                        print("   ⚠️ Invalid/missing match score; leaving job eligible for retry.")
                        low_matches += 1
                        await asyncio.sleep(JOB_DELAY_SECONDS)
                        continue

                    exp_match = result.get("experience_match", True)
                    exp_required = result.get("experience_required", 0)

                    # Mark only successfully evaluated jobs as processed.
                    mark_job_processed(title, company)

                    # ── Block 1: Experience mismatch → skip ──
                    if not exp_match:
                        skipped_exp += 1
                        print(f"   ⛔ Skipping alert — needs {exp_required} yrs (accepting up to {MAX_ACCEPTABLE_EXPERIENCE_YEARS})")
                        low_matches += 1
                        continue

                    # ── Block 2: Below alert threshold ──
                    if score < MIN_MATCH_SCORE:
                        low_matches += 1
                        if score < MIN_FEEDBACK_SCORE:
                            print(f"   🔴 Score too low ({score}) — feedback skipped to conserve tokens")
                        else:
                            print(f"   🟠 Score {score} — feedback attempted; skipping alert")
                        continue

                    # ── Block 3: Good match → send Telegram (no cover letter) ──
                    high_matches += 1
                    await send_job_alert(result)
                    print(f"   📱 Telegram alert sent!")

                except Exception as e:
                    print(f"   ❌ Agent error: {e}")
                    # Do not mark jobs processed on exceptions; later scans can retry them.

                # Delay between jobs
                await asyncio.sleep(JOB_DELAY_SECONDS)

            # Delay between search terms
            await asyncio.sleep(SEARCH_DELAY_SECONDS)

    # ── Final summary ──────────────────────────────────────
    print(f"\n{'='*55}")
    print(f"✅ Scan Complete!")
    print(f"   Total found:        {total_jobs}")
    print(f"   Skipped (internships): {skipped_internships}")
    print(f"   Skipped (dupes):       {skipped_dupes}")
    print(f"   Skipped (exp):      {skipped_exp}")
    print(f"   Failed scrapes:     {failed_scrapes}")
    print(f"   After filters:      {filtered_jobs}")
    print(f"   High matches:       {high_matches}")
    print(f"   Low matches:        {low_matches}")
    print(f"{'='*55}")

    await send_daily_summary(filtered_jobs, high_matches, low_matches)


# ── Local scheduler: runs every 10 hours ───────────────────

async def scheduler():
    """Runs the agent every 10 hours when used outside GitHub Actions."""
    while True:
        try:
            await run_agent()
        except Exception as e:
            print(f"❌ Scheduler error: {e}")

        next_run = datetime.now() + timedelta(hours=2)
        print(f"\n⏳ Next run at: {next_run.strftime('%H:%M')}")
        await asyncio.sleep(2 * 60 * 60)


# ── Entry point ────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LinkedIn job search agent")
    parser.add_argument(
        "--once", action="store_true",
        help="Run one scan and exit (for GitHub Actions).",
    )
    parser.add_argument(
        "--skip-schedule-check", action="store_true",
        help="Internal: the GitHub Actions schedule guard already claimed this run.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="With --once, run immediately even if fewer than 2 hours have elapsed.",
    )
    args = parser.parse_args()

    if args.once:
        if args.skip_schedule_check and args.force:
            parser.error("--force and --skip-schedule-check should not be used together.")

        claimed_here = not args.skip_schedule_check
        if claimed_here and not try_start_scheduled_run(interval_hours=2, force=args.force):
            print("⏭️  Scan not due yet; the last run started less than 2 hours ago.")
            raise SystemExit(0)

        print("🚀 Starting LinkedIn Job Agent for a single scan...\n")
        try:
            asyncio.run(run_agent())
        except BaseException:
            # A failed one-shot run should be eligible for retry at the next hourly check.
            if claimed_here:
                try:
                    reset_scheduled_run_after_failure()
                except Exception as reset_exc:
                    print(f"⚠️ Could not reset schedule state after failure: {reset_exc}")
            raise
    else:
        print("🚀 Starting LinkedIn Job Agent with 2-hour local scheduler...")
        print("   Press Ctrl+C to stop\n")
        asyncio.run(scheduler())
