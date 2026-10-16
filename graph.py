"""LangGraph workflow without cover-letter generation."""
from langgraph.graph import END, StateGraph

from state import AgentState
from nodes.evaluator import evaluator_node
from nodes.feedback import feedback_node
from nodes.logger import logger_node

MIN_MATCH_SCORE = 75
MIN_FEEDBACK_SCORE = 50


def score_router(state: dict) -> str:
    """Route evaluated jobs while avoiding unnecessary LLM calls."""
    if state.get("llm_failed"):
        print("\n⏭️ Evaluation failed. Stopping without feedback generation.")
        return "error"

    if not state.get("experience_match", True):
        print("\n⛔ Experience mismatch — routing to logger only.")
        return "skip"

    score = state.get("match_score")
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        print("\n❌ Invalid evaluation score — stopping graph.")
        return "error"

    if score >= MIN_MATCH_SCORE:
        print(f"\n🟢 Score {score}/100 → Eligible for Telegram alert (no cover letter)")
        return "high"
    if score >= MIN_FEEDBACK_SCORE:
        print(f"\n🟠 Score {score}/100 → Generating concise feedback")
        return "feedback"

    print(f"\n🔴 Score {score}/100 → Skipping feedback to conserve tokens")
    return "skip"


def build_graph():
    graph = StateGraph(AgentState)
    graph.add_node("evaluator", evaluator_node)
    graph.add_node("feedback", feedback_node)
    graph.add_node("logger", logger_node)
    graph.set_entry_point("evaluator")
    graph.add_conditional_edges(
        "evaluator",
        score_router,
        {
            "high": "logger",
            "feedback": "feedback",
            "skip": "logger",
            "error": END,
        },
    )
    graph.add_edge("feedback", "logger")
    graph.add_edge("logger", END)
    return graph.compile()


app = build_graph()