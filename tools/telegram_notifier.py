import os
import asyncio
import aiohttp
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")

async def send_telegram_message(message: str):
    """Sends a message to your Telegram chat."""
    if not BOT_TOKEN or not CHAT_ID:
        print("⚠️  Telegram not configured in .env")
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    
    payload = {
        "chat_id":    CHAT_ID,
        "text":       message,
        "parse_mode": "HTML"
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    print("   📱 Telegram notification sent!")
                else:
                    print(f"   ⚠️  Telegram error: {resp.status}")
    except Exception as e:
        print(f"   ❌ Telegram failed: {e}")


async def send_job_alert(state: dict):
    """Sends a formatted job alert with missing skills."""
    
    score        = state.get("match_score", 0)
    job_title    = state.get("job_title", "N/A")
    company      = state.get("company_name", "N/A")
    job_url      = state.get("job_url", "")
    missing      = state.get("missing_skills", [])
    matched      = state.get("matched_skills", [])

    # Format missing skills as bullet list
    if missing:
        missing_text = "\n".join([f"  • {skill}" for skill in missing[:6]])
    else:
        missing_text = "  • None — perfect match!"

    # Format matched skills (top 5 only)
    if matched:
        matched_text = ", ".join(matched[:5])
        if len(matched) > 5:
            matched_text += f" +{len(matched)-5} more"
    else:
        matched_text = "See resume"

    message = f"""
🔥 <b>HIGH MATCH JOB ALERT</b>

📋 <b>{job_title}</b>
🏢 {company}
⭐ Match Score: <b>{score}/100</b>
✅ <b>Your Matching Skills:</b>
{matched_text}

⚠️ <b>Missing Skills to Add:</b>
{missing_text}

🔗 <a href="{job_url}">Apply Now →</a>

<i>Posted in last 12 hours • Apply fast!</i>
"""

    await send_telegram_message(message.strip())


async def send_daily_summary(jobs_processed: int, 
                              high_matches: int, 
                              low_matches: int):
    """Sends a summary report to Telegram."""
    
    message = f"""
📊 <b>Job Agent Report</b>

🔍 Jobs Scanned: {jobs_processed}
✅ High Matches (75+): {high_matches}
🔴 Low Matches: {low_matches}

<i>Next scan in about 2 hours</i>
"""
    await send_telegram_message(message.strip())
