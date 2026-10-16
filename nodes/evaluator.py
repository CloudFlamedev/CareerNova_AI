"""Evaluate resume/job fit with JSON validation and retry via model rotation."""
import json
import re
from numbers import Number

from tools.llm_client import llm


def _mark_llm_failure(state: dict, message: str) -> dict:
    """Mark evaluation as failed without inventing a zero score."""
    state["llm_failed"] = True
    state["llm_error"] = message[:1000]
    state["application_status"] = "llm_error"
    state["match_score"] = None
    state["gap_analysis"] = "LLM evaluation failed; this job can be retried on a later run."
    state["missing_skills"] = []
    state["matched_skills"] = []
    state["experience_match"] = True
    state["experience_required"] = 0
    print(f"   ⚠️ LLM evaluation failed: {message[:250]}")
    return state


def _parse_json_object(content: str) -> dict:
    raw = (content or "").strip()
    if not raw:
        raise ValueError("Empty response from the LLM")
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s*```$", "", raw).strip()
    start = raw.find("{")
    if start < 0:
        raise ValueError("Response did not contain a JSON object")
    parsed, _ = json.JSONDecoder().raw_decode(raw[start:])
    if not isinstance(parsed, dict):
        raise ValueError("LLM response was not a JSON object")
    return parsed


async def evaluator_node(state: dict) -> dict:
    """Score the resume against a job description."""
    resume_text = str(state.get("resume_text", ""))[:1400]
    job_desc = str(state.get("job_description", ""))[:900]
    candidate_years = int(state.get("candidate_years_experience", 3) or 0)
    max_acceptable = int(state.get("max_acceptable_experience_years", 5) or 0)

    state["llm_failed"] = False
    state["llm_error"] = ""

    prompt = f"""Act as a hiring manager comparing a resume with a job description.
Return ONLY one valid JSON object. Do not use markdown or extra text.
Schema:
{{
  "score": 0,
  "matched_skills": [],
  "missing_skills": [],
  "experience_required": 0,
  "experience_match": true,
  "summary": "one concise sentence"
}}
Rules:
- score must be an integer from 0 to 100.
- matched_skills and missing_skills must be arrays of short strings.
- experience_required is the minimum required years, or 0 if unstated.
- Candidate has {candidate_years} years of experience.
- Set experience_match to false if the role requires more than {max_acceptable} years.
- Keep the JSON concise. Do not claim skills that the resume does not demonstrate.

RESUME:
{resume_text}

JOB TITLE: {state.get('job_title', '')}
COMPANY: {state.get('company_name', '')}
JOB DESCRIPTION:
{job_desc}
"""

    last_error = ""
    # A malformed JSON response is not a rate limit. Retry a few times;
    # the router round-robins the starting model on each call.
    for attempt in range(1, 4):
        try:
            response = await llm.ainvoke(prompt)
            result = _parse_json_object(str(response.content or ""))

            raw_score = result.get("score")
            if isinstance(raw_score, bool) or not isinstance(raw_score, Number):
                if not isinstance(raw_score, str) or not raw_score.strip().isdigit():
                    raise ValueError("score must be an integer from 0 to 100")
                raw_score = int(raw_score.strip())
            score = max(0, min(100, int(raw_score)))

            matched = result.get("matched_skills", [])
            missing = result.get("missing_skills", [])
            if not isinstance(matched, list) or not isinstance(missing, list):
                raise ValueError("matched_skills and missing_skills must be JSON arrays")

            raw_exp = result.get("experience_required", 0)
            if isinstance(raw_exp, bool):
                raise ValueError("experience_required must be a number")
            exp_required = max(0, int(raw_exp or 0))
            exp_match_value = result.get("experience_match", True)
            if not isinstance(exp_match_value, bool):
                raise ValueError("experience_match must be true or false")
            break
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            print(f"   ⚠️ Evaluation attempt {attempt}/3 failed: {last_error[:180]}")
            if attempt < 3:
                continue
            return _mark_llm_failure(state, last_error)

    exp_match = exp_match_value and exp_required <= max_acceptable
    state["match_score"] = score if exp_match else 0
    state["gap_analysis"] = (
        f"Job requires {exp_required} years; candidate has {candidate_years} years."
        if not exp_match
        else str(result.get("summary", ""))[:1000]
    )
    state["missing_skills"] = [str(item)[:100] for item in missing[:20]]
    state["matched_skills"] = [str(item)[:100] for item in matched[:20]]
    state["experience_match"] = exp_match
    state["experience_required"] = exp_required
    state["application_status"] = "evaluated"

    print(f"\n✅ Match Score: {state['match_score']}/100")
    print(
        f"✅ Experience required: {exp_required} years — "
        f"{'candidate qualifies' if exp_match else 'experience mismatch'}"
    )
    print(f"✅ Matched skills: {state['matched_skills']}")
    print(f"⚠️ Missing skills: {state['missing_skills']}")
    print(f"📝 {state['gap_analysis']}")
    return state