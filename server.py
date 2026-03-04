"""
Cold Caller Agent — FastAPI Server

Endpoints:
    POST /call/start     — Start a new call session
    POST /call/turn      — Process a prospect's message
    GET  /call/{id}      — Get current session state
    POST /call/{id}/end  — End a call and get summary
    GET  /health         — Health check
    WS   /ws/call/{id}   — Realtime voice call via WebSocket
    GET  /voice-test     — Browser voice testing page
"""

import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

from agent.brain import AgentBrain
from agent.models import (
    StartCallRequest,
    TurnRequest,
    CallResponse,
    CallSummary,
)

load_dotenv()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger(__name__)


# ============================================================
# APP LIFECYCLE
# ============================================================

brain: AgentBrain | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize the brain on startup."""
    global brain
    logger.info("Initializing AgentBrain...")
    brain = AgentBrain()
    logger.info("AgentBrain ready.")
    yield
    logger.info("Shutting down.")


app = FastAPI(
    title="Cold Caller Agent API",
    version="2.0.0",
    description="Prompt-driven outbound sales agent",
    lifespan=lifespan,
)

# Static files for browser voice client
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")


# ============================================================
# ENDPOINTS
# ============================================================

@app.post("/call/start", response_model=CallResponse)
def start_call(req: StartCallRequest):
    """
    Start a new call session.

    Fetches lead data from Cosmos DB, assembles the prompt,
    and generates the agent's opening line.
    """
    try:
        output = brain.start_call(req.session_id, req.lead_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to start call: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to start call")

    session = brain.get_session(req.session_id)

    return CallResponse(
        session_id=req.session_id,
        spoken_response=output.spoken_response,
        phase=output.meta.current_phase,
        sentiment=output.meta.prospect_sentiment,
        is_call_over=False,
        call_outcome=output.meta.call_outcome,
        should_escalate=output.meta.should_escalate,
        meta=output.meta,
    )


@app.post("/call/turn", response_model=CallResponse)
def process_turn(req: TurnRequest):
    """
    Process the prospect's message and return the agent's response.

    If the call ends naturally (wrap_up, DNC, etc.), auto-logs to Cosmos DB.
    """
    try:
        output = brain.process_turn(req.session_id, req.prospect_message)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to process turn: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to process turn")

    session = brain.get_session(req.session_id)
    is_over = session.is_over if session else False

    # Auto-log if the call ended naturally
    if is_over and session:
        try:
            _persist_call(req.session_id)
            logger.info(f"Auto-logged completed call: {req.session_id}")
        except Exception as e:
            logger.error(f"Auto-log failed: {e}", exc_info=True)

    return CallResponse(
        session_id=req.session_id,
        spoken_response=output.spoken_response,
        phase=output.meta.current_phase,
        sentiment=output.meta.prospect_sentiment,
        is_call_over=is_over,
        call_outcome=output.meta.call_outcome,
        should_escalate=output.meta.should_escalate,
        meta=output.meta,
    )


@app.get("/call/{session_id}", response_model=CallSummary)
def get_call_state(session_id: str):
    """Get the current state of an active call session."""
    summary = brain.get_call_summary(session_id)
    if not summary:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")

    return CallSummary(**summary)


@app.post("/call/{session_id}/end", response_model=CallSummary)
def end_call(session_id: str):
    """
    End a call session, log to Cosmos DB, and return the full summary.
    """
    session = brain.end_call(session_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")

    summary = brain.get_call_summary(session_id)
    _persist_call(session_id)
    brain.cleanup_session(session_id)

    return CallSummary(**summary)


# ============================================================
# PERSISTENCE — Write call data to Cosmos DB
# ============================================================

def _persist_call(session_id: str):
    """
    Write call log and update lead in Cosmos DB.
    Called by both end_call (explicit) and process_turn (auto on call end).
    Best-effort — errors are logged but don't fail the request.
    """
    from agent.cosmos import write_call_log, update_lead_after_call

    summary = brain.get_call_summary(session_id)
    if not summary:
        return

    # --- Write to call-logs container ---
    try:
        call_log = {
            "id": summary["session_id"],
            "lead_id": summary["lead_id"],
            "agent_prompt_version": _get_active_prompt_version(),
            "started_at": datetime.fromtimestamp(summary["started_at"], tz=timezone.utc).isoformat(),
            "ended_at": datetime.now(timezone.utc).isoformat(),
            "duration_seconds": int(summary["duration_seconds"]),
            "outcome": summary["call_outcome"] or "no_outcome",
            "transcript": summary["transcript"],
            "final_meta": {
                "qualifying_data": summary["qualifying_data"],
                "objections_raised": summary["objections_raised"],
                "objections_resolved": summary["objections_resolved"],
                "knowledge_used": summary["knowledge_used"],
            },
            "model_used": brain.llm_model,
            "total_turns": summary["total_turns"],
            "final_phase": summary["final_phase"],
        }
        write_call_log(call_log)
        logger.info(f"Call log written: {session_id}")
    except Exception as e:
        logger.error(f"Failed to write call log: {e}", exc_info=True)

    # --- Update lead status + score ---
    try:
        outcome = summary["call_outcome"] or "no_outcome"
        new_status, score_delta = _outcome_to_status(outcome)

        call_record = {
            "call_id": summary["session_id"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "duration_seconds": int(summary["duration_seconds"]),
            "outcome": outcome,
            "summary": _generate_call_summary_text(summary),
            "objections_raised": summary["objections_raised"],
            "objections_resolved": summary["objections_resolved"],
            "qualifying_data": summary["qualifying_data"],
            "next_steps": summary.get("next_steps"),
            "knowledge_used": summary["knowledge_used"],
        }

        update_lead_after_call(
            lead_id=summary["lead_id"],
            call_record=call_record,
            new_status=new_status,
            lead_score_delta=score_delta,
        )
        logger.info(f"Lead updated: {summary['lead_id']} → status={new_status}, score_delta={score_delta:+d}")
    except Exception as e:
        logger.error(f"Failed to update lead: {e}", exc_info=True)


# ============================================================
# HELPERS — Outcome mapping and summary generation
# ============================================================

def _outcome_to_status(outcome: str) -> tuple[str, int]:
    """Map call outcome to lead status and score delta."""
    mapping = {
        "meeting_booked":     ("meeting_booked",  +25),
        "follow_up_scheduled": ("contacted",       +10),
        "not_interested":      ("not_interested",   -10),
        "wrong_person":        ("new",               0),
        "voicemail_left":      ("contacted",         +5),
        "escalated":           ("contacted",         +5),
        "do_not_call":         ("do_not_call",      -50),
        "no_outcome":          ("contacted",          0),
    }
    return mapping.get(outcome, ("contacted", 0))


def _generate_call_summary_text(summary: dict) -> str:
    """Generate a human-readable summary line for the lead's call history."""
    parts = []

    outcome = summary.get("call_outcome", "no_outcome")
    parts.append(f"Outcome: {outcome}")

    qd = summary.get("qualifying_data", {})
    qualified = [f"{k}={v}" for k, v in qd.items() if v]
    if qualified:
        parts.append(f"Qualified: {', '.join(qualified)}")

    objections = summary.get("objections_raised", [])
    if objections:
        parts.append(f"Objections: {', '.join(objections)}")

    resolved = summary.get("objections_resolved", [])
    if resolved:
        parts.append(f"Resolved: {', '.join(resolved)}")

    return " | ".join(parts)


