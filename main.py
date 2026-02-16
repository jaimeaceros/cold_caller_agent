from agent.brain import AgentBrain, CallContext
import json



def main():
    context = CallContext(
        agent_name="Sarah",
        company_name="SalesPilot",
        product_name="SalesPilot AI — AI-powered outbound sales platform",
        prospect_name="James",
        prospect_title="VP Sales",
        prospect_company="TechCorp",
        prospect_industry="SaaS",
        prospect_company_size="50",
        personalization_hook="Recently posted 3 SDR job openings on LinkedIn",
        pain_hypothesis="Scaling outbound is hard with a small team",
    )

    brain = AgentBrain(
        knowledge_path="knowledge_base/seed_data/knowledge_base.json",
        call_context = context,
        )

    print("=" * 60)
    print("COLD CALLER AGENT — Text Test Mode")
    print("=" * 60)
    print(f"Calling: {context.prospect_name} ({context.prospect_title} at {context.prospect_company})")
    print(f"State: {brain.current_state.value}")
    print("-" * 60)

    # Agent opens the call
    opening = brain.start_call()
    print(f"\nAgent: {opening}")
    print(f"  [State: {brain.current_state.value}]")


    while not brain.is_call_over:
        print()
        prospect_input = input("Prospect: ").strip()

        if not prospect_input:
            continue

        # Debug commands
        if prospect_input.lower() == "quit":
            break
        if prospect_input.lower() == "state":
            print(f"  [Current state: {brain.current_state.value}]")
            print(f"  [Valid triggers: {[t.value for t in brain.state_machine.get_valid_triggers()]}]")
            continue
        if prospect_input.lower() == "summary":
            print(json.dumps(brain.get_call_summary(), indent=2))
            continue

        # Process the turn
        response = brain.process_turn(prospect_input)
        print(f"\nAgent: {response}")
        print(f"  [State: {brain.current_state.value}]")

    # Call ended
    print("\n" + "=" * 60)
    print("CALL ENDED")
    print("=" * 60)
    summary = brain.get_call_summary()
    print(f"Outcome: {summary['outcome']}")
    print(f"Total turns: {summary['total_turns']}")
    print(f"States visited: {' → '.join(summary['states_visited'])}")
    print()



if __name__ == "__main__":
    main()