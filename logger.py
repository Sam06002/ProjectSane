"""
logger.py — Structured run logger for Project Sane v2 (enhanced observability).

Creates a complete, machine-readable audit trail of every pipeline execution.
No external dependencies — uses only standard Python (json, os, time, traceback).

Output layout:
    logs/
        run_<timestamp>.json
        screenshots/
            step_<step_id>.png

All behavior must be reconstructable from logs alone.

Fix list applied (v2):
  #1  final_payload in llm_calls
  #2  plan snapshot (top-level "plan" key)
  #3  execution_gate decision log
  #4  steps / actions sorted by step_id before save()
  #5  browser entries always carry "screenshot" field
  #8  metrics summary computed at save()
  #9  error enrichment (selector, stack trace always present)
  #10 log-size control: DEBUG_FULL_LOG flag + 5 000-char truncation
"""

import json
import os
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# ── Fix #10: log-size control flag ───────────────────────────────────────────
# Set to False in production to aggressively truncate prompts/responses.
DEBUG_FULL_LOG: bool = os.getenv("DEBUG_FULL_LOG", "true").lower() != "false"

_FULL_MAX_CHARS   = 5_000   # per field, when DEBUG_FULL_LOG=True
_COMPACT_MAX_CHARS = 500    # per field, when DEBUG_FULL_LOG=False


def _trunc(text: str, label: str = "") -> str:
    """Truncate a string to the current log-size limit and mark it."""
    limit = _FULL_MAX_CHARS if DEBUG_FULL_LOG else _COMPACT_MAX_CHARS
    if len(text) <= limit:
        return text
    return text[:limit] + f"... [TRUNCATED {len(text) - limit} chars | field={label}]"


