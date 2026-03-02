"""
Cosmos DB data access layer.

All database queries live here. The brain never talks to Cosmos directly.
"""

import os
from functools import lru_cache
from azure.cosmos import CosmosClient



_client: CosmosClient | None = None
_db = None


def get_db():
    global _client, _db
    if _db is None:
        conn_str = os.environ.get("COSMOS_CONNECTION_STRING", "")
        if not conn_str:
            raise ValueError("COSMOS_CONNECTION_STRING not set")
        _client = CosmosClient.from_connection_string(conn_str)
        _db = _client.get_database_client(os.environ.get("COSMOS_DB_NAME", "cold_caller_db"))
    return _db



def fetch_knowledge(category: str) -> list[dict]:
    """Fetch all active knowledge entries for a category."""
    db = get_db()
    container = db.get_container_client("knowledge-base")
    query = "SELECT * FROM c WHERE c.category = @cat AND c.active = true"
    params = [{"name": "@cat", "value": category}]
    return list(container.query_items(query, parameters=params, partition_key=category))


def fetch_all_knowledge() -> dict[str, list[dict]]:
    """Fetch all knowledge categories in one call. Returns dict keyed by category."""
    categories = ["product", "objection", "competitor", "case_study", "qualifying"]
    return {cat: fetch_knowledge(cat) for cat in categories}


# ============================================================
# LEAD QUERIES
# ============================================================

def fetch_lead(lead_id: str) -> dict | None:
    """Fetch a lead profile by ID."""
    db = get_db()
    container = db.get_container_client("leads")
    query = "SELECT * FROM c WHERE c.id = @id"
    params = [{"name": "@id", "value": lead_id}]
    items = list(container.query_items(query, parameters=params, enable_cross_partition_query=True))
    return items[0] if items else None


def update_lead_after_call(lead_id: str, call_record: dict, new_status: str, lead_score_delta: int = 0):
    """Update a lead document after a call completes."""
    db = get_db()
    container = db.get_container_client("leads")

    lead = fetch_lead(lead_id)
    if not lead:
        return

    # Append call to history
    if "call_history" not in lead:
        lead["call_history"] = []
    lead["call_history"].append(call_record)

    # Update status
    lead["status"] = new_status

    # Update lead score
    lead["lead_score"] = min(100, max(0, lead.get("lead_score", 50) + lead_score_delta))

    # Handle do_not_call
    if call_record.get("outcome") == "do_not_call":
        lead["do_not_call"] = True
        lead["status"] = "do_not_call"

    container.upsert_item(lead)


# ============================================================
# AGENT CONFIG QUERIES
# ============================================================

def fetch_agent_config() -> dict:
    """Fetch all agent config documents, returned as {id: data} dict."""
    db = get_db()
    container = db.get_container_client("agent-config")
    items = list(container.read_all_items())
    return {item["id"]: item.get("data", {}) for item in items}


# ============================================================
# CALL LOG WRITES
# ============================================================

def write_call_log(call_log: dict):
    """Write an immutable call log record."""
    db = get_db()
    container = db.get_container_client("call-logs")
    container.upsert_item(call_log)


# ============================================================
# FORMATTING — Prepare data for prompt injection
# ============================================================

def format_knowledge_for_prompt(items: list[dict]) -> str:
    """Format knowledge base items into prompt-ready text."""
    if not items:
        return "No data available."

    lines = []
    for item in items:
        lines.append(f"### {item.get('title', item['id'])}")
        lines.append(f"ID: {item['id']}")
        if item.get("subcategory"):
            lines.append(f"Topic: {item['subcategory']}")
        lines.append(item["content"])
        if item.get("follow_up_action"):
            lines.append(f"Suggested follow-up: {item['follow_up_action']}")
        lines.append("")

    return "\n".join(lines)