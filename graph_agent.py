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

import base64
import os
import time
from typing import TypedDict, Optional

from langchain_core.messages import HumanMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import StateGraph, END
import memory_store
import nest_asyncio
nest_asyncio.apply()  # allows run_until_complete inside a running event loop (thread safety)


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
    final_findings: str


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

    # --- Fetch historical resolution and routing patterns ---
    odoo_module = state["ticket_info"].get("module", "")
    error_message = state["ticket_info"].get("summary", "")
    try:
        similar_resolutions = memory_store.search_similar_resolutions(odoo_module, error_message)
        nav_patterns = memory_store.get_all_navigation_patterns()
        
        memory_context = ""
        if similar_resolutions:
            memory_context += "PAST SIMILAR RESOLUTIONS (Use these to guide your plan):\n"
            for res in similar_resolutions:
                memory_context += f"- Ticket: {res['ticket_summary']}\n  Root Cause: {res['root_cause']}\n  Fix: {res['resolution_steps']}\n"
        if nav_patterns:
            memory_context += "\nKNOWN NAVIGATION PATTERNS:\n"
            for pat in nav_patterns:
                memory_context += f"- {pat['pattern_name']}: {pat['url_structure']}\n"
    except Exception as e:
        print(f"[Graph Memory] Failed to fetch memory context: {e}")
        memory_context = ""

    prompt = (
        "You are an expert Odoo functional support analyst.\n"
        "Given the following support ticket and an approved high-level plan, "
        "produce a detailed step-by-step investigation plan to reproduce and "
        "diagnose the reported issue inside the Odoo sandbox.\n\n"
        f"DATABASE URL: {state['base_url']}\n\n"
        f"TICKET TEXT:\n{state['ticket_text']}\n\n"
        f"TICKET METADATA:\n{state['ticket_info']}\n\n"
        f"APPROVED HIGH-LEVEL PLAN:\n{state['approved_plan']}\n\n"
        f"{memory_context}\n"
        "Output a numbered list of concrete browser actions (max 10 steps). "
        "Each step must be a single, unambiguous action."
    )

    response = await llm.ainvoke(prompt)
    plan_text = response.content if hasattr(response, "content") else str(response)

    return {
        "approved_plan": plan_text,
        "attempt": 0,
    }


