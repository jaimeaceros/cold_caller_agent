# Cold Caller v2 — Claude Code Instructions

## Project overview
AI-driven cold-call agent using Azure OpenAI (GPT-5), Azure Cosmos DB, and Python/FastAPI.
Active branch: **jaimeslilbranch**. `main` holds v1 (legacy). Migrate only when results are confirmed better.

## Key files
- `agent/brain.py` — LLM orchestrator, prompt assembly, session management
- `agent/cosmos.py` — Cosmos DB data layer
- `agent/models.py` — Pydantic models (AgentOutput, TurnMeta, QualifyingData)
- `server.py` — FastAPI server
- `test_db_beh.py` — Standalone test harness (single / conversation / interactive / prompt modes)
- `system_prompt_v1.md` — Prompt template with `{{PLACEHOLDER}}` variables
- `seed_cosmos.py` / `seed_data/` — Cosmos DB seeding

## Environment
- Platform: Windows 11, shell: bash
- LLM: Azure OpenAI via `LLM_BASE_URL` (full deployment URL including `chat/completions`)
- DB: Azure Cosmos DB via `COSMOS_CONNECTION_STRING`
- Secrets live in `.env` — **never commit `.env`**

## LLM URL rule
`LLM_BASE_URL` in `.env` is the **full** Azure endpoint URL (includes `/chat/completions?api-version=...`).
Code detects this with `"chat/completions" in url` and uses it as-is — never appends path segments.

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
python test_db_beh.py single lead_001        # One greeting turn
python test_db_beh.py conversation lead_001  # Scripted multi-turn
python test_db_beh.py interactive lead_001   # Live prospect role-play
python test_db_beh.py prompt lead_001        # Print assembled prompt
```

## Branch strategy
| Branch | Purpose |
|---|---|
| `jaimeslilbranch` | Active development (v2) |
| `main` | Legacy v1 — do not modify |

Promote `jaimeslilbranch` → `main` only when v2 outperforms v1 in live tests.
