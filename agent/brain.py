import json
import os
from dataclasses import dataclass, field
from pydantic import BaseModel

from groq import Groq
from dotenv import load_dotenv

from agent.states import StateMachine, CallState, Trigger
from agent.knowledge import KnowledgeBase, RetrievedEntry
from agent.prompts.brain_base import BASE_SYSTEM_PROMPT
from agent.prompts.state_prompts import get_state_prompt

load_dotenv()


# ---------------------------------------------------------------------------
# 1. CALL CONTEXT — all the info about who we're calling
# ---------------------------------------------------------------------------

class CallContext(BaseModel):
    """
    Pre-call information about the prospect.
    In production, this comes from CRM + enrichment APIs.
    For now, you pass it manually or load from a JSON file.
    """
    # Agent info
    agent_name: str = "Sarah"
    company_name: str = "SalesPilot"
    product_name: str = "SalesPilot AI — AI-powered outbound sales platform"

    # Prospect info
    prospect_name: str = "James"
    prospect_title: str = "VP Sales"
    prospect_company: str = "TechCorp"
    prospect_industry: str = "SaaS"
    prospect_company_size: str = "50"

    # Personalization
    personalization_hook: str = "Recently posted 3 SDR job openings on LinkedIn"
    pain_hypothesis: str = "Scaling outbound is hard with a small team"


# ---------------------------------------------------------------------------
# 2. CONVERSATION TURN — a single exchange in the call
# ---------------------------------------------------------------------------

@dataclass
class ConversationTurn:
    role: str          # "prospect" or "agent"
    message: str       # What was said
    state: str         # What state the machine was in during this turn


# ---------------------------------------------------------------------------
# 3. LLM RESPONSE — parsed output from the model
# ---------------------------------------------------------------------------

@dataclass
class LLMResponse:
    trigger: str              # The trigger the LLM classified (or "NONE")
    response: str             # What the agent says to the prospect
    internal_reasoning: str   # Why the LLM made this choice
    raw: str                  # The raw LLM output (for debugging)


# ---------------------------------------------------------------------------
# 4. THE BRAIN
# ---------------------------------------------------------------------------

