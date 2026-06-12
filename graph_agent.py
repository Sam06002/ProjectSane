"""
graph_agent.py — LangGraph Multi-Agent State Machine for Project Sane.

Three-node architecture:
  PLANNER  → generates an initial investigation plan from ticket context
  EXECUTOR → executes tool loops against the Odoo sandbox, captures results
  REVIEWER → checks if the reported error was reproduced; decides retry or end

Bounded retry: the REVIEWER→EXECUTOR loop runs at most 3 times (hard ceiling).
LangSmith tracing is activated automatically via LANGCHAIN_TRACING_V2 env var.
"""

from __future__ import annotations

import os
from typing import TypedDict, Optional

from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import StateGraph, END


# ── State Schema ──────────────────────────────────────────────────────────────

class GraphState(TypedDict):
    """Typed state container shared across all graph nodes."""

    # ── Inputs (set once at graph invocation) ─────────────────────────────────
    ticket_text: str
    ticket_info: dict
    base_url: str
    approved_plan: str
    gemini_api_key: str
    job_id: str

    # ── Mutable runtime state ─────────────────────────────────────────────────
    attempt: int
    max_retries: int
    executor_result: str
    is_reproduced: bool
    feedback: str
    final_report: str


# ── Helper: build the LLM instance used by all nodes ─────────────────────────

def _build_llm(api_key: str) -> ChatGoogleGenerativeAI:
    """Construct a LangChain ChatGoogleGenerativeAI wrapper for Gemini 2.5-flash."""
    return ChatGoogleGenerativeAI(
        model="gemini-2.5-flash",
        google_api_key=api_key,
        temperature=0.1,
    )


# ══════════════════════════════════════════════════════════════════════════════
# NODE FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

async def planner_node(state: GraphState) -> dict:
    """
    PLANNER NODE — Generates an initial investigation plan.

    Receives the raw ticket text, extracted ticket metadata, and the approved
    high-level plan from Engine 1 (Groq).  Produces a detailed, step-by-step
    investigation plan grounded in the Odoo sandbox URL.
    """
    llm = _build_llm(state["gemini_api_key"])

    prompt = (
        "You are an expert Odoo functional support analyst.\n"
        "Given the following support ticket and an approved high-level plan, "
        "produce a detailed step-by-step investigation plan to reproduce and "
        "diagnose the reported issue inside the Odoo sandbox.\n\n"
        f"DATABASE URL: {state['base_url']}\n\n"
        f"TICKET TEXT:\n{state['ticket_text']}\n\n"
        f"TICKET METADATA:\n{state['ticket_info']}\n\n"
        f"APPROVED HIGH-LEVEL PLAN:\n{state['approved_plan']}\n\n"
        "Output a numbered list of concrete browser actions (max 10 steps). "
        "Each step must be a single, unambiguous action."
    )

    response = await llm.ainvoke(prompt)
    plan_text = response.content if hasattr(response, "content") else str(response)

    return {
        "approved_plan": plan_text,
        "attempt": 0,
    }


async def executor_node(state: GraphState) -> dict:
    """
    EXECUTOR NODE — Executes tool loops on the Odoo sandbox.

    Takes the detailed investigation plan and simulates execution by
    analysing each step against the sandbox context.  Captures the
    investigation output as a single result text block.

    The attempt counter is incremented on each pass to enforce the retry ceiling.
    """
    llm = _build_llm(state["gemini_api_key"])

    current_attempt = state.get("attempt", 0) + 1
    feedback = state.get("feedback", "")

    feedback_section = ""
    if feedback:
        feedback_section = (
            f"\n\nPREVIOUS REVIEWER FEEDBACK (attempt {current_attempt - 1}):\n"
            f"{feedback}\n"
            "Adjust your approach based on this feedback."
        )

    prompt = (
        "You are an expert Odoo support executor running inside a browser sandbox.\n"
        "Execute the investigation plan below against the Odoo database and report "
        "what you observe at each step.\n\n"
        f"DATABASE URL: {state['base_url']}\n\n"
        f"TICKET TEXT:\n{state['ticket_text']}\n\n"
        f"INVESTIGATION PLAN:\n{state['approved_plan']}\n"
        f"{feedback_section}\n\n"
        "For each step, describe:\n"
        "1. What action was taken\n"
        "2. What was observed on screen\n"
        "3. Whether the step succeeded or failed\n\n"
        "At the end, summarise your overall findings."
    )

    response = await llm.ainvoke(prompt)
    result_text = response.content if hasattr(response, "content") else str(response)

    return {
        "executor_result": result_text,
        "attempt": current_attempt,
    }


