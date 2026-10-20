"""Generate concise feedback only for mid-range matches (50–74)."""
from tools.llm_client import extract_response_text, llm


async def feedback_node(state: dict) -> dict:
    """Feedback is optional: if models are unavailable, preserve evaluation."""
    prompt = f"""Give concise, practical resume feedback for this job match.
Score: {state.get('match_score', 0)}/100
Job: {state.get('job_title', '')} at {state.get('company_name', '')}
Matched skills: {state.get('matched_skills', [])}
Missing skills: {state.get('missing_skills', [])}
Evaluation: {state.get('gap_analysis', '')}
Resume excerpt: {str(state.get('resume_text', ''))[:900]}

In at most 120 words, list the 2 most important truthful improvements.
Do not suggest inventing skills or experience."""
    try:
        response = await llm.ainvoke(prompt)
        content = extract_response_text(response)
        if not content:
            raise RuntimeError("Empty feedback response")
        state["feedback"] = content[:1500]
        state["application_status"] = "feedback_generated"
        print(f"\n💡 Feedback generated for: {state.get('job_title', '')}")
        print(content[:350])
    except Exception as exc:
        # Evaluation succeeded, so don't force an expensive re-evaluation next scan.
        state["feedback"] = None
        state["application_status"] = "evaluated_feedback_unavailable"
        print(f"   ⚠️ Feedback skipped (evaluation retained): {type(exc).__name__}: {str(exc)[:180]}")
    return state
