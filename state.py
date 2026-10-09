from typing import TypedDict, Optional, List

class AgentState(TypedDict):
    # Input
    job_url:               str
    resume_text:           str
    resume_data:           dict
    resume_pdf_path:       str

    # Ingested
    job_title:             str
    company_name:          str
    job_description:       str
    required_skills:       list
    external_url:          Optional[str]

    # Evaluated
    match_score:           int
    gap_analysis:          str
    missing_skills:        List[str]
    matched_skills:        List[str]
    experience_match:      bool
    experience_required:   int
    candidate_years_experience:      int
    max_acceptable_experience_years: int

    # Generated
    cover_letter:          Optional[str]

    # Apply state
    apply_method:          Optional[str]
    application_status:    Optional[str]
    feedback:              Optional[str]