class AgentBrain:
    """
    Orchestrates a single cold call conversation.

    Usage:
        brain = AgentBrain(
            knowledge_path="knowledge_base/seed_data/knowledge_base.json",
            call_context=CallContext(),
        )

        # Agent opens the call
        opening = brain.start_call()
        print(f"Agent: {opening}")

        # Conversation loop
        while not brain.is_call_over:
            prospect_input = input("Prospect: ")
            response = brain.process_turn(prospect_input)
            print(f"Agent: {response}")
    """

    def __init__(
        self,
        knowledge_path: str,
        call_context: CallContext | None = None,
        model: str = "llama-3.3-70b-versatile",
    ):
        self.context = call_context or CallContext()
        self.state_machine = StateMachine()
        self.knowledge = KnowledgeBase(knowledge_path)
        self.history: list[ConversationTurn] = []
        self.model = model

        # Groq client — reads GROQ_API_KEY from environment
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            raise ValueError("GROQ_API_KEY not found in environment. Add it to your .env file.")
        self.llm_client = Groq(api_key=api_key)

    @property
    def is_call_over(self) -> bool:
        return self.state_machine.is_terminal

    @property
    def current_state(self) -> CallState:
        return self.state_machine.current_state

    # -------------------------------------------------------------------
    # PUBLIC METHODS
    # -------------------------------------------------------------------

    def start_call(self) -> str:
        """
        Generate the agent's opening line (GREETING state).
        Call this once at the start — no prospect input needed.
        """
        system_prompt = self._build_system_prompt(retrieved_knowledge="")

        # For the opening, there's no prospect message yet.
        # We tell the LLM to generate the first line of the call.
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "[Call connected. Introduce yourself and confirm you're speaking to the right person.]"},
        ]

        llm_response = self._call_llm(messages)

        self.history.append(ConversationTurn(
            role="agent",
            message=llm_response.response,
            state=self.current_state.value,
        ))

        return llm_response.response

    def process_turn(self, prospect_message: str) -> str:
        """
        Process one turn of the conversation.

        Takes what the prospect said, returns what the agent says.
        Handles state transitions internally.
        """
        if self.is_call_over:
            return "[Call has ended]"

        # Record prospect's message
        self.history.append(ConversationTurn(
            role="prospect",
            message=prospect_message,
            state=self.current_state.value,
        ))

        # Retrieve relevant knowledge for this turn
        knowledge_entries = self._retrieve_knowledge(prospect_message)
        knowledge_text = self._format_knowledge(knowledge_entries)

        # Build full prompt
        system_prompt = self._build_system_prompt(retrieved_knowledge=knowledge_text)
        messages = self._build_messages(system_prompt)

        # Call LLM
        llm_response = self._call_llm(messages)

        # Apply state transition if the LLM classified a trigger
        self._apply_transition(llm_response.trigger)

        # Check if agent is stuck in a state too long
        stuck = self.state_machine.increment_turn()
        if stuck and not self.is_call_over:
            # Force a graceful transition if stuck
            self._handle_stuck_state()

        # Record agent's response
        self.history.append(ConversationTurn(
            role="agent",
            message=llm_response.response,
            state=self.current_state.value,
        ))

        return llm_response.response

    # -------------------------------------------------------------------
    # PRIVATE: PROMPT ASSEMBLY
    # -------------------------------------------------------------------

    def _build_system_prompt(self, retrieved_knowledge: str) -> str:
        """Assemble base prompt + state-specific prompt."""

        # Valid triggers as a readable string
        valid_triggers = ", ".join(
            t.value for t in self.state_machine.get_valid_triggers()
        )
        # Add NONE as an option (stay in current state)
        if valid_triggers:
            valid_triggers += ", NONE"
        else:
            valid_triggers = "NONE"

        # Base prompt with all context filled in
        base = BASE_SYSTEM_PROMPT.format(
            agent_name=self.context.agent_name,
            company_name=self.context.company_name,
            product_name=self.context.product_name,
            prospect_name=self.context.prospect_name,
            prospect_title=self.context.prospect_title,
            prospect_company=self.context.prospect_company,
            prospect_industry=self.context.prospect_industry,
            prospect_company_size=self.context.prospect_company_size,
            personalization_hook=self.context.personalization_hook,
            pain_hypothesis=self.context.pain_hypothesis,
            valid_triggers=valid_triggers,
        )

        # State-specific prompt
        state_prompt = get_state_prompt(
            state=self.current_state,
            agent_name=self.context.agent_name,
            company_name=self.context.company_name,
            product_name=self.context.product_name,
            prospect_name=self.context.prospect_name,
            prospect_company=self.context.prospect_company,
            personalization_hook=self.context.personalization_hook,
            retrieved_knowledge=retrieved_knowledge,
        )

        return base + "\n\n" + state_prompt

    def _build_messages(self, system_prompt: str) -> list[dict]:
        """
        Build the messages array for the LLM call.
        Converts conversation history into the chat format.
        """
        messages = [{"role": "system", "content": system_prompt}]

        for turn in self.history:
            if turn.role == "prospect":
                messages.append({"role": "user", "content": turn.message})
            elif turn.role == "agent":
                # Wrap previous agent responses back in the JSON format
                # so the LLM sees a consistent pattern
                messages.append({
                    "role": "assistant",
                    "content": json.dumps({
                        "trigger": "NONE",
                        "response": turn.message,
                        "internal_reasoning": "Previous turn.",
                    }),
                })

        return messages

    # -------------------------------------------------------------------
    # PRIVATE: KNOWLEDGE RETRIEVAL
    # -------------------------------------------------------------------

    def _retrieve_knowledge(self, prospect_message: str) -> list[RetrievedEntry]:
        """
        Fetch relevant knowledge based on what the prospect said
        and what state we're in.
        """
        categories = self.state_machine.config.knowledge_categories

        results = self.knowledge.retrieve(
            query=prospect_message,
            categories=categories,
        )

        # If no scored results came back but we have categories that
        # use reference material (like qualifying_criteria), fetch by
        # category directly. These entries have empty trigger_phrases.
        if not any(r.category != "compliance_rules" for r in results):
            for entry in self.knowledge.entries:
                if entry.get("category") in categories:
                    results.append(RetrievedEntry(
                        id=entry.get("id", ""),
                        category=entry.get("category", ""),
                        subcategory=entry.get("subcategory", ""),
                        content=entry.get("content", ""),
                        follow_up_action=entry.get("follow_up_action"),
                        score=0.5,
                    ))
            # Deduplicate by id
            seen = set()
            unique = []
            for r in results:
                if r.id not in seen:
                    seen.add(r.id)
                    unique.append(r)
            results = unique

        return results

    def _format_knowledge(self, entries: list[RetrievedEntry]) -> str:
        """Format retrieved knowledge entries into readable text for the prompt."""
        if not entries:
            return "No specific knowledge retrieved for this turn."

        sections = []
        for entry in entries:
            # Skip compliance in the knowledge display — it's already
            # enforced via the base prompt hard rules
            if entry.category == "compliance_rules":
                continue
            sections.append(
                f"[{entry.category}/{entry.subcategory}] {entry.content}"
            )

        if not sections:
            return "No specific knowledge retrieved for this turn."

        return "\n\n".join(sections)

    # -------------------------------------------------------------------
    # PRIVATE: LLM CALL
    # -------------------------------------------------------------------

    def _call_llm(self, messages: list[dict]) -> LLMResponse:
        """
        Call Groq and parse the structured JSON response.
        Handles malformed responses gracefully.
        """
        try:
            completion = self.llm_client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0.7,
                max_tokens=300,
                response_format={"type": "json_object"},
            )

            raw_content = completion.choices[0].message.content or ""
            parsed = json.loads(raw_content)

            return LLMResponse(
                trigger=parsed.get("trigger", "NONE"),
                response=parsed.get("response", "I'm sorry, could you repeat that?"),
                internal_reasoning=parsed.get("internal_reasoning", ""),
                raw=raw_content,
            )

        except json.JSONDecodeError:
            # LLM didn't return valid JSON — use raw text as response
            return LLMResponse(
                trigger="NONE",
                response=raw_content if raw_content else "I'm sorry, could you repeat that?",
                internal_reasoning="Failed to parse JSON from LLM response.",
                raw=raw_content,
            )

        except Exception as e:
            # Network error, rate limit, etc.
            import sys
            print(f"[LLM ERROR] {type(e).__name__}: {e}", file=sys.stderr)
            return LLMResponse(
                trigger="NONE",
                response="I'm having a brief technical issue. Could you give me one moment?",
                internal_reasoning=f"LLM call failed: {str(e)}",
                raw=str(e),
            )

    # -------------------------------------------------------------------
    # PRIVATE: STATE TRANSITIONS
    # -------------------------------------------------------------------

    def _apply_transition(self, trigger_str: str) -> None:
        """
        Apply the trigger the LLM classified.
        If invalid or NONE, stay in current state.
        """
        if trigger_str == "NONE" or not trigger_str:
            return

        # Try to match the string to a valid Trigger enum
        try:
            trigger = Trigger(trigger_str)
        except ValueError:
            # LLM returned a trigger string that doesn't exist in the enum
            return

        # Check if this transition is valid from the current state
        if self.state_machine.can_transition(trigger):
            self.state_machine.transition(trigger)

    def _handle_stuck_state(self) -> None:
        """
        Force a transition when the agent has been in one state too long.
        This is a safety valve — the agent should naturally transition,
        but if it doesn't, we push it forward.
        """
        state = self.current_state

        # Define fallback transitions for when the agent gets stuck
        fallback_triggers = {
            CallState.RAPPORT: Trigger.RAPPORT_ESTABLISHED,
            CallState.DISCOVERY: Trigger.QUALIFIED,
            CallState.PITCH: Trigger.BUYING_SIGNAL,
            CallState.OBJECTION: Trigger.OBJECTION_UNRESOLVED,
            CallState.CLOSE: Trigger.COMMITMENT_NO,
        }

        fallback = fallback_triggers.get(state)
        if fallback and self.state_machine.can_transition(fallback):
            self.state_machine.transition(fallback)

    # -------------------------------------------------------------------
    # UTILITY
    # -------------------------------------------------------------------

    def get_call_summary(self) -> dict:
        """
        Generate a summary of the call for logging/CRM.
        Call this after the conversation ends.
        """
        return {
            "prospect": {
                "name": self.context.prospect_name,
                "title": self.context.prospect_title,
                "company": self.context.prospect_company,
            },
            "outcome": self._determine_outcome(),
            "total_turns": len(self.history),
            "states_visited": [t[0].value for t in self.state_machine.history],
            "transitions": [
                {
                    "from": t[0].value,
                    "trigger": t[1].value,
                    "to": t[2].value,
                }
                for t in self.state_machine.history
            ],
            "conversation": [
                {"role": t.role, "message": t.message, "state": t.state}
                for t in self.history
            ],
        }

    def _determine_outcome(self) -> str:
        """Determine call outcome based on transition history."""
        triggers_used = [t[1] for t in self.state_machine.history]

        if Trigger.COMMITMENT_YES in triggers_used:
            return "meeting_booked"
        if Trigger.NO_ANSWER in triggers_used:
            return "voicemail_left"
        if Trigger.DISQUALIFIED in triggers_used:
            return "disqualified"
        if Trigger.NOT_INTERESTED_EARLY in triggers_used:
            return "rejected_early"
        if Trigger.OBJECTION_UNRESOLVED in triggers_used:
            return "objection_unresolved"
        if Trigger.COMMITMENT_NO in triggers_used:
            return "declined"

        return "unknown"