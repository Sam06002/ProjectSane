"""
graph_agent.py — LangGraph Multi-Agent State Machine for Project Sane v3.

Hybrid Split-Compute Topology (ANTIGRAVITY_PROMPT_015):
  PLANNER  → Text-only reasoning via Groq (llama-3.3-70b-versatile, 14,400 RPD).
  EXECUTOR → Multimodal vision via Gemini (gemini-2.5-flash); screenshots embedded
             as base64 image parts at key navigation decision checkpoints.
  REVIEWER → Text-only reasoning via Groq.

Bounded retry: the REVIEWER→EXECUTOR loop runs at most 3 times (hard ceiling).
LangSmith tracing is activated automatically via LANGCHAIN_TRACING_V2 env var.
"""

from __future__ import annotations

import base64
import os
import time
from typing import TypedDict, Optional

from langchain_core.messages import HumanMessage, ToolMessage
from langchain_groq import ChatGroq
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import StateGraph, END
import memory_store
import nest_asyncio
nest_asyncio.apply()  # allows run_until_complete inside a running event loop (thread safety)
from langchain_agent import create_langchain_tools
from demo_mode import emit_demo_thought, emit_plan_progress
import sys
from exceptions import BaseProjectSaneException, PlanningError, ExecutionError, BrowserError


# ── State Schema ──────────────────────────────────────────────────────────────

class GraphState(TypedDict):
    """Typed state container shared across all graph nodes."""

    # ── Inputs (set once at graph invocation) ─────────────────────────────────
    ticket_text: str
    ticket_info: dict
    base_url: str
    approved_plan: str
    groq_api_key: str    # for planner + reviewer nodes (text reasoning)
    gemini_api_key: str  # for executor node (multimodal vision checkpoints)
    job_id: str

    # ── Mutable runtime state ─────────────────────────────────────────────────
    attempt: int
    max_retries: int
    executor_result: str
    is_reproduced: bool
    feedback: str
    final_report: str
    final_findings: str


# ── Helper: provider engine factories ────────────────────────────────────────

def get_text_engine(groq_key: str) -> ChatGroq:
    """Planner & Reviewer: text-only Groq wrapper (llama-3.3-70b-versatile)."""
    return ChatGroq(
        model="llama-3.3-70b-versatile",
        groq_api_key=groq_key,
        temperature=0.1,
    )


def get_vision_engine(gemini_key: str) -> ChatGoogleGenerativeAI:
    """Executor: multimodal Gemini wrapper (gemini-2.5-flash)."""
    return ChatGoogleGenerativeAI(
        model="gemini-2.5-flash",
        google_api_key=gemini_key,
        temperature=0.1,
    )


async def safe_groq_invoke(prompt_payload, groq_api_key: str, tools: list = None):
    """
    Groq ainvoke with exponential backoff for rate-limit (429) / high-demand (503) errors.
    Used by planner_node and reviewer_node.
    """
    max_retries = 3
    for attempt in range(max_retries):
        try:
            llm = get_text_engine(groq_api_key)
            if tools:
                llm = llm.bind_tools(tools)
            return await llm.ainvoke(prompt_payload)

        except Exception as e:
            error_msg = str(e)
            if (
                "429" in error_msg or "rate_limit" in error_msg.lower()
                or "503" in error_msg or "UNAVAILABLE" in error_msg
            ) and attempt < max_retries - 1:
                import asyncio
                backoff = 2 ** attempt
                print(f"\n[SYSTEM WARN] Groq Rate Limit (attempt {attempt+1}/{max_retries}). "
                      f"Retrying in {backoff}s...")
                sys.stdout.flush()
                await asyncio.sleep(backoff)
                continue
            raise e


