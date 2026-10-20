"""Evaluate resume/job fit with JSON validation and retry via model rotation."""
from __future__ import annotations

import json
import re
from numbers import Number

from tools.llm_client import extract_response_text, llm


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
    """Parse the first JSON object from a plain-text or fenced model response."""
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


def _extract_explicit_experience_years(text: str) -> int:
    """Extract a stated minimum-years requirement when the model returns 0/unstated.

    For ranges such as '3-5 years of experience', the lower end is treated as
    the minimum required experience. This is a fallback, not a replacement for
    the evaluator's interpretation of the full job description.
    """
    source = re.sub(r"\s+", " ", str(text or "").lower())
    patterns = (
        # e.g. 'minimum of 5 years', 'at least 5 years'
        r"\b(?:at least|minimum(?: of)?|min\.?)\s*(\d{1,2})\s*(?:\+\s*)?(?:years?|yrs?)\b",
        # e.g. '5+ years of relevant experience'
        r"\b(\d{1,2})\s*\+\s*(?:years?|yrs?)\s*(?:of\s+)?(?:relevant\s+)?experience\b",
        # e.g. '3-5 years experience'; use the lower end as the minimum
        r"\b(\d{1,2})\s*(?:-|–|—|to)\s*(\d{1,2})\s*(?:years?|yrs?)\s*(?:of\s+)?(?:relevant\s+)?experience\b",
        # e.g. '5 years of experience'
        r"\b(\d{1,2})\s*(?:years?|yrs?)\s*(?:of\s+)?(?:relevant\s+)?experience\b",
        # e.g. 'experience: 5 years'
        r"\bexperience\s*(?:of|:)?\s*(\d{1,2})\s*(?:years?|yrs?)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, source)
        if match:
            return int(match.group(1))
    return 0


def evaluator_node_experience_gate(required_years: int, max_acceptable_years: int) -> bool:
    """Apply the configured experience rule deterministically.

    A requirement of 0 means the job description did not yield a clear numeric
    requirement, so it is not rejected solely because the LLM's boolean says
    otherwise. The LLM boolean is intentionally not authoritative: the Python
    rule is simply to reject roles whose stated minimum exceeds the configured
    maximum.
    """
    return required_years <= max_acceptable_years


async def evaluator_node(state: dict) -> dict:
    """Score the resume against a job description."""
    resume_text = str(state.get("resume_text", ""))[:1400]
    job_title = str(state.get("job_title", ""))
    full_job_desc = str(state.get("job_description", ""))
    job_desc = full_job_desc[:900]
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
  "summary": "one concise sentence"
}}
Rules:
- score must be an integer from 0 to 100.
- matched_skills and missing_skills must each contain no more than 6 short strings.
- experience_required must be the minimum required years as an integer, or 0 if unstated.
- For a range like "3-5 years experience", use the lower end (3) as the minimum requirement.
- Candidate has {candidate_years} years of experience.
- Do not return or decide an experience_match boolean; Python applies the configured limit.
- Keep the JSON short and complete. Never cut off a string or leave JSON unfinished.
- Do not claim skills that the resume does not demonstrate.

RESUME:
{resume_text}

JOB TITLE: {job_title}
COMPANY: {state.get('company_name', '')}
JOB DESCRIPTION:
{job_desc}
"""

    last_error = ""
    # Malformed JSON is retried; each router call rotates its starting model.
    for attempt in range(1, 4):
        try:
            response = await llm.ainvoke(prompt)
            result = _parse_json_object(extract_response_text(response))

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
            try:
                exp_required = max(0, int(raw_exp or 0))
            except (TypeError, ValueError):
                raise ValueError("experience_required must be a non-negative integer") from None

            # If the LLM did not extract a numeric requirement, recover a clearly
            # stated value from the title/description before deciding eligibility.
            if exp_required == 0:
                exp_required = _extract_explicit_experience_years(f"{job_title}\n{full_job_desc}")
            break
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            print(f"   ⚠️ Evaluation attempt {attempt}/3 failed: {last_error[:180]}")
            if attempt < 3:
                continue
            return _mark_llm_failure(state, last_error)

    # Do not combine this with the LLM's experience_match boolean. Logs showed
    # that the model marked 5-year roles as mismatches even when the configured
    # maximum was 5. The numeric rule is the source of truth.
    exp_match = evaluator_node_experience_gate(exp_required, max_acceptable)
    state["match_score"] = score if exp_match else 0
    state["gap_analysis"] = (
        f"Job requires {exp_required} years; configured maximum is {max_acceptable} years."
        if not exp_match
        else str(result.get("summary", ""))[:1000]
    )
    state["missing_skills"] = [str(item)[:100] for item in missing[:6]]
    state["matched_skills"] = [str(item)[:100] for item in matched[:6]]
    state["experience_match"] = exp_match
    state["experience_required"] = exp_required
    state["application_status"] = "evaluated"

    if exp_required == 0:
        print("ℹ️ No explicit numeric experience requirement found; evaluating by match score.")
    print(f"\n✅ Match Score: {state['match_score']}/100")
    print(
        f"✅ Experience required: {exp_required} years — "
        f"{'candidate passes configured experience gate' if exp_match else 'experience mismatch'}"
    )
    print(f"✅ Matched skills: {state['matched_skills']}")
    print(f"⚠️ Missing skills: {state['missing_skills']}")
    print(f"📝 {state['gap_analysis']}")
    return state
