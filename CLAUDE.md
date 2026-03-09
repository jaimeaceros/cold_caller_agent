# Cold Caller v2 — Claude Code Instructions

## Project overview
AI-driven cold-call agent using Azure OpenAI Realtime API, Azure Cosmos DB, and Python.
Active branch: **jaimeslilbranch**. `main` holds v1 (legacy). Migrate only when results are confirmed better.

## Key files
- `agent/realtime.py` — Realtime API WebSocket client, prompt assembly, session tracking
- `agent/cosmos.py` — Cosmos DB data layer
- `agent/models.py` — Pydantic models (TurnMeta, QualifyingData) + dataclasses (CallSession, ConversationTurn)
- `assemble_prompt.py` — Assembles prompt + tool JSON for Azure AI Foundry Playground
- `test_realtime.py` — Text-mode realtime test harness (tools execute against real Cosmos data)
- `system_prompt_v2_realtime.md` — Prompt template with `{{PLACEHOLDER}}` variables
- `seed_cosmos.py` / `seed_data/` — Cosmos DB seeding

## Environment
- Platform: Windows 11, shell: bash
- LLM: Azure OpenAI Realtime API via WebSocket
- DB: Azure Cosmos DB via `COSMOS_CONNECTION_STRING`
- Secrets live in `.env` — **never commit `.env`**

## Auto-commit policy
**After every meaningful code change, create a git commit automatically.**
- Stage only source files (never `.env`, `__pycache__`, or binary files)
- Commit message format: `<type>: <short description>` (e.g. `fix: azure url path append bug`)
- Types: `fix`, `feat`, `refactor`, `test`, `chore`, `docs`
- Always push to `jaimeslilbranch` after committing
- If a session ends without committing pending changes, commit them before closing

## Git workflow
```
git add <specific files>
git commit -m "type: description"
git push origin jaimeslilbranch
```

## Testing
```bash
python assemble_prompt.py --lead lead_001    # Assemble prompt for Azure Playground
python test_realtime.py lead_001             # Quick 2-turn text test (tools work)
python test_realtime.py interactive lead_001 # Interactive text test (tools work)
```

## Branch strategy
| Branch | Purpose |
|---|---|
| `jaimeslilbranch` | Active development (v2) |
| `main` | Legacy v1 — do not modify |

Promote `jaimeslilbranch` → `main` only when v2 outperforms v1 in live tests.
