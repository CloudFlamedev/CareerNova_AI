Deploy the LinkedIn Job Agent with GitHub Actions + Neon
This change keeps the existing workflow and uses Neon for shared processed-job history. Local runs continue to use SQLite when DATABASE_URL is not set. GitHub Actions runs one scan at a time; it checks hourly but starts the agent only when 10 hours have passed since the previous start.
1. Create the Neon database
1. Create a project in Neon.
2. Open Connect and select the database/role for the project.
3. Copy the pooled PostgreSQL connection string if the Console offers direct and pooled options. Keep the sslmode=require parameter. Treat the URL like a password.
4. Add it to your local .env as DATABASE_URL=... to migrate the local job history.
Do not paste the URL into chat or commit it to GitHub.
2. Migrate existing SQLite job history
From the project root, make sure logs/applications.db is the database you currently use and DATABASE_URL points to Neon. Then run:
source venv/bin/activate
python -m pip install "psycopg[binary]==3.3.6"
python migrate_sqlite_to_neon.py
The migration is idempotent: existing Neon rows with the same job_hash are skipped. It does not delete or modify the local SQLite database. Run this before your first scheduled workflow so existing processed jobs stay deduplicated.
3. Keep the GitHub repository private and clean
The agent reads data/resume.pdf, so that file must exist on the runner. The simplest arrangement is a private GitHub repository containing the resume file. Do not put your resume in a public repository.
Never commit .env, logs/applications.db, venv/, or the full ZIP containing your virtual environment. The fixed .gitignore in this update excludes those paths. If any are already tracked, run this from the project root:
git rm --cached --ignore-unmatch .env
git rm -r --cached --ignore-unmatch logs venv .venv
Then review git status before committing. .gitignore cannot untrack a file that Git already knows about.
4. Add GitHub Actions secrets
In the GitHub repository, open Settings → Secrets and variables → Actions → New repository secret and add:
Secret name	Value
DATABASE_URL	Neon pooled PostgreSQL connection string
GROQ_API_KEY	Your Groq API key
TELEGRAM_BOT_TOKEN	Your Telegram bot token
TELEGRAM_CHAT_ID	Your Telegram chat ID


GEMINI_API_KEY is not required by the current Groq-only model router. Do not add email credentials unless you later re-enable an email application feature.
5. Add the updated files
Copy the individual files from this update into these locations in your existing project:
- main.py
- requirements.txt
- .gitignore
- .env.example
- tools/database.py (new)
- tools/job_tracker.py
- tools/telegram_notifier.py
- nodes/logger.py
- scripts/schedule_guard.py (new)
- .github/workflows/linkedin-agent.yml (new)
- migrate_sqlite_to_neon.py (new)
Keep the remaining project files unchanged. Preserve your real local .env and credentials; do not replace it with .env.example.
6. Commit and run a manual test
Commit the workflow to the repository's default branch. GitHub scheduled workflows run from the default branch.
Open Actions → LinkedIn Job Agent → Run workflow. For the first test, check force_run if you need to bypass the 10-hour gate. Inspect the logs. The job should:
1. Connect to Neon and claim the 10-hour run window.
2. Install the agent dependencies only if a scan is due.
3. Verify data/resume.pdf is present.
4. Run a single scan and exit.
5. Reset the schedule claim if the workflow itself fails, allowing the next hourly check to retry.
Do not add the workflow to a public repository containing your resume or secrets.
How the 10-hour schedule works
GitHub Actions checks the schedule at minute 17 of every hour in Asia/Kolkata. A lightweight guard stored in Neon decides whether to run the full agent. The actual scan starts at the first hourly check after 10 hours have elapsed, so intervals are approximately 10–11 hours, not an exact real-time timer. Use Run workflow with force_run to start a scan manually.
The current agent still searches India and remote jobs posted in the last 12 hours. Cover-letter generation stays disabled.