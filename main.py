import asyncio
import os
import re
from datetime import datetime, timedelta
from jobspy import scrape_jobs
from tools.resume_parser import parse_resume
from tools.telegram_notifier import send_job_alert, send_daily_summary
from tools.job_tracker import is_already_processed, mark_job_processed
from graph import app
from dotenv import load_dotenv

load_dotenv()

# ── Config ─────────────────────────────────────────────────

MIN_MATCH_SCORE    = 75
RESULTS_PER_SEARCH = 20
MAX_RETRIES        = 3

# International scraping — add/remove locations as you like.
# "Remote" is a valid LinkedIn location query and surfaces
# globally-remote postings that aren't tied to one country.
LOCATIONS = [
    "India",
    "United States",
    "United Kingdom",
    "Canada",
    "Remote",
]

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

def is_posted_recently(job, hours: int = 24) -> bool:
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
                hours_old=24,
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
    print(f"📊 Min match score:   {MIN_MATCH_SCORE}+")
    print(f"📈 Experience gate:   candidate {CANDIDATE_YEARS_EXPERIENCE} yrs, accept up to {MAX_ACCEPTABLE_EXPERIENCE_YEARS} yrs required")
    print(f"🔍 Search terms:      {len(JOB_SEARCH_TERMS)}")
    print(f"🌍 Locations:         {len(LOCATIONS)} ({', '.join(LOCATIONS)})")
    print(f"🔁 Total searches:    {total_combos} (terms × locations)")
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

                # ── Filter 1: Skip already processed jobs ──
                if is_already_processed(title, company):
                    skipped_dupes += 1
                    print(f"   ⏭️  Already processed: {title} @ {company}")
                    continue

                # ── Filter 2: Posted in last 24 hours ──
                if not is_posted_recently(job, hours=24):
                    print(f"   ⏭️  Skipping (too old): {title}")
                    continue

                # ── Filter 3: Description must actually be present ──
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
                    "match_score":                    0,
                    "gap_analysis":                   "",
                    "missing_skills":                 [],
                    "matched_skills":                 [],
                    "experience_match":               True,   # ← LLM will update this
                    "experience_required":            0,      # ← LLM will update this
                    "candidate_years_experience":      CANDIDATE_YEARS_EXPERIENCE,
                    "max_acceptable_experience_years": MAX_ACCEPTABLE_EXPERIENCE_YEARS,
                    "cover_letter":                   None,
                    "apply_method":                   None,
                    "application_status":             None,
                    "feedback":                       None,
                }

                # ── Run the graph ──
                try:
                    result = await app.ainvoke(state)

                    score        = result.get("match_score", 0)
                    exp_match    = result.get("experience_match", True)
                    exp_required = result.get("experience_required", 0)

                    # Mark processed regardless of outcome
                    mark_job_processed(title, company)

                    # ── Block 1: Experience mismatch → skip ──
                    if not exp_match:
                        skipped_exp += 1
                        print(f"   ⛔ Skipping alert — needs {exp_required} yrs (accepting up to {MAX_ACCEPTABLE_EXPERIENCE_YEARS})")
                        low_matches += 1
                        continue

                    # ── Block 2: Score too low → skip ──
                    if score < MIN_MATCH_SCORE:
                        low_matches += 1
                        print(f"   🔴 Score too low ({score}) — skipping alert")
                        continue

                    # ── Block 3: Good match → send Telegram ──
                    high_matches += 1
                    await send_job_alert(result)
                    print(f"   📱 Telegram alert sent!")

                except Exception as e:
                    print(f"   ❌ Agent error: {e}")
                    mark_job_processed(title, company)

                # Delay between jobs
                await asyncio.sleep(30)

            # Delay between search terms
            await asyncio.sleep(10)

    # ── Final summary ──────────────────────────────────────
    print(f"\n{'='*55}")
    print(f"✅ Scan Complete!")
    print(f"   Total found:        {total_jobs}")
    print(f"   Skipped (dupes):    {skipped_dupes}")
    print(f"   Skipped (exp):      {skipped_exp}")
    print(f"   Failed scrapes:     {failed_scrapes}")
    print(f"   After filters:      {filtered_jobs}")
    print(f"   High matches:       {high_matches}")
    print(f"   Low matches:        {low_matches}")
    print(f"{'='*55}")

    await send_daily_summary(filtered_jobs, high_matches, low_matches)


# ── Scheduler: runs every 6 hours ─────────────────────────

async def scheduler():
    """Runs the agent every 6 hours automatically."""
    while True:
        try:
            await run_agent()
        except Exception as e:
            print(f"❌ Scheduler error: {e}")

        next_run = datetime.now() + timedelta(hours=6)
        print(f"\n⏳ Next run at: {next_run.strftime('%H:%M')}")

        await asyncio.sleep(6 * 60 * 60)


# ── Entry point ────────────────────────────────────────────

if __name__ == "__main__":
    print("🚀 Starting LinkedIn Job Agent with scheduler...")
    print("   Press Ctrl+C to stop\n")
    asyncio.run(scheduler())