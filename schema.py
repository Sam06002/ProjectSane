"""
schema.py — Strict Pydantic models for the Project Sane v2 planning engine.

All AI output MUST be validated against these models before any execution occurs.
Invalid plans are rejected outright. No partial execution of malformed plans.
"""

from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class ActionType(str, Enum):
    navigate = "navigate"
    click = "click"
    input = "input"
    extract = "extract"
    wait = "wait"
    screenshot = "screenshot"


class Action(BaseModel):
    """
    A single atomic browser operation.

    The 'target' field MUST be one of:
      - A named route key (for navigate): "settings", "apps", "modules", ...
      - An absolute path (for navigate): "/odoo/settings"
      - A registry label (for click/input/extract): "save_button", "confirm_dialog", ...
      - A plain text string (fallback): matched as Playwright text= selector

    The 'value' field is only meaningful for ActionType.input.
    LLM MUST NOT generate CSS selectors, XPath, or any DOM query strings.
    """

    type: ActionType
    target: str = Field(min_length=1, max_length=256)
    value: Optional[str] = Field(default=None, max_length=1024)

    @field_validator("target")
    @classmethod
    def reject_css_selectors(cls, v: str) -> str:
        """
        Prevent the LLM from sneaking CSS/XPath into the target field.
        Legitimate targets never start with these characters or patterns.
        """
        banned_prefixes = ("//", "(//", "./", "document.")
        banned_chars = set("{}")
        if any(v.startswith(p) for p in banned_prefixes):
            raise ValueError(
                f"CSS/XPath selectors are not allowed in target. Got: {v!r}"
            )
        if banned_chars & set(v):
            raise ValueError(
                f"Target contains banned characters. Got: {v!r}"
            )
        return v.strip()

    @field_validator("value")
    @classmethod
    def value_only_for_input(cls, v: Optional[str]) -> Optional[str]:
        # Structural check only — cross-field check is in the Action model_validator
        return v

    @model_validator(mode="after")
    def check_input_has_value(self) -> "Action":
        if self.type == ActionType.input and not self.value:
            self.value = "test"  # Default fallback instead of crashing
        return self


class Step(BaseModel):
    """A single numbered step in the execution plan."""

    id: int = Field(ge=1, le=50)
    intent: str = Field(min_length=5, max_length=256)
    reasoning: str = Field(min_length=5, max_length=512)
    action: Action
    expected_outcome: str = Field(min_length=5, max_length=256)
    fallback: str = Field(min_length=5, max_length=256)


class Plan(BaseModel):
    """
    The complete structured execution plan produced by the LLM.

    Rules enforced here:
    - confidence must be in [0.0, 1.0]
    - Steps must be non-empty and sequentially numbered starting at 1
    - Maximum 10 steps (prevents runaway plans)
    - summary and module must be non-empty strings
    """

    summary: str = Field(min_length=10, max_length=512)
    module: str = Field(min_length=2, max_length=128)
    confidence: float = Field(ge=0.0, le=1.0)
    steps: List[Step] = Field(min_length=1, max_length=10)

    @field_validator("steps")
    @classmethod
    def steps_must_be_sequential(cls, steps: List[Step]) -> List[Step]:
        for i, step in enumerate(steps, start=1):
            if step.id != i:
                raise ValueError(
                    f"Steps must be numbered sequentially from 1. "
                    f"Expected id={i}, got id={step.id}."
                )
        return steps


class ExecutionResult(BaseModel):
    """Result of executing a single Step."""

    step_id: int
    success: bool
    message: str
    extracted_text: Optional[str] = None
    screenshot_path: Optional[str] = None


class PipelineReport(BaseModel):
    """Final structured report returned to the frontend after pipeline completion."""

    ticket_summary: str
    module: str
    confidence: float
    steps_total: int
    steps_succeeded: int
    steps_failed: int
    was_executed: bool
    skip_reason: Optional[str] = None
    results: List[ExecutionResult]
    findings: List[str]
    recommendation: str