def _get_active_prompt_version() -> str:
    """Get the currently active prompt version from agent config."""
    try:
        from agent.cosmos import fetch_agent_config
        config = fetch_agent_config()
        return config.get("prompt_config", {}).get("active_version", "unknown")
    except Exception:
        return "unknown"


@app.get("/health")
def health():
    """Health check for Azure Container Apps."""
    return {
        "status": "ok",
        "version": "2.0.0",
        "model": brain.llm_model if brain else "not initialized",
        "active_sessions": len(brain.sessions) if brain else 0,
    }


# ============================================================
# VOICE TEST — Browser client
# ============================================================

@app.get("/voice-test")
async def voice_test():
    """Serve the browser voice testing page."""
    return FileResponse("static/voice_test.html")


# ============================================================
# WEBSOCKET — Realtime voice call proxy
# ============================================================

@app.websocket("/ws/call/{lead_id}")
async def ws_call(websocket: WebSocket, lead_id: str):
    """WebSocket proxy between browser and Azure OpenAI Realtime API.

    Protocol:
        Browser → Server: {"type": "start_call"} | {"type": "audio", "data": "..."} | {"type": "stop"}
        Server → Browser: {"type": "ready"} | {"type": "audio"} | {"type": "agent_transcript_*"} | etc.
    """
    await websocket.accept()
    logger.info(f"WebSocket connected: lead={lead_id}")

    from agent.realtime import RealtimeSession

    session = RealtimeSession(mode="audio")
    session_id = f"voice_{uuid.uuid4().hex[:8]}"

    async def send_browser(msg: dict):
        """Send JSON message to the browser, ignoring closed-connection errors."""
        try:
            await websocket.send_json(msg)
        except Exception:
            pass

    # --- Wire up callbacks ---
    async def on_audio_delta(base64_chunk):
        await send_browser({"type": "audio", "data": base64_chunk})

    async def on_audio_done():
        await send_browser({"type": "audio_done"})

    async def on_transcript_delta(delta):
        await send_browser({"type": "agent_transcript_delta", "delta": delta})

    async def on_transcript_done(transcript):
        await send_browser({"type": "agent_transcript_done", "transcript": transcript})

    async def on_input_transcript(transcript):
        await send_browser({"type": "user_transcript", "transcript": transcript})

    async def on_speech_started():
        await send_browser({"type": "speech_started"})

    async def on_speech_stopped():
        await send_browser({"type": "speech_stopped"})

    async def on_call_ended(outcome):
        await send_browser({"type": "call_ended", "outcome": outcome or {}})

    async def on_error(message):
        await send_browser({"type": "error", "message": message})

    session.on_audio_delta = on_audio_delta
    session.on_audio_done = on_audio_done
    session.on_transcript_delta = on_transcript_delta
    session.on_transcript_done = on_transcript_done
    session.on_input_transcript = on_input_transcript
    session.on_speech_started = on_speech_started
    session.on_speech_stopped = on_speech_stopped
    session.on_call_ended = on_call_ended
    session.on_error = on_error

    event_loop_task = None

    try:
        # Connect to Azure Realtime API
        await session.connect()
        call_session = session.init_call_session(session_id, lead_id)
        await session.configure()

        await send_browser({
            "type": "ready",
            "session_id": session_id,
            "lead_id": lead_id,
            "prospect_name": call_session.lead.get("contact", {}).get("name", "Unknown"),
        })

        # Start Azure event loop in background
        event_loop_task = asyncio.create_task(session.run_event_loop())

        # Read browser messages
        while True:
            try:
                data = await websocket.receive_json()
            except WebSocketDisconnect:
                logger.info(f"Browser disconnected: {session_id}")
                break
            except Exception as e:
                logger.error(f"Browser recv error: {e}")
                break

            msg_type = data.get("type", "")

            if msg_type == "start_call":
                # Trigger agent greeting via text (audio response comes back via event loop)
                await session.send_text()

            elif msg_type == "audio":
                audio_data = data.get("data", "")
                if audio_data:
                    await session.send_audio(audio_data)

            elif msg_type == "stop":
                logger.info(f"Browser requested stop: {session_id}")
                break

    except Exception as e:
        logger.error(f"WebSocket error: {e}", exc_info=True)
        await send_browser({"type": "error", "message": str(e)})

    finally:
        # Cancel the event loop task
        if event_loop_task and not event_loop_task.done():
            event_loop_task.cancel()
            try:
                await event_loop_task
            except asyncio.CancelledError:
                pass

        # Persist call to Cosmos
        try:
            _persist_realtime_call(session)
        except Exception as e:
            logger.error(f"Failed to persist realtime call: {e}", exc_info=True)

        await session.close()
        logger.info(f"WebSocket session cleaned up: {session_id}")


