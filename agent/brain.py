"""
Agent Brain — prompt-driven orchestrator.

Replaces the old FSM + classifier + state transition logic.
The LLM now handles phase awareness, objection detection, and
conversation flow via a single structured prompt.

Each turn:
1. Assembles the full prompt (template + Cosmos DB data + history)
2. Calls the LLM
3. Parses structured JSON response
4. Returns spoken response + metadata

This is the only module that talks to the LLM.
"""

import json
import os
import re
import time
import logging
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from agent.cosmos import (
    fetch_lead,
    fetch_all_knowledge,
    fetch_agent_config,
    format_knowledge_for_prompt,
)
from agent.models import AgentOutput, TurnMeta, QualifyingData

logger = logging.getLogger(__name__)


# ============================================================
# CONVERSATION TURN — a single exchange in the call
# ============================================================

@dataclass
class ConversationTurn:
    role: str        # "prospect" or "agent"
    content: str     # What was said
    phase: str       # Phase during this turn
    meta: dict | None = None  # Agent metadata (agent turns only)
    timestamp: float = field(default_factory=time.time)


# ============================================================
# SESSION — holds state for one active call
# ============================================================

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


# ============================================================
# THE BRAIN
# ============================================================

class AgentBrain:
    """
    Orchestrates a single cold call conversation.

    Usage:
        brain = AgentBrain()
        session = brain.start_call("session_123", "lead_001")
        # session.spoken_response has the greeting

        result = brain.process_turn("session_123", "Yeah, who's this?")
        # result.spoken_response has the agent's reply
    """

    def __init__(self):
        self.sessions: dict[str, CallSession] = {}

        # LLM config
        self.llm_provider = os.environ.get("LLM_PROVIDER", "groq")  # "groq" or "azure"
        self.llm_base_url = os.environ.get("LLM_BASE_URL", "https://api.groq.com/openai/v1")
        self.llm_api_key = os.environ.get("LLM_API_KEY", os.environ.get("GROQ_API_KEY", ""))
        self.llm_model = os.environ.get("LLM_MODEL", "llama-3.3-70b-versatile")
        self.llm_temperature = float(os.environ.get("LLM_TEMPERATURE", "0.7"))
        self.llm_max_tokens = int(os.environ.get("LLM_MAX_TOKENS", "1024"))

        # Auto-detect provider from URL if not explicitly set
        if "azure" in self.llm_base_url or "ai.azure.com" in self.llm_base_url:
            self.llm_provider = "azure"

        # Prompt template (local file for now — Blob Storage later)
        self.prompt_template_path = os.environ.get("PROMPT_TEMPLATE_PATH", "system_prompt_v1.md")
        self._prompt_template: str | None = None

        logger.info(f"LLM provider: {self.llm_provider} | model: {self.llm_model} | base_url: {self.llm_base_url}")

    # -------------------------------------------------------------------
    # PROMPT TEMPLATE
    # -------------------------------------------------------------------

    def _load_prompt_template(self) -> str:
        """Load and cache the prompt template."""
        if self._prompt_template is None:
            path = Path(self.prompt_template_path)
            if not path.exists():
                raise FileNotFoundError(f"Prompt template not found: {path}")
            self._prompt_template = path.read_text(encoding="utf-8")
            logger.info(f"Loaded prompt template: {len(self._prompt_template)} chars")
        return self._prompt_template

    def reload_prompt(self):
        """Force reload of prompt template (for hot-swapping)."""
        self._prompt_template = None

    # -------------------------------------------------------------------
    # PROMPT ASSEMBLY
    # -------------------------------------------------------------------

    def _assemble_prompt(self, lead: dict, knowledge: dict, agent_config: dict) -> str:
        """Assemble the full system prompt from template + data."""
        template = self._load_prompt_template()

        agent = agent_config.get("agent_identity", {})
        plan = lead.get("call_plan", {})

        # Agent identity
        template = template.replace("{{AGENT_NAME}}", agent.get("agent_name", "Alex"))
        template = template.replace("{{COMPANY_NAME}}", agent.get("company_name", "PipelineAI"))
        template = template.replace("{{CALLBACK_NUMBER}}", agent.get("callback_number", ""))

        # Call objectives
        template = template.replace("{{CALL_OBJECTIVE}}", plan.get("primary_objective", "Book a demo"))
        template = template.replace("{{FALLBACK_OBJECTIVE}}", plan.get("fallback_objective", "Send information and follow up"))

        # Lead context
        template = template.replace("{{LEAD_CONTEXT}}", "")
        template = template.replace("{{PROSPECT_NAME}}", lead["contact"]["name"])
        template = template.replace("{{PROSPECT_TITLE}}", lead["contact"]["title"])
        template = template.replace("{{COMPANY}}", lead["company"]["name"])
        template = template.replace("{{INDUSTRY}}", lead["company"]["industry"])
        template = template.replace("{{COMPANY_SIZE}}", f"{lead['company']['size']} ({lead['company'].get('employee_count', '?')} employees)")
        template = template.replace("{{HOOK}}", plan.get("hook", ""))
        template = template.replace("{{PAIN_HYPOTHESIS}}", plan.get("pain_hypothesis", ""))

        # Previous interactions
        call_history = lead.get("call_history", [])
        if call_history:
            prev = "\n".join(
                f"- {c.get('timestamp', '?')}: {c.get('outcome', '?')} — {c.get('summary', 'No summary')}"
                for c in call_history[-3:]  # Last 3 calls max
            )
            template = template.replace("{{PREVIOUS_INTERACTIONS}}", prev)
        else:
            template = template.replace("{{PREVIOUS_INTERACTIONS}}", "None — this is the first call.")

        # Qualifying
        template = template.replace("{{QUALIFYING_FRAMEWORK}}", agent.get("qualifying_framework", "BANT"))
        template = template.replace("{{DISCOVERY_QUESTIONS}}", format_knowledge_for_prompt(knowledge.get("qualifying", [])))

        # Knowledge
        template = template.replace("{{PRODUCT_KNOWLEDGE}}", format_knowledge_for_prompt(knowledge.get("product", [])))
        template = template.replace("{{OBJECTION_PLAYBOOK}}", format_knowledge_for_prompt(knowledge.get("objection", [])))
        template = template.replace("{{COMPETITOR_INTEL}}", format_knowledge_for_prompt(knowledge.get("competitor", [])))
        template = template.replace("{{CASE_STUDIES}}", format_knowledge_for_prompt(knowledge.get("case_study", [])))

        # Voicemail
        template = template.replace("{{VOICEMAIL_HOOK}}", agent.get("voicemail_hook", ""))

        # Compliance
        template = template.replace("{{ADDITIONAL_COMPLIANCE_RULES}}", agent.get("additional_compliance_rules", ""))

        # Conversation history placeholder — replaced per-turn
        template = template.replace("{{CONVERSATION_HISTORY}}", "{{CONVERSATION_HISTORY}}")

        return template

    def _inject_history(self, base_prompt: str, history: list[ConversationTurn]) -> str:
        """Replace the conversation history placeholder with actual history."""
        if not history:
            return base_prompt.replace(
                "{{CONVERSATION_HISTORY}}",
                "No conversation history yet. This is the start of the call. The prospect just picked up the phone."
            )

        lines = []
        for turn in history:
            role = "Agent" if turn.role == "agent" else "Prospect"
            lines.append(f"{role}: {turn.content}")

        return base_prompt.replace("{{CONVERSATION_HISTORY}}", "\n".join(lines))

    # -------------------------------------------------------------------
    # LLM CALL
    # -------------------------------------------------------------------

    def _call_llm(self, system_prompt: str, user_message: str | None = None) -> AgentOutput:
        """Call the LLM and parse the structured response. Supports Groq and Azure AI Foundry."""
        messages = [{"role": "system", "content": system_prompt}]

        if user_message:
            messages.append({"role": "user", "content": user_message})
        else:
            messages.append({"role": "user", "content": "[The prospect just picked up the phone. Begin the call.]"})

        # --- Build request based on provider ---
        if self.llm_provider == "azure":
            # Azure AI Foundry uses api-key header and different endpoint path
            base = self.llm_base_url.rstrip("/")
            if "/chat/completions" in base:
                url = base  # Already a full endpoint URL (may have query string)
            else:
                url = f"{base}/chat/completions"

            headers = {
                "api-key": self.llm_api_key,
                "Content-Type": "application/json",
            }
            body = {
                "model": self.llm_model,
                "messages": messages,
                "temperature": self.llm_temperature,
                "max_tokens": self.llm_max_tokens,
            }
        else:
            # Groq / OpenAI-compatible
            url = f"{self.llm_base_url}/chat/completions"
            headers = {
                "Authorization": f"Bearer {self.llm_api_key}",
                "Content-Type": "application/json",
            }
            body = {
                "model": self.llm_model,
                "messages": messages,
                "temperature": self.llm_temperature,
                "max_tokens": self.llm_max_tokens,
            }

        # --- API call ---
        try:
            response = httpx.post(url, headers=headers, json=body, timeout=30.0)
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
        except httpx.HTTPStatusError as e:
            logger.error(f"LLM HTTP error: {e.response.status_code} — {e.response.text[:300]}")
            return AgentOutput(
                spoken_response="I apologize, I'm having a technical issue. Can I call you back in a moment?",
                meta=TurnMeta(current_phase="wrap_up", should_escalate=True, escalation_reason=f"LLM HTTP {e.response.status_code}")
            )
        except Exception as e:
            logger.error(f"LLM call failed: {e}")
            return AgentOutput(
                spoken_response="I apologize, I'm having a technical issue. Can I call you back in a moment?",
                meta=TurnMeta(current_phase="wrap_up", should_escalate=True, escalation_reason=f"LLM error: {e}")
            )

        # --- Parse response ---
        return self._parse_llm_response(content)

    def _parse_llm_response(self, content: str) -> AgentOutput:
        """Parse LLM response into AgentOutput. Handles markdown fences, preamble, etc."""
        try:
            cleaned = content.strip()

            # Strip markdown code fences
            if cleaned.startswith("```json"):
                cleaned = cleaned[7:]
            if cleaned.startswith("```"):
                cleaned = cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            cleaned = cleaned.strip()

            # Find JSON object if buried in text
            if not cleaned.startswith("{"):
                match = re.search(r'\{[\s\S]*\}', cleaned)
                if match:
                    cleaned = match.group(0)

            parsed = json.loads(cleaned)

            if isinstance(parsed, dict) and "spoken_response" in parsed:
                # Parse meta safely — use defaults for missing fields
                raw_meta = parsed.get("meta", {})
                meta = TurnMeta(
                    current_phase=raw_meta.get("current_phase", "greeting"),
                    phase_transition=raw_meta.get("phase_transition"),
                    prospect_sentiment=raw_meta.get("prospect_sentiment", "neutral"),
                    objections_detected=raw_meta.get("objections_detected", []),
                    objections_resolved=raw_meta.get("objections_resolved", []),
                    qualifying_data=QualifyingData(**raw_meta.get("qualifying_data", {})),
                    buying_signals=raw_meta.get("buying_signals", False),
                    should_escalate=raw_meta.get("should_escalate", False),
                    escalation_reason=raw_meta.get("escalation_reason"),
                    next_move=raw_meta.get("next_move"),
                    knowledge_used=raw_meta.get("knowledge_used", []),
                    call_outcome=raw_meta.get("call_outcome"),
                )
                return AgentOutput(spoken_response=parsed["spoken_response"], meta=meta)
            else:
                # Valid JSON but wrong structure
                logger.warning(f"LLM returned JSON without spoken_response: {str(parsed)[:200]}")
                return AgentOutput(
                    spoken_response=str(parsed.get("spoken_response", parsed))[:500],
                    meta=TurnMeta()
                )

        except (json.JSONDecodeError, AttributeError, TypeError) as e:
            logger.warning(f"Failed to parse LLM response as JSON: {e}")
            # Fallback: treat the raw content as the spoken response
            return AgentOutput(spoken_response=content[:500], meta=TurnMeta())

    # -------------------------------------------------------------------
    # SESSION MANAGEMENT
    # -------------------------------------------------------------------

    def _update_session(self, session: CallSession, output: AgentOutput):
        """Update session state from the LLM's output metadata."""
        meta = output.meta

        # Phase
        session.current_phase = meta.current_phase

        # Qualifying data — merge cumulatively (don't overwrite with None)
        qd = meta.qualifying_data
        if qd.budget:
            session.cumulative_qualifying["budget"] = qd.budget
        if qd.authority:
            session.cumulative_qualifying["authority"] = qd.authority
        if qd.need:
            session.cumulative_qualifying["need"] = qd.need
        if qd.timeline:
            session.cumulative_qualifying["timeline"] = qd.timeline

        # Objections — accumulate unique values
        for obj in meta.objections_detected:
            if obj not in session.all_objections_detected:
                session.all_objections_detected.append(obj)
        for obj in meta.objections_resolved:
            if obj not in session.all_objections_resolved:
                session.all_objections_resolved.append(obj)

        # Knowledge used
        for kid in meta.knowledge_used:
            if kid not in session.all_knowledge_used:
                session.all_knowledge_used.append(kid)

        # Call outcome
        if meta.call_outcome:
            session.call_outcome = meta.call_outcome

        # Check if call is over
        terminal_outcomes = {"meeting_booked", "not_interested", "wrong_person", "voicemail_left", "escalated", "do_not_call"}
        if meta.call_outcome in terminal_outcomes or meta.current_phase == "wrap_up":
            session.is_over = True

    # -------------------------------------------------------------------
    # PUBLIC API
    # -------------------------------------------------------------------

    def start_call(self, session_id: str, lead_id: str) -> AgentOutput:
        """
        Initialize a call session and generate the opening line.

        Returns AgentOutput with the greeting and initial metadata.
        """
        # Fetch all data from Cosmos DB
        lead = fetch_lead(lead_id)
        if not lead:
            raise ValueError(f"Lead {lead_id} not found")

        if lead.get("do_not_call"):
            raise ValueError(f"Lead {lead_id} is on do-not-call list")

        knowledge = fetch_all_knowledge()
        agent_config = fetch_agent_config()

        # Assemble the base prompt (without history)
        base_prompt = self._assemble_prompt(lead, knowledge, agent_config)

        # Create session
        session = CallSession(
            session_id=session_id,
            lead_id=lead_id,
            lead=lead,
            agent_config=agent_config,
            knowledge=knowledge,
            system_prompt=base_prompt,
        )

        # Generate opening — inject empty history
        prompt_with_history = self._inject_history(base_prompt, [])
        output = self._call_llm(prompt_with_history)

        # Record the turn
        session.history.append(ConversationTurn(
            role="agent",
            content=output.spoken_response,
            phase=output.meta.current_phase,
            meta=output.meta.model_dump(),
        ))
        self._update_session(session, output)

        # Store session
        self.sessions[session_id] = session

        logger.info(f"Call started: session={session_id} lead={lead_id} phase={output.meta.current_phase}")
        return output

    def process_turn(self, session_id: str, prospect_message: str) -> AgentOutput:
        """
        Process a prospect's message and generate the agent's response.

        Returns AgentOutput with spoken response and metadata.
        """
        session = self.sessions.get(session_id)
        if not session:
            raise ValueError(f"Session {session_id} not found")

        if session.is_over:
            raise ValueError(f"Session {session_id} is already complete")

        # Record prospect turn
        session.history.append(ConversationTurn(
            role="prospect",
            content=prospect_message,
            phase=session.current_phase,
        ))

        # Inject current history into prompt
        prompt_with_history = self._inject_history(session.system_prompt, session.history)

        # Call LLM
        output = self._call_llm(prompt_with_history, prospect_message)

        # Record agent turn
        session.history.append(ConversationTurn(
            role="agent",
            content=output.spoken_response,
            phase=output.meta.current_phase,
            meta=output.meta.model_dump(),
        ))

        # Update session state
        self._update_session(session, output)

        logger.info(
            f"Turn processed: session={session_id} "
            f"phase={output.meta.current_phase} "
            f"sentiment={output.meta.prospect_sentiment} "
            f"outcome={output.meta.call_outcome}"
        )
        return output

    def get_session(self, session_id: str) -> CallSession | None:
        """Get a session by ID."""
        return self.sessions.get(session_id)

    def end_call(self, session_id: str) -> CallSession | None:
        """Mark a call as ended and return the session for logging."""
        session = self.sessions.get(session_id)
        if session:
            session.is_over = True
        return session

    def get_call_summary(self, session_id: str) -> dict | None:
        """Generate a summary dict suitable for call_logs and lead updates."""
        session = self.sessions.get(session_id)
        if not session:
            return None

        return {
            "session_id": session.session_id,
            "lead_id": session.lead_id,
            "started_at": session.started_at,
            "duration_seconds": time.time() - session.started_at,
            "total_turns": len(session.history),
            "final_phase": session.current_phase,
            "call_outcome": session.call_outcome,
            "qualifying_data": session.cumulative_qualifying,
            "objections_raised": session.all_objections_detected,
            "objections_resolved": session.all_objections_resolved,
            "knowledge_used": session.all_knowledge_used,
            "transcript": [
                {
                    "role": t.role,
                    "content": t.content,
                    "phase": t.phase,
                    "timestamp": t.timestamp,
                }
                for t in session.history
            ],
        }

    def cleanup_session(self, session_id: str):
        """Remove a session from memory after logging."""
        self.sessions.pop(session_id, None)