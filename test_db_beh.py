"""
Test harness for the Cold Caller Agent.

Supports both Groq and Azure AI Foundry as LLM providers.

Usage:
    python test_db_beh.py single lead_001          # Single greeting
    python test_db_beh.py conversation lead_001     # Scripted conversation
    python test_db_beh.py interactive lead_001      # You play the prospect
    python test_db_beh.py prompt lead_001           # Print assembled prompt
"""

import io
import json
import os
import re
import sys

# Force UTF-8 output on Windows to avoid cp1252 encoding errors from LLM responses
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")
import sys
import httpx
from azure.cosmos import CosmosClient
from dotenv import load_dotenv

load_dotenv()

# --- CONFIG ---
COSMOS_CONNECTION_STRING = os.environ.get("COSMOS_CONNECTION_STRING", "")
DATABASE_NAME = os.environ.get("COSMOS_DB_NAME", "cold_caller_db")

# LLM — auto-detect provider
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "groq")
LLM_API_KEY = os.environ.get("LLM_API_KEY", os.environ.get("GROQ_API_KEY", ""))
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.groq.com/openai/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "llama-3.3-70b-versatile")
LLM_TEMPERATURE = float(os.environ.get("LLM_TEMPERATURE", "0.7"))
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "1024"))

# Auto-detect azure from URL
if "azure" in LLM_BASE_URL or "ai.azure.com" in LLM_BASE_URL:
    LLM_PROVIDER = "azure"

print(f"Provider: {LLM_PROVIDER} | Model: {LLM_MODEL}")
print(f"   Endpoint: {LLM_BASE_URL}")

# --- COSMOS DB CLIENT ---
cosmos_client = CosmosClient.from_connection_string(COSMOS_CONNECTION_STRING)
db = cosmos_client.get_database_client(DATABASE_NAME)


# ============================================================
# DATA LAYER
# ============================================================

def fetch_knowledge(category: str) -> list:
    container = db.get_container_client("knowledge-base")
    query = "SELECT * FROM c WHERE c.category = @cat AND c.active = true"
    params = [{"name": "@cat", "value": category}]
    return list(container.query_items(query, parameters=params, partition_key=category))


def fetch_lead(lead_id: str) -> dict:
    container = db.get_container_client("leads")
    query = "SELECT * FROM c WHERE c.id = @id"
    params = [{"name": "@id", "value": lead_id}]
    items = list(container.query_items(query, parameters=params, enable_cross_partition_query=True))
    return items[0] if items else None


def fetch_agent_config() -> dict:
    container = db.get_container_client("agent-config")
    items = list(container.read_all_items())
    return {item["id"]: item.get("data", {}) for item in items}


def format_knowledge_for_prompt(items: list, category: str) -> str:
    if not items:
        return f"No {category} data available."
    lines = []
    for item in items:
        lines.append(f"### {item.get('title', item['id'])}")
        lines.append(f"ID: {item['id']}")
        if item.get("subcategory"):
            lines.append(f"Topic: {item['subcategory']}")
        lines.append(f"{item['content']}")
        if item.get("follow_up_action"):
            lines.append(f"Suggested follow-up: {item['follow_up_action']}")
        lines.append("")
    return "\n".join(lines)


# ============================================================
# PROMPT ASSEMBLY
# ============================================================

