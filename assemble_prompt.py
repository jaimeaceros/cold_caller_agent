"""
Assemble a fully populated system prompt + tool definitions for Azure AI Foundry Playground.

Usage:
    python assemble_prompt.py                  # Default: lead_001
    python assemble_prompt.py --lead lead_002  # Specific lead
    python assemble_prompt.py --tools-only     # Just print tool JSON

Workflow:
    1. Run this script → copy the system prompt
    2. Open Azure AI Foundry → Playground → Realtime
    3. Paste the system prompt
    4. Add the 3 tool definitions (printed as JSON)
    5. Talk via browser mic
"""

import argparse
import json
import sys

from dotenv import load_dotenv

load_dotenv()

from agent.cosmos import fetch_lead, fetch_all_knowledge, fetch_agent_config
from agent.realtime import assemble_realtime_prompt, inject_history, TOOLS


def main():
    parser = argparse.ArgumentParser(description="Assemble prompt for Azure AI Foundry Playground")
    parser.add_argument("--lead", default="lead_001", help="Lead ID (default: lead_001)")
    parser.add_argument("--tools-only", action="store_true", help="Only print tool definitions")
    args = parser.parse_args()

    if args.tools_only:
        print(json.dumps(TOOLS, indent=2))
        return

    # Fetch data from Cosmos
    lead = fetch_lead(args.lead)
    if not lead:
        print(f"Error: Lead '{args.lead}' not found in Cosmos DB", file=sys.stderr)
        sys.exit(1)

    knowledge = fetch_all_knowledge()
    agent_config = fetch_agent_config()

    # Assemble prompt
    base_prompt = assemble_realtime_prompt(lead, knowledge, agent_config)
    full_prompt = inject_history(base_prompt, [])

    # Output
    print("=" * 70)
    print("SYSTEM PROMPT")
    print("=" * 70)
    print(full_prompt)
    print()
    print("=" * 70)
    print("TOOL DEFINITIONS (paste into Azure Playground)")
    print("=" * 70)
    print(json.dumps(TOOLS, indent=2))
    print()
    print(f"Prompt length: {len(full_prompt)} chars")
    print(f"Lead: {lead['contact']['name']} @ {lead['company']['name']}")
    print(f"Tools: {len(TOOLS)}")


if __name__ == "__main__":
    main()
