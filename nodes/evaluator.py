from tools.llm_client import llm
import json
import re

async def evaluator_node(state: dict) -> dict:
    """Scores resume against job description using Groq/Gemini."""

    resume_text = state['resume_text'][:2000]
    job_desc    = state['job_description'][:1500]

    candidate_years = state.get('candidate_years_experience', 3)
    max_acceptable   = state.get('max_acceptable_experience_years', 5)

    prompt = f"""
You are a hiring manager. Read this job description carefully.

RESUME:
{resume_text}

JOB TITLE: {state['job_title']}
COMPANY: {state['company_name']}
JOB DESCRIPTION:
{job_desc}

TASK:
1. Read the job description and find how many years of experience it requires.
2. The candidate has {candidate_years} years of experience.
3. If the job requires MORE than {max_acceptable} years — set experience_match to false.
4. Score how well the resume matches the job skills.

IMPORTANT: Return ONLY a JSON object. No explanation. No markdown.

The JSON must follow this exact structure:
{{
    "score": <number between 0 and 100>,
    "matched_skills": <list of skills from resume that match job>,
    "missing_skills": <list of skills job needs but resume lacks>,
    "experience_required": <exact minimum years the job requires, write 0 if not mentioned>,
    "experience_match": <write false if job needs more than {max_acceptable} years, otherwise true>,
    "summary": <one sentence about the match>
}}
"""

    try:
        response = await llm.ainvoke(prompt)
        raw = response.content.strip()

        if not raw:
            print(f"   ⚠️  Empty LLM response — skipping job")
            state["match_score"]        = 0
            state["gap_analysis"]       = "LLM returned empty response"
            state["missing_skills"]     = []
            state["matched_skills"]     = []
            state["experience_match"]   = True
            state["experience_required"] = 0
            return state

        # Clean response
        raw = raw.replace("```json", "").replace("```", "").strip()

        # Extract JSON if wrapped in extra text
        json_match = re.search(r'\{.*\}', raw, re.DOTALL)
        if json_match:
            raw = json_match.group(0)

        result = json.loads(raw)

    except json.JSONDecodeError as e:
        print(f"   ⚠️  JSON parse error: {e} — skipping")
        state["match_score"]        = 0
        state["gap_analysis"]       = "Could not parse LLM response"
        state["missing_skills"]     = []
        state["matched_skills"]     = []
        state["experience_match"]   = True
        state["experience_required"] = 0
        return state

    except Exception as e:
        print(f"   ⚠️  LLM error: {e} — skipping")
        state["match_score"]        = 0
        state["gap_analysis"]       = str(e)
        state["missing_skills"]     = []
        state["matched_skills"]     = []
        state["experience_match"]   = True
        state["experience_required"] = 0
        return state

    # ── Experience gate ────────────────────────────────────
    exp_required = result.get("experience_required", 0)
    exp_match    = result.get("experience_match", True)

    if not exp_match or exp_required > max_acceptable:
        print(f"\n⛔ Job needs {exp_required} yrs — candidate has {candidate_years} yrs — skipping")
        state["match_score"]         = 0
        state["gap_analysis"]        = f"Job requires {exp_required} years, candidate has {candidate_years}"
        state["missing_skills"]      = []
        state["matched_skills"]      = []
        state["experience_match"]    = False
        state["experience_required"] = exp_required
        return state

    # ── Good match — set full state ────────────────────────
    state["match_score"]         = result.get("score", 0)
    state["gap_analysis"]        = result.get("summary", "")
    state["missing_skills"]      = result.get("missing_skills", [])
    state["matched_skills"]      = result.get("matched_skills", [])
    state["experience_match"]    = True
    state["experience_required"] = exp_required

    print(f"\n✅ Match Score: {result.get('score', 0)}/100")
    print(f"✅ Exp required: {exp_required} yrs — candidate qualifies")
    print(f"✅ Matched: {result.get('matched_skills', [])}")
    print(f"⚠️  Missing: {result.get('missing_skills', [])}")
    print(f"📝 {result.get('summary', '')}")

    return state