class RunLogger:
    """
    Central observability object for one pipeline run.

    Instantiate once per request, pass the same instance to every component
    (server, ai_agent, executor, browser_agent), call save() at the end.
    """

    def __init__(self):
        self._run_id: str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        self.run_id: str = self._run_id
        self._started_at: float = time.time()

        # ── Structured payload ────────────────────────────────────────────────
        self.data: Dict[str, Any] = {
            "run_id": self._run_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "duration_ms": None,
            "debug_full_log": DEBUG_FULL_LOG,
            # 1. INPUT LAYER
            "input": {},
            # 2. LLM INTERACTION LAYER
            "llm_calls": [],
            # Fix #2: plan snapshot — populated by log_plan()
            "plan": None,
            # Fix #3: execution gate — populated by log_execution_gate()
            "execution_gate": None,
            # 3. REASONING TRACE LAYER
            "steps": [],
            # 4. EXECUTION LAYER
            "actions": [],
            # 5. ERROR LAYER
            "errors": [],
            # 6. BROWSER STATE LAYER
            "browser": [],
            # 7. DOCUMENTATION CONTEXT
            "doc_urls": [],
            # Fix #8: metrics — computed at save()
            "metrics": {},
        }

        # Ensure output directories exist
        os.makedirs("logs/screenshots", exist_ok=True)

    # ── 1. INPUT LAYER ────────────────────────────────────────────────────────
    def log_input(
        self,
        ticket_text: str,
        db_url: str,
        extracted: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Log the raw analyst input and the fields extracted from the ticket.

        Args:
            ticket_text: The full raw ticket text submitted by the analyst.
            db_url:      The target Odoo database URL.
            extracted:   Dict of fields extracted by the LLM (module, version, etc.).
        """
        self.data["input"] = {
            "ticket_text": _trunc(ticket_text, "ticket_text"),
            "db_url": db_url,
            "char_count": len(ticket_text),
            "extracted": extracted or {},
        }

    # ── 2. LLM INTERACTION LAYER — Fix #1 (final_payload) + Fix #10 ──────────
    def log_llm_call(
        self,
        model: str,
        provider: str,
        system_prompt: str,
        user_prompt: str,
        raw_response: str,
        latency_ms: float,
        purpose: str = "",
        error: Optional[str] = None,
        final_payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Log one complete LLM round-trip.

        Args:
            model:          Exact model identifier string (e.g. 'google/gemma-3-4b').
            provider:       Provider name ('lmstudio', 'groq', 'gemini').
            system_prompt:  Full system prompt sent.
            user_prompt:    Full user message sent.
            raw_response:   Complete response text returned by the model.
            latency_ms:     Wall-clock time from request start to last token, in ms.
            purpose:        Human label (e.g. 'ticket_analysis').
            error:          Error string if the call failed, else None.
            final_payload:  Fix #1 — the exact dict sent to the model endpoint.
        """
        # Fix #10: safe truncation
        sys_trunc  = _trunc(system_prompt, "system_prompt")
        user_trunc = _trunc(user_prompt,   "user_prompt")
        resp_trunc = _trunc(raw_response,  "raw_response")

        # Fix #1: serialise the final payload sent to the model
        payload_str: Optional[str] = None
        if final_payload is not None:
            try:
                payload_str = json.dumps(final_payload, ensure_ascii=False)
                payload_str = _trunc(payload_str, "final_payload")
            except Exception:
                payload_str = "<serialisation error>"

        self.data["llm_calls"].append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "purpose": purpose,
            "provider": provider,
            "model": model,
            "system_prompt_chars": len(system_prompt),
            "user_prompt_chars": len(user_prompt),
            "system_prompt": sys_trunc,
            "user_prompt": user_trunc,
            "raw_response": resp_trunc,
            "response_chars": len(raw_response),
            "latency_ms": round(latency_ms, 1),
            "error": error,
            # Fix #1
            "final_payload": payload_str,
        })

    # ── Fix #2: PLAN SNAPSHOT ─────────────────────────────────────────────────
    def log_plan(self, plan_dict: Dict[str, Any]) -> None:
        """
        Store the full structured plan before execution starts.

        Args:
            plan_dict: The complete plan object serialised to a plain dict
                       (e.g. plan.dict() from the Pydantic model).
        """
        self.data["plan"] = plan_dict

    # ── Fix #3: EXECUTION GATE ────────────────────────────────────────────────
    def log_execution_gate(
        self,
        confidence: float,
        threshold: float = 0.6,
        executed: bool = True,
        reason: str = "",
    ) -> None:
        """
        Record the go/no-go decision before the execution loop starts.

        Args:
            confidence: The plan confidence score.
            threshold:  The minimum confidence required to execute.
            executed:   True if execution proceeded, False if aborted.
            reason:     Optional human note explaining the decision.
        """
        self.data["execution_gate"] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "confidence": confidence,
            "threshold": threshold,
            "executed": executed,
            "reason": reason,
        }

    # ── 3. REASONING TRACE LAYER ──────────────────────────────────────────────
    def log_step(
        self,
        step_id: int,
        intent: str,
        reasoning: str,
        action_type: str,
        action_target: str,
        expected_outcome: str,
        fallback: str,
        confidence: Optional[float] = None,
    ) -> None:
        """Log the reasoning behind a plan step before it is executed."""
        self.data["steps"].append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "step_id": step_id,
            "intent": intent,
            "reasoning": reasoning,
            "action_type": action_type,
            "action_target": action_target,
            "expected_outcome": expected_outcome,
            "fallback": fallback,
            "confidence": confidence,
        })

    # ── 4. EXECUTION LAYER — Fix #6 (elements_found) + Fix #7 (retry) ────────
    def log_action(
        self,
        step_id: int,
        action_type: str,
        target: str,
        selector_used: Optional[str],
        success: bool,
        message: str,
        duration_ms: float,
        retry_count: int = 0,
        retry_attempts: Optional[List[Dict[str, Any]]] = None,
        extracted_text: Optional[str] = None,
        screenshot_path: Optional[str] = None,
        elements_found: Optional[int] = None,
    ) -> None:
        """
        Log the concrete outcome of executing one action.

        Args:
            step_id:          Which step this action belongs to.
            action_type:      Action type string.
            target:           The human-readable target that was resolved.
            selector_used:    The actual CSS selector resolved from the registry.
            success:          Whether the action succeeded.
            message:          Result message from the executor.
            duration_ms:      How long the action took in ms.
            retry_count:      Number of retries before this result.
            retry_attempts:   Fix #7 — list of {attempt, status} dicts.
            extracted_text:   Text extracted (for extract action type).
            screenshot_path:  Path to screenshot if taken.
            elements_found:   Fix #6 — count of elements matched by the selector.
        """
        # Fix #7: structured retry trace
        retry_block: Optional[Dict[str, Any]] = None
        if retry_count > 0:
            retry_block = {
                "count": retry_count,
                "attempts": retry_attempts or [],
            }

        self.data["actions"].append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "step_id": step_id,
            "action_type": action_type,
            "target": target,
            # Fix #6: selector evidence
            "selector": selector_used,
            "elements_found": elements_found,
            "success": success,
            "message": message,
            "duration_ms": round(duration_ms, 1),
            # Fix #7: retry trace
            "retry": retry_block,
            "extracted_text": extracted_text,
            "screenshot_path": screenshot_path,
        })

    # ── 5. ERROR LAYER — Fix #9 (selector + stack always present) ────────────
    def log_error(
        self,
        message: str,
        step_id: Optional[int] = None,
        exc: Optional[BaseException] = None,
        context: str = "",
        selector: Optional[str] = None,
    ) -> None:
        """
        Log a structured error with optional stack trace.

        Args:
            message:   Human-readable error description.
            step_id:   Step that was executing when the error occurred, if any.
            exc:       The Python exception object, if available.
            context:   Short string describing where in the pipeline this occurred.
            selector:  Fix #9 — CSS selector in use when the error occurred.
        """
        stack: Optional[List[str]] = None
        if exc is not None:
            stack = traceback.format_exception(type(exc), exc, exc.__traceback__)

        self.data["errors"].append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "step_id": step_id,
            "context": context,
            "message": message,
            # Fix #9
            "selector": selector,
            "stack_trace": stack,
        })

    # ── 6. BROWSER STATE LAYER — Fix #5 (always explicit screenshot key) ──────
    def log_browser_state(
        self,
        url: str,
        step_id: Optional[int] = None,
        screenshot_path: Optional[str] = None,
        event: str = "",
    ) -> None:
        """
        Log the browser's current state after a navigation or action.

        Fix #5: the screenshot field is always present (None when not captured).

        Args:
            url:             Current page URL.
            step_id:         Which step triggered this state capture, if any.
            screenshot_path: Path where the screenshot was saved.
            event:           Short label (e.g. 'post_navigate', 'post_step').
        """
        self.data["browser"].append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "step_id": step_id,
            "event": event,
            "url": url,
            # Fix #5: always explicitly present
            "screenshot": screenshot_path,
        })

    # ── 7. DOCUMENTATION CONTEXT ──────────────────────────────────────────────
    def log_doc_urls(self, urls: List[str]) -> None:
        """Record documentation URLs referenced during the run."""
        for url in urls:
            if url not in self.data["doc_urls"]:
                self.data["doc_urls"].append(url)

    # ── 8. ENGINE 2 OBSERVABILITY (Gemini AI Studio) ────────────────────────
    def log_engine2_rate_limit(
        self,
        attempt: int,
        wait_s: int,
        error: str = "",
    ) -> None:
        """
        Log a Gemini 429 / quota-exhausted event with retry metadata.

        Args:
            attempt: Which retry attempt (1-indexed).
            wait_s:  Seconds the system will wait before the next attempt.
            error:   Raw error string from the SDK.
        """
        self.data["errors"].append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "step_id": None,
            "context": "engine2_rate_limit",
            "message": (
                f"Gemini rate limit hit on attempt {attempt}. "
                f"Retrying in {wait_s}s."
            ),
            "selector": None,
            "stack_trace": None,
            "engine": "gemini",
            "model": "gemini-2.5-flash",
            "retry_attempt": attempt,
            "wait_seconds": wait_s,
            "raw_error": _trunc(error, "engine2_rate_limit_error"),
        })

    def log_engine2_schema_exception(
        self,
        context: str = "",
        error: str = "",
    ) -> None:
        """
        Log a Gemini schema / response_schema validation failure.

        Args:
            context: Which pipeline step triggered the failure.
            error:   Raw error string from the SDK.
        """
        self.data["errors"].append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "step_id": None,
            "context": f"engine2_schema_exception | {context}",
            "message": "Gemini returned output that did not match the expected schema.",
            "selector": None,
            "stack_trace": None,
            "engine": "gemini",
            "model": "gemini-2.5-flash",
            "raw_error": _trunc(error, "engine2_schema_error"),
        })

    # ── SCREENSHOT HELPER — Fix #10 (compression) ────────────────────────────
    async def capture_screenshot(self, page: Any, step_id: int) -> Optional[str]:
        """
        Take a Playwright screenshot and save it to logs/screenshots/step_<id>.png.

        Fix #10: uses quality=60 (JPEG compression) when DEBUG_FULL_LOG=False.
        Playwright only supports quality for JPEG; PNG is lossless — we use JPEG
        extension when compressing.

        Args:
            page:    The active Playwright Page object.
            step_id: Step number used to name the file.

        Returns:
            The relative path string on success, or None on failure.
        """
        if DEBUG_FULL_LOG:
            path = f"logs/screenshots/step_{step_id}.png"
            kwargs: Dict[str, Any] = {"path": path, "timeout": 8000}
        else:
            path = f"logs/screenshots/step_{step_id}.jpg"
            kwargs = {"path": path, "timeout": 8000, "type": "jpeg", "quality": 60}

        try:
            await page.screenshot(**kwargs)
            return path
        except Exception as exc:
            self.log_error(
                message=f"Screenshot failed for step {step_id}: {exc}",
                step_id=step_id,
                exc=exc,
                context="capture_screenshot",
            )
            return None

    # ── INVARIANT CHECKS (Task 8) ─────────────────────────────────────────────
    def _run_invariant_checks(self) -> bool:
        """
        Validate internal consistency of the accumulated log data.

        Checks:
          1. step_ids are sequential with no gaps (1, 2, 3 …)
          2. every logged step has a corresponding action entry
          3. every executed step that produced a screenshot path has
             that file actually present on disk

        On any violation: appends to self.data["errors"] and returns False.
        The log is always written regardless — invariant failures are surfaced
        inside the JSON, not by suppressing the file.

        Returns:
            True if all invariants hold, False if any violation was found.
        """
        violations: List[str] = []

        steps   = self.data["steps"]
        actions = self.data["actions"]

        step_ids  = [s["step_id"] for s in steps]
        action_ids = {a["step_id"] for a in actions}

        # ── Invariant 1: steps are sequential, starting at 1, no duplicates ──
        if step_ids:
            expected = list(range(1, len(step_ids) + 1))
            if sorted(step_ids) != expected:
                violations.append(
                    f"Invariant1: step_ids are not sequential. "
                    f"Got {sorted(step_ids)}, expected {expected}."
                )

        # ── Invariant 2: every step has a corresponding action ────────────────
        missing_actions = [sid for sid in step_ids if sid not in action_ids]
        if missing_actions:
            violations.append(
                f"Invariant2: steps {missing_actions} have no corresponding "
                f"action entry. Possible executor failure before log_action()."
            )

        # ── Invariant 3: screenshot files exist for steps that recorded one ──
        missing_files: List[str] = []
        for action in actions:
            spath = action.get("screenshot_path")
            if spath and not os.path.isfile(spath):
                missing_files.append(spath)
        if missing_files:
            violations.append(
                f"Invariant3: {len(missing_files)} screenshot file(s) logged "
                f"but not found on disk: {missing_files[:5]}"  # cap at 5 for brevity
            )

        # ── Record violations as internal errors ──────────────────────────────
        if violations:
            for v in violations:
                self.data["errors"].append({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "step_id": None,
                    "context": "invariant_check",
                    "message": v,
                    "selector": None,
                    "stack_trace": None,
                })
            return False
        return True

    # ── PERSISTENCE — Fix #4 (sort), Fix #8 (metrics), Task 8 (invariants) ───
    def save(self) -> str:
        """
        Finalise timing, run invariant checks, sort steps/actions, compute
        metrics, then write JSON.

        Fix #4:   sorts steps and actions by step_id for deterministic order.
        Fix #8:   computes metrics summary from raw data at save time.
        Task 8:   runs invariant checks; marks run_valid=False on violation.

        The file is ALWAYS written — even when invalid — so that the log is
        never lost and violations are fully inspectable.

        Returns:
            The path of the written file.
        """
        self.data["finished_at"] = datetime.now(timezone.utc).isoformat()
        self.data["duration_ms"] = round((time.time() - self._started_at) * 1000, 1)

        # Fix #4: deterministic step/action order (must happen before invariants)
        self.data["steps"]   = sorted(self.data["steps"],   key=lambda s: s.get("step_id", 0))
        self.data["actions"] = sorted(self.data["actions"], key=lambda a: a.get("step_id", 0))

        # Task 8: invariant checks — appends to errors[] on failure
        self.data["run_valid"] = self._run_invariant_checks()

        # Fix #8: metrics recomputed from raw data (includes any new errors added above)
        total   = len(self.data["actions"])
        success = sum(1 for a in self.data["actions"] if a.get("success"))
        self.data["metrics"] = {
            "total_steps":       total,
            "successful_steps":  success,
            "failed_steps":      total - success,
            "total_llm_calls":   len(self.data["llm_calls"]),
            "total_errors":      len(self.data["errors"]),
            "screenshots_taken": sum(
                1 for b in self.data["browser"] if b.get("screenshot")
            ),
            "invariants_passed": self.data["run_valid"],
        }

        path = f"logs/run_{self._run_id}.json"
        os.makedirs("logs", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=2, ensure_ascii=False, default=str)

        validity = "VALID" if self.data["run_valid"] else "INVALID (see errors[])"
        print(f"[RunLogger] Saved run log [{validity}]: {path}")
        return path
