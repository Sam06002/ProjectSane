"""
demo_mode.py - Observable automation settings and SSE helpers.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from stream_manager import StreamManager


DEMO_MODE = os.getenv("DEMO_MODE", "false").strip().lower() in {"1", "true", "yes", "on"}
DEMO_ACTION_DELAY_MS = int(os.getenv("DEMO_ACTION_DELAY_MS", "1000") if DEMO_MODE else os.getenv("DEMO_ACTION_DELAY_MS", "650"))
DEMO_CURSOR_STEP_DELAY_MS = int(os.getenv("DEMO_CURSOR_STEP_DELAY_MS", "18"))
DEMO_HIGHLIGHT_MS = int(os.getenv("DEMO_HIGHLIGHT_MS", "550"))


def demo_settings() -> Dict[str, Any]:
    return {
        "enabled": DEMO_MODE,
        "action_delay_ms": DEMO_ACTION_DELAY_MS,
        "cursor_step_delay_ms": DEMO_CURSOR_STEP_DELAY_MS,
        "highlight_ms": DEMO_HIGHLIGHT_MS,
    }


async def emit_demo_thought(browser: Any, message: str, step_id: int = 0) -> None:
    emitter = getattr(browser, "sse_emitter", None)
    if not emitter:
        return
    await emitter(StreamManager.emit_demo_thought(message, step_id=step_id))


async def emit_plan_progress(browser: Any, step_id: int, status: str, message: Optional[str] = None) -> None:
    emitter = getattr(browser, "sse_emitter", None)
    if not emitter:
        return
    await emitter(StreamManager.emit_plan_progress(step_id=step_id, status=status, message=message))
