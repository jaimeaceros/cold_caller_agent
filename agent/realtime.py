"""
Realtime API session — production module.

Uses the same Cosmos data layer, prompt template system, and session tracking
as the REST path (agent/brain.py), but communicates over WebSocket using the
Azure OpenAI Realtime API.  The model speaks naturally and reports metadata
via tool calls instead of structured JSON output.

Supports two modes:
  - "text"  — request/response style (used by test_realtime.py)
  - "audio" — continuous event loop with PCM16 audio streaming (used by browser voice client)
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Callable, Awaitable

from agent.cosmos import (
    fetch_lead,
    fetch_all_knowledge,
    fetch_agent_config,
    format_knowledge_for_prompt,
    search_knowledge_base,
    fetch_prompt_template,
)
from agent.brain import CallSession, ConversationTurn
from agent.models import TurnMeta, QualifyingData

logger = logging.getLogger(__name__)

# Type alias for async callbacks
AsyncCallback = Callable[..., Awaitable[None]]

# ============================================================
# CONFIG
# ============================================================

RESOURCE = os.environ.get("REALTIME_RESOURCE", "ccafoundryresource")
DEPLOYMENT = os.environ.get("REALTIME_DEPLOYMENT", "gpt-realtime")
API_VERSION = os.environ.get("REALTIME_API_VERSION", "2024-10-01-preview")
API_KEY = os.environ.get("LLM_API_KEY", "")

WS_URL = (
    f"wss://{RESOURCE}.cognitiveservices.azure.com"
    f"/openai/realtime?api-version={API_VERSION}&deployment={DEPLOYMENT}"
)

# ============================================================
# TOOL DEFINITIONS — sent to the Realtime API
# ============================================================

TOOLS = [
    {
        "type": "function",
        "name": "search_knowledge_base",
        "description": (
            "Search the product knowledge base for information needed during the call. "
            "Use this for: product features, pricing, objection rebuttals, competitor comparisons, "
            "case studies, and qualifying questions. Call this BEFORE responding to the prospect "
            "when you need specific information."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to search for (e.g., 'pricing', 'outreach comparison', 'ROI case study')"
                },
                "category": {
                    "type": "string",
                    "enum": ["product", "objection", "competitor", "case_study", "qualifying"],
                    "description": "Knowledge category to search"
                }
            },
            "required": ["query"]
        }
    },
    {
        "type": "function",
        "name": "report_turn_metadata",
        "description": (
            "Report the current state of the conversation after each turn. "
            "You MUST call this after every response you give to the prospect."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "current_phase": {
                    "type": "string",
                    "enum": ["greeting", "rapport", "discovery", "pitch", "objection_handling", "close", "wrap_up"],
                    "description": "Current phase of the call"
                },
                "prospect_sentiment": {
                    "type": "string",
                    "enum": ["positive", "neutral", "negative", "hostile"],
                    "description": "Prospect's current sentiment"
                },
                "objections_detected": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "New objections detected this turn"
                },
                "objections_resolved": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Objections resolved this turn"
                },
                "qualifying_data": {
                    "type": "object",
                    "properties": {
                        "budget": {"type": "string", "description": "Budget info gathered (null if unknown)"},
                        "authority": {"type": "string", "description": "Decision-maker info (null if unknown)"},
                        "need": {"type": "string", "description": "Pain/need identified (null if unknown)"},
                        "timeline": {"type": "string", "description": "Timeline info (null if unknown)"}
                    }
                },
                "next_move": {
                    "type": "string",
                    "description": "What you plan to do next"
                },
                "buying_signals": {
                    "type": "boolean",
                    "description": "Whether buying signals were detected"
                }
            },
            "required": ["current_phase", "prospect_sentiment", "next_move"]
        }
    },
    {
        "type": "function",
        "name": "end_call",
        "description": "End the call. Call this when the conversation has reached a natural conclusion.",
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Why the call is ending"
                },
                "outcome": {
                    "type": "string",
                    "enum": [
                        "meeting_booked", "follow_up_scheduled", "not_interested",
                        "wrong_person", "voicemail_left", "escalated", "do_not_call"
                    ],
                    "description": "Call outcome"
                },
                "summary": {
                    "type": "string",
                    "description": "Brief summary of the call"
                }
            },
            "required": ["reason", "outcome", "summary"]
        }
    }
]

# ============================================================
# PROMPT ASSEMBLY — reuses brain.py patterns
# ============================================================

def _load_realtime_prompt_template() -> str:
    """Load the realtime prompt template (Blob Storage → local fallback)."""
    try:
        return fetch_prompt_template(
            blob_path="cold_caller/system_prompt_v2_realtime.md"
        )
    except Exception as e:
        logger.warning(f"Blob fetch failed ({e}), falling back to local file")
        path = Path(os.environ.get(
            "REALTIME_PROMPT_TEMPLATE_PATH",
            "system_prompt_v2_realtime.md",
        ))
        if not path.exists():
            raise FileNotFoundError(f"Realtime prompt template not found: {path}")
        return path.read_text(encoding="utf-8")


def assemble_realtime_prompt(lead: dict, knowledge: dict, agent_config: dict) -> str:
    """Assemble the full system prompt from template + Cosmos data.

    Mirrors AgentBrain._assemble_prompt but uses the realtime template.
    """
    template = _load_realtime_prompt_template()

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
    prospect_first_name = lead["contact"]["name"].split()[0]
    template = template.replace("{{PROSPECT_FIRST_NAME}}", prospect_first_name)
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
            for c in call_history[-3:]
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


def inject_history(base_prompt: str, history: list[ConversationTurn]) -> str:
    """Replace the conversation history placeholder with actual history.

    Same logic as AgentBrain._inject_history.
    """
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


# ============================================================
# SESSION STATE UPDATES — maps tool call args to models
# ============================================================

def _apply_turn_metadata(session: CallSession, args: dict):
    """Map report_turn_metadata args → TurnMeta → update session state.

    Same cumulative logic as AgentBrain._update_session.
    """
    qd_raw = args.get("qualifying_data", {})
    meta = TurnMeta(
        current_phase=args.get("current_phase", session.current_phase),
        prospect_sentiment=args.get("prospect_sentiment", "neutral"),
        objections_detected=args.get("objections_detected", []),
        objections_resolved=args.get("objections_resolved", []),
        qualifying_data=QualifyingData(
            budget=qd_raw.get("budget"),
            authority=qd_raw.get("authority"),
            need=qd_raw.get("need"),
            timeline=qd_raw.get("timeline"),
        ),
        buying_signals=args.get("buying_signals", False),
        next_move=args.get("next_move"),
    )

    # Phase
    session.current_phase = meta.current_phase

    # Qualifying — merge cumulatively
    qd = meta.qualifying_data
    if qd.budget:
        session.cumulative_qualifying["budget"] = qd.budget
    if qd.authority:
        session.cumulative_qualifying["authority"] = qd.authority
    if qd.need:
        session.cumulative_qualifying["need"] = qd.need
    if qd.timeline:
        session.cumulative_qualifying["timeline"] = qd.timeline

    # Objections
    for obj in meta.objections_detected:
        if obj not in session.all_objections_detected:
            session.all_objections_detected.append(obj)
    for obj in meta.objections_resolved:
        if obj not in session.all_objections_resolved:
            session.all_objections_resolved.append(obj)

    return meta


def _apply_end_call(session: CallSession, args: dict):
    """Map end_call args → session terminal state."""
    session.is_over = True
    session.call_outcome = args.get("outcome")


# ============================================================
# REALTIME SESSION — WebSocket client
# ============================================================

class RealtimeSession:
    """Manages a WebSocket session with the Azure OpenAI Realtime API.

    Integrates with the production data layer (agent/cosmos.py) and session
    tracking (CallSession from agent/brain.py).

    Modes:
        "text"  — text-only, request/response style (process_events)
        "audio" — audio + text, continuous event loop (run_event_loop)
    """

    def __init__(self, mode: str = "text"):
        self.mode = mode
        self.ws = None
        self.session: CallSession | None = None
        self.metadata_log: list[dict] = []
        self.knowledge_queries: list[dict] = []
        self.call_ended: bool = False
        self.call_outcome: dict | None = None

        # Audio-mode async callbacks (set by the WebSocket proxy)
        self.on_audio_delta: AsyncCallback | None = None
        self.on_audio_done: AsyncCallback | None = None
        self.on_transcript_delta: AsyncCallback | None = None
        self.on_transcript_done: AsyncCallback | None = None
        self.on_input_transcript: AsyncCallback | None = None
        self.on_speech_started: AsyncCallback | None = None
        self.on_speech_stopped: AsyncCallback | None = None
        self.on_call_ended: AsyncCallback | None = None
        self.on_error: AsyncCallback | None = None

    # ----- lifecycle -----

    async def connect(self):
        """Connect to the Realtime API via WebSocket."""
        import websockets

        headers = {"api-key": API_KEY}

        logger.info(f"Connecting to Realtime API: {WS_URL}")
        print(f"\n🔌 Connecting to Realtime API...")
        self.ws = await websockets.connect(
            WS_URL,
            additional_headers=headers,
            ping_interval=20,
            ping_timeout=20,
        )
        print(f"✅ Connected!")

        # Wait for session.created
        msg = await self.ws.recv()
        event = json.loads(msg)
        if event.get("type") == "session.created":
            print(f"✅ Session created: {event['session']['id']}")
            print(f"   Model: {event['session'].get('model', 'unknown')}")
        else:
            print(f"⚠️  Unexpected first event: {event.get('type')}")

    def init_call_session(self, session_id: str, lead_id: str) -> CallSession:
        """Initialize a CallSession from Cosmos data (mirrors AgentBrain.start_call data loading)."""
        lead = fetch_lead(lead_id)
        if not lead:
            raise ValueError(f"Lead {lead_id} not found")
        if lead.get("do_not_call"):
            raise ValueError(f"Lead {lead_id} is on do-not-call list")

        knowledge = fetch_all_knowledge()
        agent_config = fetch_agent_config()

        base_prompt = assemble_realtime_prompt(lead, knowledge, agent_config)

        self.session = CallSession(
            session_id=session_id,
            lead_id=lead_id,
            lead=lead,
            agent_config=agent_config,
            knowledge=knowledge,
            system_prompt=base_prompt,
        )

        logger.info(f"CallSession initialized: session={session_id} lead={lead_id}")
        return self.session

    async def configure(self, system_prompt: str | None = None):
        """Send session.update with system prompt and tools.

        If no prompt is passed, uses the session's assembled prompt with
        history injected.  Configuration adapts to self.mode.
        """
        if system_prompt is None:
            if self.session is None:
                raise ValueError("No CallSession — call init_call_session first or pass a prompt")
            system_prompt = inject_history(self.session.system_prompt, self.session.history)

        session_config = {
            "instructions": system_prompt,
            "tools": TOOLS,
            "tool_choice": "auto",
            "temperature": 0.7,
        }

        if self.mode == "audio":
            session_config.update({
                "modalities": ["text", "audio"],
                "voice": "alloy",
                "input_audio_format": "pcm16",
                "output_audio_format": "pcm16",
                "input_audio_transcription": {"model": "whisper-1"},
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": 0.5,
                    "prefix_padding_ms": 300,
                    "silence_duration_ms": 1200,
                },
            })
        else:
            session_config["modalities"] = ["text"]

        config = {
            "type": "session.update",
            "session": session_config,
        }

        await self.ws.send(json.dumps(config))
        logger.info(f"Session configured: mode={self.mode}, tools={len(TOOLS)}")

    # ----- messaging -----

    def _response_modalities(self) -> list[str]:
        """Return the modality list for response.create based on mode."""
        return ["text", "audio"] if self.mode == "audio" else ["text"]

    async def send_text(self, text: str = None):
        """Send a text message and trigger a response."""
        if text:
            await self.ws.send(json.dumps({
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": text}
                    ]
                }
            }))
            # Record prospect turn
            if self.session:
                self.session.history.append(ConversationTurn(
                    role="prospect",
                    content=text,
                    phase=self.session.current_phase,
                ))
        else:
            await self.ws.send(json.dumps({
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "[The prospect just picked up the phone. Begin the call.]"}
                    ]
                }
            }))

        await self.ws.send(json.dumps({
            "type": "response.create",
            "response": {
                "modalities": self._response_modalities()
            }
        }))

    async def send_audio(self, base64_audio: str):
        """Forward base64-encoded PCM16 audio to the Realtime API input buffer.

        Server VAD handles commit automatically — no manual commit needed.
        """
        await self.ws.send(json.dumps({
            "type": "input_audio_buffer.append",
            "audio": base64_audio,
        }))

    # ----- event processing -----

    async def process_events(self) -> str:
        """Process events until a complete response. Handles text deltas,
        function calls, and continuation after tool results.

        Returns the agent's spoken text.
        """
        full_text = ""
        pending_function_calls = {}
        response_done = False

        while not response_done:
            try:
                msg = await asyncio.wait_for(self.ws.recv(), timeout=30.0)
            except asyncio.TimeoutError:
                print("⏰ Timeout waiting for response")
                break

            event = json.loads(msg)
            event_type = event.get("type", "")

            # --- Text streaming ---
            if event_type == "response.text.delta":
                full_text += event.get("delta", "")

            elif event_type == "response.text.done":
                pass

            # --- Function call streaming ---
            elif event_type == "response.function_call_arguments.delta":
                call_id = event.get("call_id", "")
                if call_id not in pending_function_calls:
                    pending_function_calls[call_id] = {
                        "name": event.get("name", ""),
                        "arguments": ""
                    }
                pending_function_calls[call_id]["arguments"] += event.get("delta", "")

            elif event_type == "response.function_call_arguments.done":
                call_id = event.get("call_id", "")
                if call_id in pending_function_calls:
                    pending_function_calls[call_id]["arguments"] = event.get(
                        "arguments", pending_function_calls[call_id]["arguments"]
                    )
                    pending_function_calls[call_id]["name"] = event.get(
                        "name", pending_function_calls[call_id]["name"]
                    )

            # --- Response complete ---
            elif event_type == "response.done":
                response = event.get("response", {})
                status = response.get("status", "unknown")
                if status == "completed":
                    response_done = True
                elif status == "failed":
                    print(f"❌ Response failed: {response.get('status_details', {})}")
                    response_done = True
                elif status == "cancelled":
                    print(f"⚠️  Response cancelled")
                    response_done = True

            # --- Errors ---
            elif event_type == "error":
                error = event.get("error", {})
                print(f"❌ Error: {error.get('message', error)}")
                response_done = True

            elif event_type == "rate_limits.updated":
                pass

        # --- Execute pending function calls ---
        if pending_function_calls:
            await self._execute_function_calls(pending_function_calls)
            continuation = await self.process_events()
            if continuation:
                full_text += continuation

        # Record agent turn in session
        if full_text and self.session:
            self.session.history.append(ConversationTurn(
                role="agent",
                content=full_text,
                phase=self.session.current_phase,
            ))

        return full_text

    # ----- audio-mode event loop -----

    async def run_event_loop(self):
        """Continuous async event loop for audio-mode calls.

        Runs for the entire call lifetime. Dispatches events to callbacks
        set by the WebSocket proxy. Handles function calls inline.
        """
        pending_function_calls = {}
        current_transcript = ""

        while not self.call_ended:
            try:
                msg = await asyncio.wait_for(self.ws.recv(), timeout=60.0)
            except asyncio.TimeoutError:
                logger.warning("Event loop: 60s timeout, continuing...")
                continue
            except Exception as e:
                logger.error(f"Event loop recv error: {e}")
                if self.on_error:
                    await self.on_error(str(e))
                break

            event = json.loads(msg)
            event_type = event.get("type", "")

            # --- Audio streaming ---
            if event_type == "response.audio.delta":
                if self.on_audio_delta:
                    await self.on_audio_delta(event.get("delta", ""))

            elif event_type == "response.audio.done":
                if self.on_audio_done:
                    await self.on_audio_done()

            # --- Agent transcript (speech-to-text of agent audio) ---
            elif event_type == "response.audio_transcript.delta":
                current_transcript += event.get("delta", "")
                if self.on_transcript_delta:
                    await self.on_transcript_delta(event.get("delta", ""))

            elif event_type == "response.audio_transcript.done":
                transcript = event.get("transcript", current_transcript)
                if self.on_transcript_done:
                    await self.on_transcript_done(transcript)
                # Record agent turn
                if transcript and self.session:
                    self.session.history.append(ConversationTurn(
                        role="agent",
                        content=transcript,
                        phase=self.session.current_phase,
                    ))
                current_transcript = ""

            # --- Text streaming (text-mode responses during audio call) ---
            elif event_type == "response.text.delta":
                pass  # text not used in audio mode
            elif event_type == "response.text.done":
                pass

            # --- User input transcription (Whisper) ---
            elif event_type == "conversation.item.input_audio_transcription.completed":
                transcript = event.get("transcript", "")
                if transcript and self.on_input_transcript:
                    await self.on_input_transcript(transcript)
                # Record prospect turn
                if transcript and self.session:
                    self.session.history.append(ConversationTurn(
                        role="prospect",
                        content=transcript,
                        phase=self.session.current_phase,
                    ))

            elif event_type == "conversation.item.input_audio_transcription.failed":
                logger.warning(f"Input transcription failed: {event.get('error', {})}")

            # --- VAD events ---
            elif event_type == "input_audio_buffer.speech_started":
                # Cancel in-progress response to prevent double-speech.
                # Server VAD auto-triggers new response.create after user finishes.
                await self.ws.send(json.dumps({"type": "response.cancel"}))
                logger.debug("Sent response.cancel on speech_started")
                if self.on_speech_started:
                    await self.on_speech_started()

            elif event_type == "input_audio_buffer.speech_stopped":
                if self.on_speech_stopped:
                    await self.on_speech_stopped()

            elif event_type == "input_audio_buffer.committed":
                pass  # Server VAD auto-committed

            # --- Function call streaming ---
            elif event_type == "response.function_call_arguments.delta":
                call_id = event.get("call_id", "")
                if call_id not in pending_function_calls:
                    pending_function_calls[call_id] = {
                        "name": event.get("name", ""),
                        "arguments": ""
                    }
                pending_function_calls[call_id]["arguments"] += event.get("delta", "")

            elif event_type == "response.function_call_arguments.done":
                call_id = event.get("call_id", "")
                if call_id in pending_function_calls:
                    pending_function_calls[call_id]["arguments"] = event.get(
                        "arguments", pending_function_calls[call_id]["arguments"]
                    )
                    pending_function_calls[call_id]["name"] = event.get(
                        "name", pending_function_calls[call_id]["name"]
                    )

            # --- Response complete ---
            elif event_type == "response.done":
                response = event.get("response", {})
                status = response.get("status", "unknown")
                if status == "failed":
                    err = response.get("status_details", {})
                    logger.error(f"Response failed: {err}")
                    if self.on_error:
                        await self.on_error(f"Response failed: {err}")

                # Execute any pending function calls
                if pending_function_calls:
                    await self._execute_function_calls(pending_function_calls)
                    pending_function_calls = {}

                # If end_call was triggered, notify
                if self.call_ended and self.on_call_ended:
                    await self.on_call_ended(self.call_outcome)

            # --- Session events ---
            elif event_type == "session.updated":
                logger.debug("Session updated confirmed")

            elif event_type == "session.created":
                logger.debug("Session created (duplicate in event loop)")

            # --- Errors ---
            elif event_type == "error":
                error = event.get("error", {})
                logger.error(f"Realtime API error: {error}")
                if self.on_error:
                    await self.on_error(error.get("message", str(error)))

            elif event_type == "rate_limits.updated":
                pass

        logger.info("Event loop ended")

    # ----- tool execution -----

    async def _execute_function_calls(self, calls: dict):
        """Execute function calls and send results back to the model."""
        for call_id, call_info in calls.items():
            name = call_info["name"]
            try:
                args = json.loads(call_info["arguments"])
            except json.JSONDecodeError:
                args = {}

            logger.info(f"Tool call: {name}({json.dumps(args)[:200]})")

            if name == "search_knowledge_base":
                result = search_knowledge_base(
                    query=args.get("query", ""),
                    category=args.get("category"),
                )
                self.knowledge_queries.append(args)

            elif name == "report_turn_metadata":
                if self.session:
                    _apply_turn_metadata(self.session, args)
                self.metadata_log.append(args)
                result = "Metadata recorded."
                logger.info(
                    f"Metadata: phase={args.get('current_phase')} "
                    f"sentiment={args.get('prospect_sentiment')} "
                    f"next={args.get('next_move', 'N/A')[:60]}"
                )

            elif name == "end_call":
                if self.session:
                    _apply_end_call(self.session, args)
                self.call_ended = True
                self.call_outcome = args
                result = "Call ended."
                logger.info(f"Call ended: {args.get('outcome')} — {args.get('reason')}")

            else:
                result = f"Unknown function: {name}"

            # Send result back
            await self.ws.send(json.dumps({
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(result) if not isinstance(result, str) else result
                }
            }))

        # Trigger continuation with mode-aware modalities
        await self.ws.send(json.dumps({
            "type": "response.create",
            "response": {
                "modalities": self._response_modalities()
            }
        }))

    # ----- teardown -----

    async def close(self):
        if self.ws:
            await self.ws.close()
            print("\n🔌 Disconnected")

    # ----- convenience -----

    def get_call_summary(self) -> dict | None:
        """Generate a summary dict (same shape as AgentBrain.get_call_summary)."""
        s = self.session
        if not s:
            return None

        return {
            "session_id": s.session_id,
            "lead_id": s.lead_id,
            "started_at": s.started_at,
            "duration_seconds": time.time() - s.started_at,
            "total_turns": len(s.history),
            "final_phase": s.current_phase,
            "call_outcome": s.call_outcome,
            "qualifying_data": s.cumulative_qualifying,
            "objections_raised": s.all_objections_detected,
            "objections_resolved": s.all_objections_resolved,
            "knowledge_used": s.all_knowledge_used,
            "transcript": [
                {
                    "role": t.role,
                    "content": t.content,
                    "phase": t.phase,
                    "timestamp": t.timestamp,
                }
                for t in s.history
            ],
        }