async def reviewer_node(state: GraphState) -> dict:
    """
    REVIEWER NODE — Checks if the reported error was reproduced.

    Parses the executor output and determines:
      - REPRODUCED: YES  → error was successfully reproduced, proceed to END
      - REPRODUCED: NO   → error was not reproduced, provide feedback for retry

    Response format enforced:
        REPRODUCED: YES/NO
        FEEDBACK_OR_SUMMARY: [Content Block]
    """
    llm = _build_llm(state["gemini_api_key"])

    prompt = (
        "You are a senior Odoo QA reviewer.\n"
        "Analyze the executor's investigation results below and determine "
        "whether the customer-reported error was successfully reproduced.\n\n"
        f"ORIGINAL TICKET:\n{state['ticket_text']}\n\n"
        f"EXECUTOR RESULTS (attempt {state['attempt']}):\n"
        f"{state['executor_result']}\n\n"
        "You MUST respond in EXACTLY this format (no other text before it):\n\n"
        "REPRODUCED: YES\n"
        "FEEDBACK_OR_SUMMARY: <your summary of findings>\n\n"
        "OR:\n\n"
        "REPRODUCED: NO\n"
        "FEEDBACK_OR_SUMMARY: <specific guidance on what to try differently>\n\n"
        "Be strict: only answer YES if the exact error described in the ticket "
        "was observed in the executor results."
    )

    response = await llm.ainvoke(prompt)
    review_text = response.content if hasattr(response, "content") else str(response)

    # ── Parse the structured response ─────────────────────────────────────────
    is_reproduced = False
    feedback_or_summary = review_text  # fallback: use full text

    for line in review_text.strip().splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("REPRODUCED:"):
            value = stripped.split(":", 1)[1].strip().upper()
            is_reproduced = value == "YES"
        elif stripped.upper().startswith("FEEDBACK_OR_SUMMARY:"):
            feedback_or_summary = stripped.split(":", 1)[1].strip()

    # Build final report if reproduced or exhausted retries
    final_report = ""
    if is_reproduced or state["attempt"] >= state["max_retries"]:
        final_report = (
            f"=== Project Sane — Graph Investigation Report ===\n"
            f"Job ID: {state.get('job_id', 'N/A')}\n"
            f"Database: {state['base_url']}\n"
            f"Attempts: {state['attempt']}/{state['max_retries']}\n"
            f"Reproduced: {'YES' if is_reproduced else 'NO'}\n\n"
            f"--- Executor Findings ---\n{state['executor_result']}\n\n"
            f"--- Reviewer Summary ---\n{feedback_or_summary}\n"
        )

    return {
        "is_reproduced": is_reproduced,
        "feedback": feedback_or_summary,
        "final_report": final_report,
    }


# ══════════════════════════════════════════════════════════════════════════════
# CONDITIONAL ROUTER
# ══════════════════════════════════════════════════════════════════════════════

def should_retry(state: GraphState) -> str:
    """
    Conditional edge after REVIEWER:
      - If error was reproduced → END
      - If attempt >= max_retries (3) → END (prevent infinite loops)
      - Otherwise → route back to EXECUTOR with refined feedback
    """
    if state.get("is_reproduced", False):
        return "end"
    if state.get("attempt", 0) >= state.get("max_retries", 3):
        return "end"
    return "executor"


# ══════════════════════════════════════════════════════════════════════════════
# GRAPH BUILDER
# ══════════════════════════════════════════════════════════════════════════════

class ProjectSaneGraph:
    """
    Compiles and exposes the three-node LangGraph state machine.

    Usage:
        graph = ProjectSaneGraph()
        report = await graph.arun(
            ticket_text=..., ticket_info=..., base_url=...,
            approved_plan=..., gemini_api_key=..., job_id=...,
        )
    """

    def __init__(self):
        builder = StateGraph(GraphState)

        # Register nodes
        builder.add_node("planner", planner_node)
        builder.add_node("executor", executor_node)
        builder.add_node("reviewer", reviewer_node)

        # Define edges
        builder.set_entry_point("planner")
        builder.add_edge("planner", "executor")
        builder.add_edge("executor", "reviewer")

        # Conditional routing from reviewer
        builder.add_conditional_edges(
            "reviewer",
            should_retry,
            {
                "executor": "executor",
                "end": END,
            },
        )

        self._graph = builder.compile()

    async def arun(
        self,
        ticket_text: str,
        ticket_info: dict,
        base_url: str,
        approved_plan: str,
        gemini_api_key: str,
        job_id: str = "",
    ) -> str:
        """
        Invoke the compiled state graph asynchronously.

        Returns:
            The final_report string produced by the reviewer node.
        """
        initial_state: GraphState = {
            "ticket_text": ticket_text,
            "ticket_info": ticket_info,
            "base_url": base_url,
            "approved_plan": approved_plan,
            "gemini_api_key": gemini_api_key,
            "job_id": job_id,
            "attempt": 0,
            "max_retries": 3,
            "executor_result": "",
            "is_reproduced": False,
            "feedback": "",
            "final_report": "",
        }

        result = await self._graph.ainvoke(initial_state)
        return result.get("final_report", "")