async def safe_gemini_invoke(prompt_payload, gemini_api_key: str, tools: list = None):
    """
    Gemini ainvoke with exponential backoff for quota (429 / RESOURCE_EXHAUSTED) errors.
    Used by executor_node for multimodal vision checkpoints.
    """
    max_retries = 3
    for attempt in range(max_retries):
        try:
            llm = get_vision_engine(gemini_api_key)
            if tools:
                llm = llm.bind_tools(tools)
            return await llm.ainvoke(prompt_payload)

        except Exception as e:
            error_msg = str(e)
            if (
                "429" in error_msg or "RESOURCE_EXHAUSTED" in error_msg
                or "503" in error_msg or "UNAVAILABLE" in error_msg
            ) and attempt < max_retries - 1:
                import asyncio
                backoff = 4 ** attempt  # longer backoff for Gemini quota
                print(f"\n[SYSTEM WARN] Gemini Quota Hit (attempt {attempt+1}/{max_retries}). "
                      f"Retrying in {backoff}s...")
                sys.stdout.flush()
                await asyncio.sleep(backoff)
                continue
            raise e


# Backward-compat alias — existing callers of safe_llm_invoke route to Groq.
async def safe_llm_invoke(prompt_payload, groq_api_key: str, tools: list = None):
    return await safe_groq_invoke(prompt_payload, groq_api_key, tools)


# ══════════════════════════════════════════════════════════════════════════════
# NODE FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

# Planner node removed. Planning is done authoritatively by Gemini/Gemma 3 prior to Graph execution.


def _demo_message_for_tool(tool_name: str, tool_args: dict) -> str:
    text = " ".join(str(v) for v in (tool_args or {}).values()).strip()
    target = text[:80] if text else "current page"
    if tool_name == "navigate_to_url":
        return "Opening Odoo page"
    if tool_name == "click_element":
        return f"Selecting {target}"
    if tool_name == "type_into_field":
        return f"Entering information in {target}"
    if tool_name == "take_screenshot":
        return "Verifying current page"
    if tool_name == "check_odoo_version":
        return "Verifying Odoo version"
    if tool_name == "get_installed_modules":
        return "Checking installed modules"
    if tool_name == "get_page_content":
        return "Reading visible page context"
    if tool_name == "search_past_tickets":
        return "Searching past resolutions"
    return f"Running {tool_name}"