async def executor_node(state: GraphState, browser=None) -> dict:
    """
    EXECUTOR NODE — Inspects the live Odoo sandbox and executes the plan.

    When a live BrowserManager is supplied, the node captures a real-time,
    full-page screenshot of the active Playwright viewport, records it in the
    browser's screenshot history, and hands it to Gemini as multimodal context
    so the evaluation is grounded in the true sandbox state (not a text-only
    simulation). Falls back to text-only reasoning if no live page is available.

    The attempt counter is incremented on each pass to enforce the retry ceiling.
    """
    llm = _build_llm(state["gemini_api_key"])

    current_attempt = state.get("attempt", 0) + 1
    feedback = state.get("feedback", "")

    # ── Live Visual Evidence Collection (the graph's "Eyes") ──────────────────
    screenshot_b64 = ""
    current_url = state["base_url"]
    if browser is not None and getattr(browser, "page", None) is not None:
        current_url = browser.page.url or current_url
        try:
            os.makedirs("output", exist_ok=True)
            screenshot_path = (
                f"output/graph_step_attempt_{current_attempt}_{int(time.time())}.png"
            )
            await browser.page.screenshot(path=screenshot_path, full_page=True)

            # Record in the browser manager's screenshot history
            if getattr(browser, "screenshots", None) is None:
                browser.screenshots = []
            if screenshot_path not in browser.screenshots:
                browser.screenshots.append(screenshot_path)

            with open(screenshot_path, "rb") as f:
                screenshot_b64 = base64.b64encode(f.read()).decode("utf-8")
        except Exception as e:
            import traceback
            error_detail = traceback.format_exc()
            print(f"[EXECUTOR ERROR] Live screenshot capture failed: {e}")
            print(error_detail)

    feedback_section = ""
    if feedback:
        feedback_section = (
            f"\n\nPREVIOUS REVIEWER FEEDBACK (attempt {current_attempt - 1}):\n"
            f"{feedback}\n"
            "Adjust your approach based on this feedback."
        )

    user_prompt = (
        "You are an expert Odoo support executor running inside a live browser sandbox.\n"
        "Inspect the attached live screenshot of the current Odoo viewport and execute "
        "the investigation plan against the real database state.\n\n"
        f"CURRENT URL: {current_url}\n\n"
        f"TICKET TEXT:\n{state['ticket_text']}\n\n"
        f"INVESTIGATION PLAN:\n{state['approved_plan']}"
        f"{feedback_section}\n\n"
        "For each step, describe:\n"
        "1. What action was taken\n"
        "2. What is actually visible on screen\n"
        "3. Whether the step succeeded or failed\n\n"
        "At the end, summarise your overall findings."
    )

    # Build a LangChain multimodal message for the Gemini wrapper.
    if screenshot_b64:
        message = HumanMessage(
            content=[
                {"type": "text", "text": user_prompt},
                {
                    "type": "image_url",
                    "image_url": f"data:image/png;base64,{screenshot_b64}",
                },
            ]
        )
    else:
        message = HumanMessage(content=user_prompt)

    try:
        response = await llm.ainvoke([message])
        result_text = response.content if hasattr(response, "content") else str(response)
    except Exception as e:
        import traceback
        error_detail = traceback.format_exc()
        print(f"[EXECUTOR ERROR] LLM invocation failed: {e}")
        print(error_detail)
        result_text = f"Executor failed: {e}\n\nDetail:\n{error_detail}"

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

    # Combined background log signature (raw trace + reviewer summary).
    # `feedback` carries the polished functional fix steps only, while
    # `final_report` retains the full machine log for diagnostics.
    executor_result = state["executor_result"]
    final_report = (
        f"=== Project Sane — Graph Investigation Report ===\n"
        f"Job ID: {state.get('job_id', 'N/A')}\n"
        f"Database: {state['base_url']}\n"
        f"Attempts: {state['attempt']}/{state['max_retries']}\n"
        f"Reproduced: {'YES' if is_reproduced else 'NO'}\n\n"
        f"--- Executor Findings ---\n{executor_result}\n\n"
        f"--- Reviewer Summary ---\n{feedback_or_summary}\n"
    )

    final_findings = ""
    if is_reproduced or state["attempt"] >= state["max_retries"]:
        # Generate clean final_findings using LLM in standard format
        findings_prompt = (
            "You are an expert Odoo support analyst. Synthesize a clean, concise resolution summary "
            "from the following ticket and investigation results.\n\n"
            f"ORIGINAL TICKET:\n{state['ticket_text']}\n\n"
            f"INVESTIGATION RESULTS:\n{executor_result}\n\n"
            "Output EXACTLY in this format with no other text, headers, code blocks, or markdown formatting outside this template:\n"
            "**Request Summary:** [1-sentence distillation of what the customer wanted]\n"
            "**Root Cause:** [Clear explanation of why it was not working]\n"
            "**The Fix:**\n"
            "- [First step to solve the issue]\n"
            "- [Second step to solve the issue]\n\n"
            "Ensure you strip away any dynamic code, raw JSON schemas, or token metrics."
        )
        try:
            findings_response = await llm.ainvoke(findings_prompt)
            final_findings = findings_response.content if hasattr(findings_response, "content") else str(findings_response)
            final_findings = final_findings.strip()
        except Exception as e:
            final_findings = (
                f"**Request Summary:** Investigation complete.\n"
                f"**Root Cause:** Could not determine root cause due to error: {e}\n"
                f"**The Fix:**\n- Manual verification required."
            )

        # --- Save Verified Resolution to Memory ---
        try:
            root_cause = "Unknown root cause"
            resolution_steps = []
            for line in final_findings.splitlines():
                if line.startswith("**Root Cause:**"):
                    root_cause = line.replace("**Root Cause:**", "").strip()
                elif line.startswith("- "):
                    resolution_steps.append(line[2:].strip())

            odoo_module = state["ticket_info"].get("module", "Unknown")
            ticket_summary = state["ticket_info"].get("summary", "Unknown")
            odoo_version = state["ticket_info"].get("version", "Unknown")
            error_msg_short = state["ticket_text"][:250]
            
            memory_store.save_resolution(
                ticket_summary=ticket_summary,
                odoo_module=odoo_module,
                odoo_version=odoo_version,
                error_message=error_msg_short,
                root_cause=root_cause,
                resolution_steps=resolution_steps
            )
        except Exception as e:
            print(f"[Graph Memory] Failed to save resolution: {e}")

    return {
        "is_reproduced": is_reproduced,
        "feedback": feedback_or_summary,      # Polished functional fix steps only
        "executor_result": executor_result,   # Raw console trace log
        "final_report": final_report,         # Combined log system signature
        "final_findings": final_findings,     # Clean summary findings for draft card
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

    def __init__(self, browser_instance=None):
        """Ingests the live BrowserManager (page + screenshots) for the executor."""
        self.browser = browser_instance

        builder = StateGraph(GraphState)

        # Register nodes
        builder.add_node("planner", planner_node)

        # Closure pass-through so the executor node receives the live browser
        # context. Must be async so LangGraph awaits the node coroutine.
        async def executor_wrapper(state: GraphState) -> dict:
            return await executor_node(state, self.browser)

        builder.add_node("executor", executor_wrapper)
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
    ) -> dict:
        """
        Invoke the compiled state graph asynchronously.

        Returns:
            The full final GraphState dict (feedback, executor_result,
            final_report, is_reproduced, final_findings, ...) so callers can map fields
            independently for the UI summary and the .docx report.
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
            "final_findings": "",
        }

        final_context = await self._graph.ainvoke(initial_state)
        return final_context