def assemble_prompt(lead_id: str) -> str:
    lead = fetch_lead(lead_id)
    if not lead:
        raise ValueError(f"Lead {lead_id} not found")

    config = fetch_agent_config()
    agent = config.get("agent_identity", {})
    plan = lead.get("call_plan", {})

    products = fetch_knowledge("product")
    objections = fetch_knowledge("objection")
    competitors = fetch_knowledge("competitor")
    case_studies = fetch_knowledge("case_study")
    qualifying = fetch_knowledge("qualifying")

    try:
        from azure.storage.blob import BlobServiceClient
        blob_conn = os.environ.get("BLOB_CONNECTION_STRING", "")
        if blob_conn:
            blob_client = BlobServiceClient.from_connection_string(blob_conn)
            # Get active version from agent config
            prompt_config = config.get("prompt_config", {})
            folder = prompt_config.get("blob_path", "cold_caller/")
            filename = prompt_config.get("active_version", "system_prompt_v2.md")
            blob = blob_client.get_blob_client(container="prompts", blob=f"{folder}{filename}")
            template = blob.download_blob().readall().decode("utf-8")
            print(f"📄 Loaded prompt from Blob Storage: {folder}{filename}")
        else:
            raise ValueError("No BLOB_CONNECTION_STRING")
    except Exception as e:
        print(f"📄 Blob unavailable ({e}), using local file")
        with open("system_prompt_v2.md", "r") as f:
            template = f.read()

    template = template.replace("{{AGENT_NAME}}", agent.get("agent_name", "Alex"))
    template = template.replace("{{COMPANY_NAME}}", agent.get("company_name", "PipelineAI"))
    template = template.replace("{{CALLBACK_NUMBER}}", agent.get("callback_number", ""))
    template = template.replace("{{CALL_OBJECTIVE}}", plan.get("primary_objective", "Book a demo"))
    template = template.replace("{{FALLBACK_OBJECTIVE}}", plan.get("fallback_objective", "Send information and follow up"))
    template = template.replace("{{LEAD_CONTEXT}}", "")
    template = template.replace("{{PROSPECT_NAME}}", lead["contact"]["name"])
    template = template.replace("{{PROSPECT_TITLE}}", lead["contact"]["title"])
    template = template.replace("{{COMPANY}}", lead["company"]["name"])
    template = template.replace("{{INDUSTRY}}", lead["company"]["industry"])
    template = template.replace("{{COMPANY_SIZE}}", f"{lead['company']['size']} ({lead['company'].get('employee_count', '?')} employees)")
    template = template.replace("{{HOOK}}", plan.get("hook", ""))
    template = template.replace("{{PAIN_HYPOTHESIS}}", plan.get("pain_hypothesis", ""))
    template = template.replace("{{PREVIOUS_INTERACTIONS}}", "None — this is the first call.")
    template = template.replace("{{QUALIFYING_FRAMEWORK}}", agent.get("qualifying_framework", "BANT"))
    template = template.replace("{{DISCOVERY_QUESTIONS}}", format_knowledge_for_prompt(qualifying, "qualifying"))
    template = template.replace("{{PRODUCT_KNOWLEDGE}}", format_knowledge_for_prompt(products, "product"))
    template = template.replace("{{OBJECTION_PLAYBOOK}}", format_knowledge_for_prompt(objections, "objection"))
    template = template.replace("{{COMPETITOR_INTEL}}", format_knowledge_for_prompt(competitors, "competitor"))
    template = template.replace("{{CASE_STUDIES}}", format_knowledge_for_prompt(case_studies, "case_study"))
    template = template.replace("{{VOICEMAIL_HOOK}}", agent.get("voicemail_hook", ""))
    template = template.replace("{{ADDITIONAL_COMPLIANCE_RULES}}", agent.get("additional_compliance_rules", ""))
    template = template.replace("{{CONVERSATION_HISTORY}}", "No conversation history yet. This is the start of the call. The prospect just picked up the phone.")

    return template


# ============================================================
# LLM CALL — supports Groq and Azure AI Foundry
# ============================================================

def call_llm(system_prompt: str, user_message: str = None, debug: bool = True) -> dict:
    messages = [{"role": "system", "content": system_prompt}]

    if user_message:
        messages.append({"role": "user", "content": user_message})
    else:
        messages.append({"role": "user", "content": "[The prospect just picked up the phone. Begin the call.]"})

    # --- Build request based on provider ---
    if LLM_PROVIDER == "azure":
        # Azure OpenAI uses full URL from portal — don't append anything
        if "chat/completions" in LLM_BASE_URL:
            url = LLM_BASE_URL
        else:
            url = f"{LLM_BASE_URL.rstrip('/')}/chat/completions"
        headers = {
            "api-key": LLM_API_KEY,
            "Content-Type": "application/json",
        }
    else:
        url = f"{LLM_BASE_URL}/chat/completions"
        headers = {
            "Authorization": f"Bearer {LLM_API_KEY}",
            "Content-Type": "application/json",
        }

    body = {
        "model": LLM_MODEL,
        "messages": messages,
        "temperature": LLM_TEMPERATURE,
        "max_tokens": LLM_MAX_TOKENS,
    }

    # --- API call ---
    try:
        response = httpx.post(url, headers=headers, json=body, timeout=30.0)
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
    except httpx.HTTPStatusError as e:
        error_detail = e.response.text[:300] if e.response else "no response body"
        print(f"\nLLM HTTP Error {e.response.status_code}:")
        print(f"   {error_detail}")
        return {"spoken_response": f"[HTTP {e.response.status_code}]", "meta": {}, "parse_error": True}
    except Exception as e:
        print(f"\nLLM Error: {e}")
        return {"spoken_response": f"[LLM ERROR: {e}]", "meta": {}, "parse_error": True}

    # --- Debug output ---
    if debug:
        print(f"\nRAW LLM RESPONSE ({len(content)} chars):")
        print(content[:500])
        if len(content) > 500:
            print(f"... ({len(content) - 500} more chars)")
        print("---")

    # --- Parse JSON ---
    try:
        cleaned = content.strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        if cleaned.startswith("```"):
            cleaned = cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

        if not cleaned.startswith("{"):
            match = re.search(r'\{[\s\S]*\}', cleaned)
            if match:
                cleaned = match.group(0)

        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
        else:
            return {"spoken_response": str(parsed), "meta": {}, "parse_error": True}
    except (json.JSONDecodeError, AttributeError) as e:
        if debug:
            print(f"JSON parse failed: {e}")
        return {"spoken_response": content[:500], "meta": {}, "parse_error": True}


# ============================================================
# DISPLAY
# ============================================================

def display_result(result: dict):
    if result.get("parse_error"):
        print("\nResponse was not valid JSON:")
        print(f"   {result.get('spoken_response', 'NO RESPONSE')[:500]}")
        return

    print(f"\nAGENT SAYS:")
    print(f"   \"{result.get('spoken_response', 'NO RESPONSE')}\"")

    meta = result.get("meta", {})
    if meta:
        print(f"\nMETADATA:")
        for key, value in meta.items():
            if value is not None and value != [] and value != {} and value is not False:
                print(f"   {key}: {value}")


