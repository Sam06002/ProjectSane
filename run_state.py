from enum import Enum

class RunState(str, Enum):
    """Structured execution state machine for Project Sane agent runs."""
    CREATED = "CREATED"
    QUEUED = "QUEUED"
    AUTHENTICATING = "AUTHENTICATING"
    DUPLICATING = "DUPLICATING"
    ANALYZING = "ANALYZING"
    PLANNING = "PLANNING"
    EXECUTING = "EXECUTING"
    REPORTING = "REPORTING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"
