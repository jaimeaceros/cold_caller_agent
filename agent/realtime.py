"""
Realtime API session — production module.

Uses the Azure AI VoiceLive SDK for typed WebSocket communication
with the Azure OpenAI Realtime API. Integrates with Cosmos DB for
data access, prompt assembly, and session state tracking.

Supports two modes:
  - "text"  — request/response style (used by test_realtime.py)
  - "audio" — continuous event loop with real-time audio streaming
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Callable, Awaitable

from azure.core.credentials import AzureKeyCredential
from azure.ai.voicelive.aio import connect as voicelive_connect
from azure.ai.voicelive.models import (
    ServerEventType,
    RequestSession,
    ServerVad,
    Modality,
    InputAudioFormat,
    OutputAudioFormat,
    AudioInputTranscriptionOptions,
    FunctionTool,
    FunctionCallOutputItem,
    UserMessageItem,
    InputTextContentPart,
    AzureStandardVoice,
    ClientEventSessionUpdate,
    ClientEventConversationItemCreate,
    ClientEventResponseCreate,
    ClientEventResponseCancel,
    ClientEventInputAudioBufferAppend,
    ResponseCreateParams,
)

from agent.cosmos import (
    fetch_lead,
    fetch_all_knowledge,
    fetch_agent_config,
    format_knowledge_for_prompt,
    search_knowledge_base,
    fetch_prompt_template,
)
from agent.models import CallSession, ConversationTurn, TurnMeta, QualifyingData

logger = logging.getLogger(__name__)

# Type alias for async callbacks
AsyncCallback = Callable[..., Awaitable[None]]

# ============================================================
# CONFIG
# ============================================================

RESOURCE = os.environ.get("REALTIME_RESOURCE", "ccafoundryresource")
ENDPOINT = os.environ.get(
    "VOICELIVE_ENDPOINT",
    f"wss://{RESOURCE}.cognitiveservices.azure.com",
)
MODEL = os.environ.get("REALTIME_DEPLOYMENT", "gpt-4o-realtime-preview")
API_KEY = os.environ.get("LLM_API_KEY", "")
VOICE = os.environ.get("REALTIME_VOICE", "alloy")

# ============================================================
# TOOL DEFINITIONS — dict form for assemble_prompt.py JSON export
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
# SDK HELPERS
# ============================================================

def _build_sdk_tools() -> list[FunctionTool]:
    """Convert TOOLS dicts to typed FunctionTool objects for the SDK."""
    return [
        FunctionTool(
            name=t["name"],
            description=t.get("description", ""),
            parameters=t.get("parameters", {}),
        )
        for t in TOOLS
    ]


OPENAI_VOICES = {"alloy", "ash", "ballad", "coral", "echo", "fable", "onyx", "nova", "sage", "shimmer", "verse"}


def _build_voice_config():
    """Build voice config — string for OpenAI voices, AzureStandardVoice for Azure voices."""
    if VOICE in OPENAI_VOICES:
        return VOICE
    return AzureStandardVoice(name=VOICE, type="azure-standard")


# ============================================================
# PROMPT ASSEMBLY
# ============================================================

def _load_realtime_prompt_template() -> str:
    """Load the realtime prompt template (Blob Storage -> local fallback)."""
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
    """Assemble the full system prompt from template + Cosmos data."""
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


# ============================================================
# SESSION STATE UPDATES — maps tool call args to models
# ============================================================

def _apply_turn_metadata(session: CallSession, args: dict):
    """Map report_turn_metadata args -> TurnMeta -> update session state."""
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
    """Map end_call args -> session terminal state."""
    session.is_over = True
    session.call_outcome = args.get("outcome")


# ============================================================
# REALTIME SESSION — VoiceLive SDK client
# ============================================================

class RealtimeSession:
    """Manages a session with the Azure OpenAI Realtime API via VoiceLive SDK.

    Modes:
        "text"  — text-only, request/response style (process_events)
        "audio" — audio + text, continuous event loop (run_event_loop)
    """

    def __init__(self, mode: str = "text"):
        self.mode = mode
        self.conn = None
        self._cm = None  # context manager for connection lifecycle
        self.session: CallSession | None = None
        self.metadata_log: list[dict] = []
        self.knowledge_queries: list[dict] = []
        self.call_ended: bool = False
        self.call_outcome: dict | None = None

        # Audio-mode async callbacks
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
        """Connect to the Realtime API via VoiceLive SDK."""
        credential = AzureKeyCredential(API_KEY)

        logger.info(f"Connecting to VoiceLive: endpoint={ENDPOINT} model={MODEL}")
        print(f"\nConnecting to Realtime API...")

        self._cm = voicelive_connect(
            endpoint=ENDPOINT,
            credential=credential,
            model=MODEL,
            connection_options={
                "max_msg_size": 10 * 1024 * 1024,
                "heartbeat": 20,
                "timeout": 20,
            },
        )
        self.conn = await self._cm.__aenter__()

        # Wait for session.created
        event = await self.conn.recv()
        if event.type == ServerEventType.SESSION_CREATED:
            print(f"Session created: {event.session.id}")
        else:
            print(f"Unexpected first event: {event.type}")

    def init_call_session(self, session_id: str, lead_id: str) -> CallSession:
        """Initialize a CallSession from Cosmos data."""
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
        """Configure session with system prompt, tools, and mode settings."""
        if system_prompt is None:
            if self.session is None:
                raise ValueError("No CallSession — call init_call_session first or pass a prompt")
            system_prompt = inject_history(self.session.system_prompt, self.session.history)

        config_kwargs = {
            "instructions": system_prompt,
            "tools": _build_sdk_tools(),
            "tool_choice": "auto",
            "temperature": 0.7,
        }

        if self.mode == "audio":
            config_kwargs.update({
                "modalities": [Modality.TEXT, Modality.AUDIO],
                "voice": _build_voice_config(),
                "input_audio_format": InputAudioFormat.PCM16,
                "output_audio_format": OutputAudioFormat.PCM16,
                "input_audio_transcription": AudioInputTranscriptionOptions(model="whisper-1"),
                "turn_detection": ServerVad(
                    threshold=0.5,
                    prefix_padding_ms=300,
                    silence_duration_ms=800,
                ),
            })
        else:
            config_kwargs["modalities"] = [Modality.TEXT]

        await self.conn.send(ClientEventSessionUpdate(
            session=RequestSession(**config_kwargs)
        ))
        logger.info(f"Session configured: mode={self.mode}, tools={len(TOOLS)}")

    # ----- messaging -----

    async def send_text(self, text: str = None):
        """Send a text message and trigger a response."""
        msg = text or "[The prospect just picked up the phone. Begin the call.]"

        await self.conn.send(ClientEventConversationItemCreate(
            item=UserMessageItem(content=[InputTextContentPart(text=msg)])
        ))

        # Record prospect turn (only for real prospect messages)
        if text and self.session:
            self.session.history.append(ConversationTurn(
                role="prospect",
                content=text,
                phase=self.session.current_phase,
            ))

        await self.conn.send(ClientEventResponseCreate())

    async def send_audio(self, base64_audio: str):
        """Forward base64-encoded PCM16 audio to the input buffer.

        Server VAD handles commit automatically.
        """
        await self.conn.send(ClientEventInputAudioBufferAppend(audio=base64_audio))

    # ----- text-mode event processing -----

    async def process_events(self) -> str:
        """Process events until a complete response (text mode).

        Handles text deltas, function calls, and continuation after tool results.
        Returns the agent's spoken text.
        """
        full_text = ""
        pending_calls = {}

        async for event in self.conn:
            if event.type == ServerEventType.RESPONSE_TEXT_DELTA:
                full_text += event.delta

            elif event.type == ServerEventType.RESPONSE_TEXT_DONE:
                pass

            elif event.type == ServerEventType.RESPONSE_FUNCTION_CALL_ARGUMENTS_DONE:
                pending_calls[event.call_id] = {
                    "name": event.name,
                    "arguments": event.arguments,
                }

            elif event.type == ServerEventType.RESPONSE_FUNCTION_CALL_ARGUMENTS_DELTA:
                pass  # we use the final DONE event

            elif event.type == ServerEventType.RESPONSE_DONE:
                resp = event.response
                if hasattr(resp, "status") and resp.status == "failed":
                    details = getattr(resp, "status_details", "unknown")
                    print(f"Response failed: {details}")
                    logger.error(f"Response failed: {details}")
                break

            elif event.type == ServerEventType.ERROR:
                print(f"Error: {event.error.message}")
                logger.error(f"Error: {event.error.message}")
                break

            elif event.type in (
                ServerEventType.SESSION_UPDATED,
                ServerEventType.SESSION_CREATED,
                ServerEventType.RESPONSE_CREATED,
                ServerEventType.RESPONSE_OUTPUT_ITEM_ADDED,
                ServerEventType.RESPONSE_OUTPUT_ITEM_DONE,
                ServerEventType.RESPONSE_CONTENT_PART_ADDED,
                ServerEventType.RESPONSE_CONTENT_PART_DONE,
                ServerEventType.CONVERSATION_ITEM_CREATED,
            ):
                pass  # expected events, no action needed

            else:
                logger.debug(f"Unhandled event: {event.type}")

        # Execute pending function calls and get continuation
        if pending_calls:
            await self._execute_function_calls(pending_calls)
            continuation = await self.process_events()
            if continuation:
                full_text += continuation

        # Record agent turn
        if full_text and self.session:
            self.session.history.append(ConversationTurn(
                role="agent",
                content=full_text,
                phase=self.session.current_phase,
            ))

        return full_text

    # ----- audio-mode event loop -----

    async def run_event_loop(self):
        """Continuous event loop for audio-mode calls.

        Dispatches events to callbacks. Handles function calls inline.
        """
        pending_calls = {}
        current_transcript = ""

        async for event in self.conn:
            if self.call_ended:
                break

            # --- Audio streaming ---
            if event.type == ServerEventType.RESPONSE_AUDIO_DELTA:
                if self.on_audio_delta:
                    await self.on_audio_delta(event.delta)

            elif event.type == ServerEventType.RESPONSE_AUDIO_DONE:
                if self.on_audio_done:
                    await self.on_audio_done()

            # --- Agent transcript ---
            elif event.type == ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DELTA:
                current_transcript += event.delta
                if self.on_transcript_delta:
                    await self.on_transcript_delta(event.delta)

            elif event.type == ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DONE:
                transcript = event.transcript or current_transcript
                if self.on_transcript_done:
                    await self.on_transcript_done(transcript)
                if transcript and self.session:
                    self.session.history.append(ConversationTurn(
                        role="agent",
                        content=transcript,
                        phase=self.session.current_phase,
                    ))
                current_transcript = ""

            # --- User input transcription (Whisper) ---
            elif event.type == ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED:
                transcript = event.transcript or ""
                if transcript and self.on_input_transcript:
                    await self.on_input_transcript(transcript)
                if transcript and self.session:
                    self.session.history.append(ConversationTurn(
                        role="prospect",
                        content=transcript,
                        phase=self.session.current_phase,
                    ))

            elif event.type == ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_FAILED:
                logger.warning(f"Input transcription failed")

            # --- VAD events ---
            elif event.type == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED:
                try:
                    await self.conn.send(ClientEventResponseCancel())
                except Exception:
                    pass  # no response to cancel — harmless
                logger.debug("Sent response.cancel on speech_started")
                if self.on_speech_started:
                    await self.on_speech_started()

            elif event.type == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STOPPED:
                if self.on_speech_stopped:
                    await self.on_speech_stopped()

            elif event.type == ServerEventType.INPUT_AUDIO_BUFFER_COMMITTED:
                pass  # Server VAD auto-committed

            # --- Function calls ---
            elif event.type == ServerEventType.RESPONSE_FUNCTION_CALL_ARGUMENTS_DONE:
                pending_calls[event.call_id] = {
                    "name": event.name,
                    "arguments": event.arguments,
                }

            elif event.type == ServerEventType.RESPONSE_FUNCTION_CALL_ARGUMENTS_DELTA:
                pass  # we use the final DONE event

            # --- Response complete ---
            elif event.type == ServerEventType.RESPONSE_DONE:
                resp = event.response
                if hasattr(resp, "status") and resp.status == "failed":
                    details = getattr(resp, "status_details", "unknown")
                    logger.error(f"Response failed: {details}")
                    if self.on_error:
                        await self.on_error(f"Response failed: {details}")

                if pending_calls:
                    await self._execute_function_calls(pending_calls)
                    pending_calls = {}

                if self.call_ended and self.on_call_ended:
                    await self.on_call_ended(self.call_outcome)

            # --- Errors ---
            elif event.type == ServerEventType.ERROR:
                logger.error(f"Realtime API error: {event.error.message}")
                if self.on_error:
                    await self.on_error(event.error.message)

            else:
                logger.debug(f"Unhandled event: {event.type}")

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
            output = result if isinstance(result, str) else json.dumps(result)
            await self.conn.send(ClientEventConversationItemCreate(
                item=FunctionCallOutputItem(call_id=call_id, output=output)
            ))

        # Trigger continuation
        await self.conn.send(ClientEventResponseCreate())

    # ----- teardown -----

    async def close(self):
        """Close the VoiceLive connection."""
        if self._cm:
            await self._cm.__aexit__(None, None, None)
            self._cm = None
            self.conn = None
            print("\nDisconnected")

    # ----- convenience -----

    def get_call_summary(self) -> dict | None:
        """Generate a summary dict."""
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
