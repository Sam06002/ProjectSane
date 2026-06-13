"""
stream_manager.py — SSE Event Formatting for Real-Time UI Updates.
"""

import json
from typing import Any, AsyncGenerator, Dict

class StreamManager:
    @staticmethod
    def format_sse(event_type: str, data: Dict[str, Any]) -> str:
        """Formats data into a Server-Sent Event string."""
        payload = json.dumps(data)
        return f"event: {event_type}\ndata: {payload}\n\n"

    @staticmethod
    def emit_thinking(step_id: int, intent: str, reasoning: str, **kwargs) -> str:
        data = {
            "step_id": step_id,
            "intent": intent,
            "reasoning": reasoning
        }
        data.update(kwargs)
        return StreamManager.format_sse("thinking_step", data)

    @staticmethod
    def emit_action_start(step_id: int, action_type: str, target: str) -> str:
        return StreamManager.format_sse("action_start", {
            "step_id": step_id,
            "action": action_type,
            "target": target
        })

    @staticmethod
    def emit_action_result(step_id: int, success: bool, message: str, extracted_text: str = None) -> str:
        data = {
            "step_id": step_id,
            "success": success,
            "message": message
        }
        if extracted_text:
            data["extracted_text"] = extracted_text
        return StreamManager.format_sse("action_result", data)

    @staticmethod
    def emit_error(message: str) -> str:
        return StreamManager.format_sse("error", {"message": message})

    @staticmethod
    def emit_summary(report: Dict[str, Any]) -> str:
        return StreamManager.format_sse("final_summary", report)
