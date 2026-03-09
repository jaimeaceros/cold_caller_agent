"""
Models for the Cold Caller Agent.

Includes Pydantic models for metadata/qualifying data, and dataclasses
for session state tracking used by the Realtime API client.
"""

import time
from dataclasses import dataclass, field

from pydantic import BaseModel, Field


# ============================================================
# PYDANTIC MODELS — used by realtime.py for metadata
# ============================================================

class QualifyingData(BaseModel):
    budget: str | None = None
    authority: str | None = None
    need: str | None = None
    timeline: str | None = None


class TurnMeta(BaseModel):
    current_phase: str = "greeting"
    phase_transition: str | None = None
    prospect_sentiment: str = "neutral"
    objections_detected: list[str] = Field(default_factory=list)
    objections_resolved: list[str] = Field(default_factory=list)
    qualifying_data: QualifyingData = Field(default_factory=QualifyingData)
    buying_signals: bool = False
    should_escalate: bool = False
    escalation_reason: str | None = None
    next_move: str | None = None
    knowledge_used: list[str] = Field(default_factory=list)
    call_outcome: str | None = None


# ============================================================
# DATACLASSES — session state (moved from brain.py)
# ============================================================

@dataclass
class ConversationTurn:
    role: str        # "prospect" or "agent"
    content: str     # What was said
    phase: str       # Phase during this turn
    meta: dict | None = None  # Agent metadata (agent turns only)
    timestamp: float = field(default_factory=time.time)


@dataclass
class CallSession:
    session_id: str
    lead_id: str
    lead: dict
    agent_config: dict
    knowledge: dict  # {category: [items]}
    system_prompt: str  # Assembled prompt (without conversation history)
    history: list[ConversationTurn] = field(default_factory=list)
    cumulative_qualifying: dict = field(default_factory=lambda: {
        "budget": None, "authority": None, "need": None, "timeline": None
    })
    all_objections_detected: list[str] = field(default_factory=list)
    all_objections_resolved: list[str] = field(default_factory=list)
    all_knowledge_used: list[str] = field(default_factory=list)
    current_phase: str = "greeting"
    call_outcome: str | None = None
    started_at: float = field(default_factory=time.time)
    is_over: bool = False
