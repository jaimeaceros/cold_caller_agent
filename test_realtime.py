"""
Realtime API test harness.

Thin wrapper around agent.realtime.RealtimeSession.
All data access, prompt assembly, and session tracking live in the agent package.

Usage:
    python test_realtime.py [lead_id]                # Quick 2-turn test
    python test_realtime.py interactive [lead_id]    # You play the prospect
"""

import asyncio
import sys
import time
import uuid

from dotenv import load_dotenv

load_dotenv()

from agent.realtime import RealtimeSession, WS_URL, DEPLOYMENT

print(f"🔧 Realtime API Test Harness")
print(f"   WebSocket: {WS_URL}")
print(f"   Deployment: {DEPLOYMENT}")


# ============================================================
# TEST MODES
# ============================================================

async def run_quick_test(lead_id: str = "lead_001"):
    """Quick test: connect, configure, get greeting, one turn."""
    print(f"\n{'='*60}")
    print(f"QUICK TEST: Realtime API with lead {lead_id}")
    print(f"{'='*60}")

    session = RealtimeSession()

    try:
        await session.connect()

        # Init call session from Cosmos data
        session_id = f"rt_test_{uuid.uuid4().hex[:8]}"
        call = session.init_call_session(session_id, lead_id)
        print(f"\n📝 System prompt: {len(call.system_prompt)} chars")
        await session.configure()

        # Get greeting
        print(f"\n{'─'*40} Turn 1: Greeting {'─'*40}")
        await session.send_text()
        t0 = time.time()
        response = await session.process_events()
        latency = time.time() - t0

        print(f"\n🗣️  AGENT: \"{response}\"")
        print(f"⏱️  Latency: {latency:.2f}s")

        # Simulate one prospect turn
        prospect_msg = "Yeah this is Sarah, who's calling?"
        print(f"\n{'─'*40} Turn 2 {'─'*40}")
        print(f"\n👤 PROSPECT: \"{prospect_msg}\"")

        await session.send_text(prospect_msg)
        t0 = time.time()
        response = await session.process_events()
        latency = time.time() - t0

        print(f"\n🗣️  AGENT: \"{response}\"")
        print(f"⏱️  Latency: {latency:.2f}s")

        # Summary
        print(f"\n{'='*60}")
        print(f"RESULTS")
        print(f"{'='*60}")
        print(f"Session turns: {len(call.history)}")
        print(f"Metadata reports: {len(session.metadata_log)}")
        print(f"Knowledge queries: {len(session.knowledge_queries)}")
        print(f"Current phase: {call.current_phase}")

        if session.metadata_log:
            print(f"\nMetadata collected:")
            for i, m in enumerate(session.metadata_log):
                print(f"  [{i+1}] phase={m.get('current_phase')} "
                      f"sentiment={m.get('prospect_sentiment')} "
                      f"next={m.get('next_move', 'N/A')[:80]}")

        if call.cumulative_qualifying:
            filled = {k: v for k, v in call.cumulative_qualifying.items() if v}
            if filled:
                print(f"\nQualifying data: {filled}")

    finally:
        await session.close()


async def run_interactive(lead_id: str = "lead_001"):
    """Interactive mode: you play the prospect."""
    print(f"\n{'='*60}")
    print(f"INTERACTIVE CALL: Realtime API with lead {lead_id}")
    print(f"Type prospect responses. Type 'quit' to end.")
    print(f"{'='*60}")

    session = RealtimeSession()

    try:
        await session.connect()

        session_id = f"rt_interactive_{uuid.uuid4().hex[:8]}"
        call = session.init_call_session(session_id, lead_id)
        await session.configure()

        turn = 0
        while not session.call_ended:
            turn += 1
            print(f"\n{'─'*40} Turn {turn} {'─'*40}")

            if turn == 1:
                await session.send_text()
            else:
                prospect_msg = input("\n👤 YOU (prospect): ").strip()
                if prospect_msg.lower() in ("quit", "exit", "q"):
                    break
                await session.send_text(prospect_msg)

            t0 = time.time()
            response = await session.process_events()
            latency = time.time() - t0

            print(f"\n🗣️  AGENT: \"{response}\"")
            print(f"   ⏱️  {latency:.2f}s")

        # Final summary
        print(f"\n{'='*60}")
        print(f"CALL SUMMARY")
        print(f"{'='*60}")

        summary = session.get_call_summary()
        if summary:
            print(f"Turns: {summary['total_turns']}")
            print(f"Duration: {summary['duration_seconds']:.1f}s")
            print(f"Final phase: {summary['final_phase']}")
            print(f"Outcome: {summary['call_outcome']}")

            filled = {k: v for k, v in summary['qualifying_data'].items() if v}
            if filled:
                print(f"Qualifying: {filled}")

            if summary['objections_raised']:
                print(f"Objections raised: {summary['objections_raised']}")
            if summary['objections_resolved']:
                print(f"Objections resolved: {summary['objections_resolved']}")

        print(f"Metadata reports: {len(session.metadata_log)}")
        print(f"Knowledge queries: {len(session.knowledge_queries)}")

        if session.call_outcome:
            print(f"\nCall outcome details:")
            print(f"  Outcome: {session.call_outcome.get('outcome')}")
            print(f"  Reason: {session.call_outcome.get('reason')}")
            print(f"  Summary: {session.call_outcome.get('summary')}")

    finally:
        await session.close()


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    args = sys.argv[1:]

    if args and args[0] == "interactive":
        lead = args[1] if len(args) > 1 else "lead_001"
        asyncio.run(run_interactive(lead))
    else:
        lead = args[0] if args else "lead_001"
        asyncio.run(run_quick_test(lead))
