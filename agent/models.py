"""
Request and response models for the Cold Caller Agent API.
"""

from pydantic import BaseModel, Field


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


class AgentOutput(BaseModel):
    """Parsed structured output from the LLM."""
    spoken_response: str
    meta: TurnMeta = Field(default_factory=TurnMeta)



class StartCallRequest(BaseModel):
    session_id: str
    lead_id: str


class TurnRequest(BaseModel):
    session_id: str
    prospect_message: str


class CallResponse(BaseModel):
    session_id: str
    spoken_response: str
    phase: str
    sentiment: str = "neutral"
    is_call_over: bool = False
    call_outcome: str | None = None
    should_escalate: bool = False
    meta: TurnMeta | None = None


class CallSummary(BaseModel):
    session_id: str
    lead_id: str
    total_turns: int
    duration_seconds: float | None = None
    final_phase: str
    call_outcome: str | None = None
    qualifying_data: QualifyingData = Field(default_factory=QualifyingData)
    objections_raised: list[str] = Field(default_factory=list)
    objections_resolved: list[str] = Field(default_factory=list)
    knowledge_used: list[str] = Field(default_factory=list)
    transcript: list[dict] = Field(default_factory=list)