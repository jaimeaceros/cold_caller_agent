"""
FastAPI Server for my coldcaller agente

Endpoints:
    POST /call/start     — Start a new call session
    POST /call/turn      — Process a prospect's message
    GET  /call/{id}      — Get current session state
    POST /call/{id}/end  — End a call and get summary
    GET  /health         — Health check
"""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
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



brain: AgentBrain | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
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

    The brain injects conversation history into the prompt,
    calls the LLM, and parses the structured output.
    """
    try:
        output = brain.process_turn(req.session_id, req.prospect_message)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to process turn: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to process turn")

    session = brain.get_session(req.session_id)

    return CallResponse(
        session_id=req.session_id,
        spoken_response=output.spoken_response,
        phase=output.meta.current_phase,
        sentiment=output.meta.prospect_sentiment,
        is_call_over=session.is_over if session else False,
        call_outcome=output.meta.call_outcome,
        should_escalate=output.meta.should_escalate,
        meta=output.meta,
    )


@app.get("/call/{session_id}", response_model=CallSummary)
def get_call_state(session_id: str):
    summary = brain.get_call_summary(session_id)
    if not summary:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")

    return CallSummary(**summary)


@app.post("/call/{session_id}/end", response_model=CallSummary)
def end_call(session_id: str):
    
    session = brain.end_call(session_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")

    summary = brain.get_call_summary(session_id)

    # TODO: Write to call-logs container
    # TODO: Update lead status in leads container

    # Clean up in-memory session
    brain.cleanup_session(session_id)

    return CallSummary(**summary)


@app.get("/health")
def health():
    """Health check for Azure Container Apps."""
    return {
        "status": "ok",
        "version": "2.0.0",
        "model": brain.llm_model if brain else "not initialized",
        "active_sessions": len(brain.sessions) if brain else 0,
    }



if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=True)