def _persist_realtime_call(rt_session: "RealtimeSession"):
    """Persist a realtime voice call to Cosmos DB (mirrors _persist_call for REST)."""
    from agent.cosmos import write_call_log, update_lead_after_call

    summary = rt_session.get_call_summary()
    if not summary or summary["total_turns"] == 0:
        return

    # Write call log
    call_log = {
        "id": summary["session_id"],
        "lead_id": summary["lead_id"],
        "agent_prompt_version": _get_active_prompt_version(),
        "channel": "voice_realtime",
        "started_at": datetime.fromtimestamp(summary["started_at"], tz=timezone.utc).isoformat(),
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": int(summary["duration_seconds"]),
        "outcome": summary["call_outcome"] or "no_outcome",
        "transcript": summary["transcript"],
        "final_meta": {
            "qualifying_data": summary["qualifying_data"],
            "objections_raised": summary["objections_raised"],
            "objections_resolved": summary["objections_resolved"],
            "knowledge_used": summary["knowledge_used"],
        },
        "model_used": "gpt-realtime",
        "total_turns": summary["total_turns"],
        "final_phase": summary["final_phase"],
    }
    write_call_log(call_log)
    logger.info(f"Realtime call log written: {summary['session_id']}")

    # Update lead
    outcome = summary["call_outcome"] or "no_outcome"
    new_status, score_delta = _outcome_to_status(outcome)

    call_record = {
        "call_id": summary["session_id"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": int(summary["duration_seconds"]),
        "outcome": outcome,
        "summary": _generate_call_summary_text(summary),
        "objections_raised": summary["objections_raised"],
        "objections_resolved": summary["objections_resolved"],
        "qualifying_data": summary["qualifying_data"],
        "knowledge_used": summary["knowledge_used"],
    }

    update_lead_after_call(
        lead_id=summary["lead_id"],
        call_record=call_record,
        new_status=new_status,
        lead_score_delta=score_delta,
    )
    logger.info(f"Lead updated after realtime call: {summary['lead_id']} → {new_status}")


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=True)