from typing import List, Optional, TypedDict


class AgentState(TypedDict, total=False):
    # Input / job details
    job_url: str
    job_title: str
    company_name: str
    job_description: str
    required_skills: list
    external_url: Optional[str]
    resume_text: str
    resume_data: dict
    resume_pdf_path: str

    # Evaluation
    match_score: Optional[int]
    gap_analysis: str
    missing_skills: List[str]
    matched_skills: List[str]
    experience_match: bool
    experience_required: int
    candidate_years_experience: int
    max_acceptable_experience_years: int

    # LLM status
    llm_failed: bool
    llm_error: str

    # Generated content / application state
    cover_letter: Optional[str]
    apply_method: Optional[str]
    application_status: Optional[str]
    feedback: Optional[str]