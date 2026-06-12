"""
monitor.py — Real-Time Observation Layer for Project Sane v3.

Wraps RunLogger with active monitoring capabilities:
  - Collects errors and warnings in-memory during a run
  - Emits structured SSE events immediately when errors occur
  - Produces a run summary (engine stats, error counts, timeline)
  - Exposes run history for the /runs dashboard

Usage in server.py:
    monitor = ObservationLayer(run_logger=run_log, sse_emitter=yield_fn)
    await monitor.observe_browser_start(page)
    await monitor.record_error("context", exc)
    summary = monitor.finalise()
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from logger import RunLogger


# ── Run History index ──────────────────────────────────────────────────────────
# Stored as a simple JSON list in logs/run_index.json
RUNS_INDEX_PATH = "logs/run_index.json"


def _load_index() -> List[Dict[str, Any]]:
    if not os.path.exists(RUNS_INDEX_PATH):
        return []
    try:
        with open(RUNS_INDEX_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_index(entries: List[Dict[str, Any]]) -> None:
    os.makedirs("logs", exist_ok=True)
    with open(RUNS_INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False, default=str)


# ── ObservationLayer ───────────────────────────────────────────────────────────

class ObservationLayer:
    """
    Active monitoring wrapper around RunLogger.

    Designed to be instantiated once per pipeline run and passed to every
    component that can generate errors. Components call the `record_*` methods;
    the observation layer logs to RunLogger AND immediately emits an SSE event
    so the analyst sees the error in real time.
    """

    def __init__(
        self,
        run_logger: RunLogger,
        sse_emitter: Optional[Callable[[str], Any]] = None,
        ticket_text: str = "",
        db_url: str = "",
    ):
        """
        Args:
            run_logger:  The RunLogger instance for this pipeline run.
            sse_emitter: An async callable that accepts a formatted SSE string
                         and yields it to the client. If None, errors are only
                         written to disk.
            ticket_text: Raw ticket text (for index metadata).
            db_url:      Database URL (for index metadata).
        """
        self._log = run_logger
        self._emit = sse_emitter
        self._ticket_text = ticket_text
        self._db_url = db_url

        # ── In-memory error/warning/event ledger ─────────────────────────────
        self.errors:   List[Dict[str, Any]] = []
        self.warnings: List[Dict[str, Any]] = []
        self.events:   List[Dict[str, Any]] = []

        # ── Engine call counters ──────────────────────────────────────────────
        self.engine1_calls: int = 0   # Groq
        self.engine2_calls: int = 0   # Gemini
        self.engine1_errors: int = 0
        self.engine2_errors: int = 0
        self.engine2_rate_limits: int = 0

        # ── Timing ────────────────────────────────────────────────────────────
        self._started_at: float = time.time()
        self.run_id: str = run_logger._run_id

    # ── SSE emitter helper ────────────────────────────────────────────────────

    async def _sse(self, event_type: str, payload: Dict[str, Any]) -> None:
        """Format and emit an SSE event immediately if an emitter is wired in."""
        if self._emit is None:
            return
        try:
            msg = f"event: {event_type}\ndata: {json.dumps(payload)}\n\n"
            await self._emit(msg)
        except Exception:
            pass  # Never let the observation layer crash the pipeline

    # ── Error recording ───────────────────────────────────────────────────────

    async def record_error(
        self,
        message: str,
        context: str = "",
        step_id: Optional[int] = None,
        exc: Optional[BaseException] = None,
        selector: Optional[str] = None,
    ) -> None:
        """
        Record a pipeline error to disk AND emit it to the live UI immediately.

        Args:
            message:  Human-readable error description.
            context:  Short label for where in the pipeline this occurred.
            step_id:  Execution step number, if applicable.
            exc:      Python exception object, if available.
            selector: CSS selector in use when the error occurred, if applicable.
        """
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "context": context,
            "message": message,
            "step_id": step_id,
        }
        self.errors.append(entry)

        # Write to structured log
        self._log.log_error(
            message=message,
            step_id=step_id,
            exc=exc,
            context=context,
            selector=selector,
        )

        # Emit to live UI
        await self._sse("monitor_error", {
            "run_id": self.run_id,
            "context": context,
            "message": message,
            "step_id": step_id,
            "timestamp": entry["timestamp"],
        })

    async def record_warning(self, message: str, context: str = "") -> None:
        """Record a non-fatal warning — logged and streamed but not counted as error."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "context": context,
            "message": message,
        }
        self.warnings.append(entry)
        await self._sse("monitor_warning", {
            "run_id": self.run_id,
            "context": context,
            "message": message,
        })

    async def record_event(self, label: str, detail: str = "") -> None:
        """Record a positive milestone event (e.g. 'DB entered', 'Blueprint ready')."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "label": label,
            "detail": detail,
        }
        self.events.append(entry)
        await self._sse("monitor_event", {
            "run_id": self.run_id,
            "label": label,
            "detail": detail,
        })

    # ── Engine-specific observers ─────────────────────────────────────────────

    async def on_engine1_call(self, purpose: str) -> None:
        """Called whenever Engine 1 (Groq) starts a call."""
        self.engine1_calls += 1
        await self._sse("monitor_engine", {
            "engine": 1, "model": "llama-3.1-8b-instant",
            "purpose": purpose, "call_number": self.engine1_calls,
        })

    async def on_engine1_error(self, error: str, purpose: str) -> None:
        """Called when Engine 1 fails."""
        self.engine1_errors += 1
        await self.record_error(
            message=f"Engine 1 (Groq) error on '{purpose}': {error}",
            context="engine1_groq",
        )

    async def on_engine2_call(self, purpose: str) -> None:
        """Called whenever Engine 2 (Gemini) starts a call."""
        self.engine2_calls += 1
        await self._sse("monitor_engine", {
            "engine": 2, "model": "gemini-2.5-flash",
            "purpose": purpose, "call_number": self.engine2_calls,
        })

    async def on_engine2_rate_limit(self, attempt: int, wait_s: int, error: str) -> None:
        """Called when Engine 2 hits a 429 rate limit."""
        self.engine2_rate_limits += 1
        self._log.log_engine2_rate_limit(attempt=attempt, wait_s=wait_s, error=error)
        await self._sse("monitor_warning", {
            "run_id": self.run_id,
            "context": "engine2_rate_limit",
            "message": (
                f"Gemini rate limit on attempt {attempt}. "
                f"Waiting {wait_s}s before retry."
            ),
        })

    async def on_engine2_schema_error(self, context: str, error: str) -> None:
        """Called when Engine 2 returns output that fails schema validation."""
        self.engine2_errors += 1
        self._log.log_engine2_schema_exception(context=context, error=error)
        await self.record_error(
            message=f"Engine 2 (Gemini) schema exception in '{context}': {error}",
            context="engine2_schema",
        )

    async def on_browser_error(self, message: str, context: str = "browser") -> None:
        """Called on any Playwright / CDP error."""
        await self.record_error(message=message, context=context)

    async def on_planner_error(self, message: str) -> None:
        """Called when the Planner fails to produce a valid plan."""
        await self.record_error(message=message, context="planner")

    async def on_step_failure(self, step_id: int, message: str) -> None:
        """Called when an ExecutionEngine step fails."""
        await self.record_error(
            message=message, context="executor_step", step_id=step_id
        )

    # ── Run summary ───────────────────────────────────────────────────────────

    def finalise(self) -> Dict[str, Any]:
        """
        Build a complete run summary, save to RunLogger, and register
        this run in the persistent run index.

        Returns the summary dict (also emitted via SSE as 'monitor_summary').
        """
        duration_s = round(time.time() - self._started_at, 1)

        summary = {
            "run_id": self.run_id,
            "started_at": datetime.fromtimestamp(
                self._started_at, tz=timezone.utc
            ).isoformat(),
            "duration_seconds": duration_s,
            "ticket_preview": self._ticket_text[:120],
            "db_url": self._db_url,
            "total_errors": len(self.errors),
            "total_warnings": len(self.warnings),
            "total_events": len(self.events),
            "engine1_calls": self.engine1_calls,
            "engine1_errors": self.engine1_errors,
            "engine2_calls": self.engine2_calls,
            "engine2_errors": self.engine2_errors,
            "engine2_rate_limits": self.engine2_rate_limits,
            "run_valid": len(self.errors) == 0,
            "errors": self.errors,
            "warnings": self.warnings,
            "events": self.events,
            "log_file": f"logs/run_{self.run_id}.json",
        }

        # Register in the run index for the /runs dashboard
        _register_run(summary)

        return summary


def _register_run(summary: Dict[str, Any]) -> None:
    """Append a lightweight entry to the run index file."""
    index = _load_index()

    # Keep only the last 100 runs in the index
    entry = {
        "run_id":          summary["run_id"],
        "started_at":      summary["started_at"],
        "duration_seconds": summary["duration_seconds"],
        "ticket_preview":  summary["ticket_preview"],
        "db_url":          summary["db_url"],
        "total_errors":    summary["total_errors"],
        "total_warnings":  summary["total_warnings"],
        "run_valid":       summary["run_valid"],
        "log_file":        summary["log_file"],
    }

    index = [e for e in index if e.get("run_id") != entry["run_id"]]
    index.insert(0, entry)
    _save_index(index[:100])


# ── Public helper for /runs dashboard ─────────────────────────────────────────

def get_run_history(limit: int = 20) -> List[Dict[str, Any]]:
    """Return the most recent `limit` run summaries from the index."""
    return _load_index()[:limit]


def get_run_detail(run_id: str) -> Optional[Dict[str, Any]]:
    """Load the full JSON log for a specific run_id."""
    path = f"logs/run_{run_id}.json"
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None
