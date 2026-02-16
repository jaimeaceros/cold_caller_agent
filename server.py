from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from agent.brain import AgentBrain, CallContext

app = FastAPI()

# In-memory session store (replaced by Redis in Step 6)
sessions: dict[str, AgentBrain] = {}


class StartCallRequest(BaseModel):
    session_id: str
    context: CallContext


class TurnRequest(BaseModel):
    session_id: str
    prospect_message: str


class AgentResponse(BaseModel):
    response: str
    state: str
    is_call_over: bool


@app.post("/call/start", response_model=AgentResponse)
def start_call(req: StartCallRequest):
    brain = AgentBrain(
        knowledge_path="knowledge_base/seed_data/knowledge_base.json",
        call_context=req.context,
    )
    opening = brain.start_call()
    sessions[req.session_id] = brain
    return AgentResponse(
        response=opening,
        state=brain.current_state.value,
        is_call_over=False,
    )


@app.post("/call/turn", response_model=AgentResponse)
def process_turn(req: TurnRequest):
    brain = sessions.get(req.session_id)
    if not brain:
        raise HTTPException(404, "Session not found")
    response = brain.process_turn(req.prospect_message)
    return AgentResponse(
        response=response,
        state=brain.current_state.value,
        is_call_over=brain.is_call_over,
    )


@app.get("/health")
def health():
    return {"status": "ok"}