async def executor_node(state: GraphState, browser=None) -> dict:
    """
    EXECUTOR NODE — Inspects the live Odoo sandbox and executes the plan.

    Uses Gemini (gemini-2.5-flash) for multimodal vision checkpoints only
    at navigation, configuration-change, submission, and explicit verification
    boundaries so demo runs remain observable without exhausting vision quota.
    Falls back to text-only mode if the browser page is unavailable.
    """
    current_attempt = state.get("attempt", 0) + 1
    feedback = state.get("feedback", "")

    feedback_section = ""
    if feedback:
        feedback_section = (
            f"\n\nPREVIOUS REVIEWER FEEDBACK (attempt {current_attempt - 1}):\n"
            f"{feedback}\n"
            "Adjust your approach based on this feedback."
        )

    # ── Fallback if browser is not available ──────────────────────────────────
    if browser is None or getattr(browser, "page", None) is None:
        print("[EXECUTOR] Running fallback text-only execution (no browser page available)")
        current_url = state["base_url"]
        user_prompt = (
            "You are an expert Odoo support executor running in a fallback text-only environment.\n"
            "Given the plan, describe how you would investigate this ticket.\n\n"
            f"CURRENT URL: {current_url}\n\n"
            f"TICKET TEXT:\n{state['ticket_text']}\n\n"
            f"INVESTIGATION PLAN:\n{state['approved_plan']}"
            f"{feedback_section}\n\n"
            "Describe what actions you would take and summarize your findings."
        )
        message = HumanMessage(content=user_prompt)
        try:
            # Fallback uses Gemini (text-only path — no image parts)
            response = await safe_gemini_invoke([message], state["gemini_api_key"])
            result_text = response.content if hasattr(response, "content") else str(response)
        except Exception as e:
            import traceback
            error_detail = traceback.format_exc()
            result_text = f"Executor failed: {e}\n\nDetail:\n{error_detail}"
        
        return {
            "executor_result": result_text,
            "attempt": current_attempt,
        }

    # ── ReAct Tool-Calling Agent Loop ─────────────────────────────────────────
    tools = create_langchain_tools(browser.page, browser, state["base_url"])

    current_url = browser.page.url or state["base_url"]

    # ── Helper: capture screenshot and return base64 string ───────────────────
    async def _capture_screenshot(label: str) -> Optional[str]:
        """Saves a PNG to disk and returns its base64 string for vision embedding."""
        try:
            os.makedirs("output", exist_ok=True)
            path = f"output/graph_step_attempt_{current_attempt}_{label}_{int(time.time())}.png"
            await browser.page.screenshot(path=path, full_page=True)
            if getattr(browser, "screenshots", None) is None:
                browser.screenshots = []
            if path not in browser.screenshots:
                browser.screenshots.append(path)
            with open(path, "rb") as f:
                return base64.b64encode(f.read()).decode("utf-8")
        except Exception as e:
            print(f"[EXECUTOR] Screenshot capture failed ({label}): {e}")
            return None

    # ── Capture starting state with vision grounding ──────────────────────────
    initial_b64 = await _capture_screenshot("start")

    # Build the opening multimodal message (text + optional screenshot)
    vision_intro_parts = [
        {
            "type": "text",
            "text": (
                "You are an expert Odoo support executor running inside a live browser sandbox.\n"
                "You have access to browser tools to navigate, click, type, and inspect the Odoo database.\n"
                "Execute the investigation plan against the real database state.\n\n"
                "Your goal is to investigate, reproduce, and locate the error reported in the ticket.\n"
                "Follow these rules:\n"
                "1. Execute the plan step-by-step using the available tools.\n"
                "2. Call tools as needed. Vision screenshots are checkpointed after navigation, "
                "configuration changes, form submissions, and verification checks. Routine clicks "
                "do not trigger vision checkpoints.\n"
                "3. Avoid guessing or hallucinating database content. Use the tools to verify everything.\n"
                "4. If you encounter an error, diagnose its cause from the visual and text context.\n"
                "5. Once investigation is complete or the issue is reproduced, output your final findings "
                "and stop calling tools.\n\n"
                f"CURRENT URL: {current_url}\n\n"
                f"TICKET TEXT:\n{state['ticket_text']}\n\n"
                f"INVESTIGATION PLAN:\n{state['approved_plan']}"
                f"{feedback_section}"
            ),
        }
    ]
    if initial_b64:
        vision_intro_parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{initial_b64}"},
        })
        print(f"[EXECUTOR] Initial page screenshot embedded for Gemini vision grounding.")

    messages = [HumanMessage(content=vision_intro_parts)]

    max_tool_calls = 15
    tool_calls_count = 0
    execution_log = []
    tool_map = {t.name: t for t in tools}

    def _is_vision_checkpoint(tool_name: str, result: str, args: dict) -> bool:
        if tool_name in {"navigate_to_url", "check_odoo_version", "get_installed_modules", "take_screenshot"}:
            return True
        haystack = f"{tool_name} {args} {result}".lower()
        checkpoint_terms = (
            "save", "submit", "apply", "confirm", "create", "update", "configure",
            "settings", "verify", "validation", "form", "posted", "changed"
        )
        if tool_name == "type_into_field" and any(term in haystack for term in ("settings", "config", "search", "filter")):
            return True
        if tool_name == "click_element" and any(term in haystack for term in checkpoint_terms):
            return True
        return False

    response = None
    while tool_calls_count < max_tool_calls:
        try:
            response = await safe_gemini_invoke(messages, state["gemini_api_key"], tools=tools)
        except Exception as e:
            import traceback
            error_detail = traceback.format_exc()
            print(f"[EXECUTOR ERROR] LLM invocation failed: {e}")
            print(error_detail)
            execution_log.append(f"LLM Error: {e}")
            break

        messages.append(response)

        if response.tool_calls:
            for tool_call in response.tool_calls:
                tool_name = tool_call["name"]
                tool_args = tool_call["args"]
                tool_call_id = tool_call["id"]

                tool_calls_count += 1
                if tool_calls_count > max_tool_calls:
                    print(f"[REACT LOOP] Tool call limit reached ({max_tool_calls}). Stopping.")
                    execution_log.append(f"Reached safety limit of {max_tool_calls} tool calls.")
                    break

                print(f"[REACT LOOP] Calling tool: {tool_name} with args {tool_args}")
                await emit_demo_thought(browser, _demo_message_for_tool(tool_name, tool_args), step_id=tool_calls_count)
                await emit_plan_progress(browser, tool_calls_count, "active", f"{tool_name}: {tool_args}")
                
                # Execute tool
                if tool_name in tool_map:
                    try:
                        tool_obj = tool_map[tool_name]
                        result = await tool_obj.ainvoke(tool_args)
                        log_entry = f"Tool Call {tool_calls_count}: {tool_name}({tool_args}) -> {result}"
                        print(f"[REACT LOOP] Tool result: {result}")
                    except Exception as e:
                        import traceback
                        result = f"Error executing tool {tool_name}: {e}"
                        print(f"[REACT LOOP ERROR] {result}\n{traceback.format_exc()}")
                        log_entry = f"Tool Call {tool_calls_count}: {tool_name}({tool_args}) -> Error: {e}"
                else:
                    result = f"Error: Tool {tool_name} not found."
                    log_entry = f"Tool Call {tool_calls_count}: {tool_name}({tool_args}) -> {result}"
                    print(f"[REACT LOOP ERROR] {result}")

                execution_log.append(log_entry)
                await emit_plan_progress(browser, tool_calls_count, "done", str(result)[:220])

                # Append tool result as text ToolMessage
                tool_message = ToolMessage(
                    tool_call_id=tool_call_id,
                    name=tool_name,
                    content=f"Result: {result}"
                )
                messages.append(tool_message)

                # ── Vision Checkpoint ─────────────────────────────────────────
                # After key browser-action tools, embed a fresh screenshot so
                # Gemini can visually validate the new page state.
                if _is_vision_checkpoint(tool_name, str(result), tool_args):
                    post_b64 = await _capture_screenshot(f"tool_{tool_calls_count}")
                    if post_b64:
                        vision_msg = HumanMessage(content=[
                            {
                                "type": "text",
                                "text": (
                                    f"[VISION CHECKPOINT] Action '{tool_name}' completed. "
                                    f"Current page screenshot follows — use it to decide the next step."
                                ),
                            },
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/png;base64,{post_b64}"},
                            },
                        ])
                        messages.append(vision_msg)
                        print(f"[EXECUTOR] Vision checkpoint embedded after '{tool_name}' (tool #{tool_calls_count}).")

            # Continue the loop
            continue
        else:
            # No tool calls: model has finished and returned text
            break

    # Construct the final result text
    final_text = response.content if (response and hasattr(response, "content")) else ""
    log_summary = "\n".join(execution_log)
    result_text = (
        f"=== TOOL EXECUTION LOG ===\n"
        f"{log_summary}\n\n"
        f"=== FINAL INVESTIGATION REPORT ===\n"
        f"{final_text}"
    )

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

    response = await safe_groq_invoke(prompt, state["groq_api_key"])
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
            findings_response = await safe_groq_invoke(findings_prompt, state["groq_api_key"])
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
            approved_plan=..., groq_api_key=..., gemini_api_key=..., job_id=...,
        )
    """

    def __init__(self, browser_instance=None):
        """Ingests the live BrowserManager (page + screenshots) for the executor."""
        self.browser = browser_instance

        builder = StateGraph(GraphState)

        # Closure pass-through so the executor node receives the live browser
        # context. Must be async so LangGraph awaits the node coroutine.
        async def executor_wrapper(state: GraphState) -> dict:
            return await executor_node(state, self.browser)

        builder.add_node("executor", executor_wrapper)
        builder.add_node("reviewer", reviewer_node)

        # Define edges
        builder.set_entry_point("executor")
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
        groq_api_key: str,
        gemini_api_key: str = "",
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
            "groq_api_key": groq_api_key,
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