# ============================================================
# TEST MODES
# ============================================================

def run_test(lead_id: str):
    print(f"\n{'='*60}")
    print(f"TEST: Lead {lead_id}")
    print(f"{'='*60}\n")

    print("Assembling prompt...")
    prompt = assemble_prompt(lead_id)
    print(f"Prompt assembled: {len(prompt)} characters\n")

    print("Calling LLM (greeting)...")
    result = call_llm(prompt)
    display_result(result)
    return prompt, result


def run_conversation(lead_id: str):
    print(f"\n{'='*60}")
    print(f"CONVERSATION TEST: Lead {lead_id}")
    print(f"{'='*60}\n")

    prompt = assemble_prompt(lead_id)
    conversation_history = []

    prospect_turns = [
        None,
        "Yeah, this is Sarah. Who's calling?",
        "Oh okay, yeah we just closed our round. What does PipelineAI do exactly?",
        "Hmm, we're actually already using Outreach. It's working okay I think.",
        "I mean, our reply rates aren't great. Like maybe 2%. But I don't know if switching tools is the answer right now.",
        "I guess I could take a quick look. But I'd need to bring in our VP Ops too.",
    ]

    for i, prospect_msg in enumerate(prospect_turns):
        print(f"\n{'-'*40}")
        print(f"  Turn {i+1}")
        print(f"{'-'*40}")

        if prospect_msg:
            print(f"\nPROSPECT: \"{prospect_msg}\"")
            conversation_history.append({"role": "prospect", "content": prospect_msg})

        history_text = ""
        if conversation_history:
            for turn in conversation_history:
                role = "Agent" if turn["role"] == "agent" else "Prospect"
                history_text += f"{role}: {turn['content']}\n"

        current_prompt = prompt.replace(
            "No conversation history yet. This is the start of the call. The prospect just picked up the phone.",
            history_text if history_text else "No conversation history yet. This is the start of the call."
        )

        result = call_llm(current_prompt, prospect_msg, debug=(i == 0))
        display_result(result)

        if not result.get("parse_error"):
            spoken = result.get("spoken_response", "")
            conversation_history.append({"role": "agent", "content": spoken})
        else:
            conversation_history.append({"role": "agent", "content": result.get("spoken_response", "[parse error]")[:200]})

    print(f"\n{'='*60}")
    print("CONVERSATION COMPLETE")
    print(f"{'='*60}")


def run_interactive(lead_id: str):
    print(f"\n{'='*60}")
    print(f"INTERACTIVE CALL: Lead {lead_id}")
    print(f"Type prospect responses. Type 'quit' to end.")
    print(f"{'='*60}\n")

    prompt = assemble_prompt(lead_id)
    conversation_history = []
    turn = 0

    while True:
        turn += 1
        print(f"\n{'-'*40} Turn {turn} {'-'*40}")

        if turn == 1:
            prospect_msg = None
        else:
            prospect_msg = input("\nYOU (prospect): ").strip()
            if prospect_msg.lower() in ("quit", "exit", "q"):
                break
            conversation_history.append({"role": "prospect", "content": prospect_msg})

        history_text = ""
        if conversation_history:
            for t in conversation_history:
                role = "Agent" if t["role"] == "agent" else "Prospect"
                history_text += f"{role}: {t['content']}\n"

        current_prompt = prompt.replace(
            "No conversation history yet. This is the start of the call. The prospect just picked up the phone.",
            history_text if history_text else "No conversation history yet. This is the start of the call."
        )

        result = call_llm(current_prompt, prospect_msg, debug=False)
        display_result(result)

        if not result.get("parse_error"):
            spoken = result.get("spoken_response", "")
            conversation_history.append({"role": "agent", "content": spoken})
        else:
            conversation_history.append({"role": "agent", "content": result.get("spoken_response", "")[:200]})

        # Allow a few goodbye turns after a terminal outcome before ending
        terminal_outcomes = {"meeting_booked", "not_interested", "wrong_person", "voicemail_left", "escalated", "do_not_call", "follow_up_scheduled"}
        outcome = result.get("meta", {}).get("call_outcome")
        if outcome in terminal_outcomes:
            if not hasattr(run_interactive, '_goodbye_turns'):
                run_interactive._goodbye_turns = 0
                print(f"\n-- outcome reached: {outcome} -- allowing goodbye exchange --")
            run_interactive._goodbye_turns += 1
            if run_interactive._goodbye_turns > 2:
                print(f"\nCall ended -- outcome: {outcome}")
                break

    print(f"\n{'='*60}")
    print("CALL ENDED")
    print(f"{'='*60}")



if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "single"
    lead = sys.argv[2] if len(sys.argv) > 2 else "lead_001"

    if mode == "conversation":
        run_conversation(lead)
    elif mode == "interactive":
        run_interactive(lead)
    elif mode == "prompt":
        prompt = assemble_prompt(lead)
        print(prompt)
    else:
        run_test(lead)