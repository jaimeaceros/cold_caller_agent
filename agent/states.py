from enum import Enum
from dataclasses import dataclass, field

class CallState(str, Enum):

    # Each of the following states represents a pahse in the sales call. Agent must behave differently on each state
    # Different state => Different prompt => Different knowledge retrieval

    GREETING = "GREETING"
    RAPPORT = "RAPPORT"
    DISCOVERY = "DISCOVERY"
    PITCH = "PITCH"
    OBJECTION = "OBJECTION" 
    CLOSE = "CLOSE"
    WRAP_UP = "WRAP_UP"
    VOICEMAIL = "VOICEMAIL"
    END = "END"

class Trigger(str, Enum):

    # These are the events that cause or generate state transitions
    # Greeting -> Rapport ---- Cause: user_prompt = "Hello, this is Jaime" and the
    
    # Greeting triggers:
    CORRECT_PERSON = "CORRECT_PERSON"
    WRONG_PERSON = "WRONG_PERSON"
    NOT_INTERESTED_EARLY = "NOT_INTERESTED_EARLY"

    # Rapport triggers:
    RAPPORT_ESTABLISHED = "RAPPORT_ESTABLISHED"

    # Discovery triggers:
    QUALIFIED = "QUALIFIED"
    DISQUALIFIED = "DISQUALIFIED"

    # Pitch triggers:
    OBJECTION_RAISED = "OBJECTION_RAISED"
    BUYING_SIGNAL = "BUYING_SIGNAL"

    # Objection triggers
    OBJECTION_RESOLVED = "OBJECTION_RESOLVED"
    OBJECTION_UNRESOLVED = "OBJECTION_UNRESOLVED"

    # Commitment triggers
    COMMITMENT_YES = "COMMITMENT_YES"
    COMMITMENT_NO = "COMMITMENT_NO"

    # No answer trigger
    NO_ANSWER = "NO_ANSWER"

    # Wrap up trigger
    WRAP_UP_COMPLETE = "WRAP_UP_COMPLETE"



TRANSITIONS: dict[tuple[CallState, Trigger], CallState] = {
    (CallState.GREETING, Trigger.CORRECT_PERSON):        CallState.RAPPORT,
    (CallState.GREETING, Trigger.WRONG_PERSON):          CallState.WRAP_UP,
    (CallState.GREETING, Trigger.NOT_INTERESTED_EARLY):  CallState.WRAP_UP,
    (CallState.GREETING, Trigger.NO_ANSWER):             CallState.VOICEMAIL,

    (CallState.RAPPORT, Trigger.RAPPORT_ESTABLISHED):    CallState.DISCOVERY,
    (CallState.RAPPORT, Trigger.NOT_INTERESTED_EARLY):   CallState.WRAP_UP,

    (CallState.DISCOVERY, Trigger.QUALIFIED):            CallState.PITCH,
    (CallState.DISCOVERY, Trigger.DISQUALIFIED):         CallState.WRAP_UP,
    (CallState.DISCOVERY, Trigger.OBJECTION_RAISED):     CallState.OBJECTION,

    (CallState.PITCH, Trigger.OBJECTION_RAISED):         CallState.OBJECTION,
    (CallState.PITCH, Trigger.BUYING_SIGNAL):            CallState.CLOSE,

    (CallState.OBJECTION, Trigger.OBJECTION_RESOLVED):   CallState.PITCH,
    (CallState.OBJECTION, Trigger.BUYING_SIGNAL):        CallState.CLOSE,
    (CallState.OBJECTION, Trigger.OBJECTION_UNRESOLVED): CallState.WRAP_UP,

    (CallState.CLOSE, Trigger.COMMITMENT_YES):           CallState.WRAP_UP,
    (CallState.CLOSE, Trigger.COMMITMENT_NO):            CallState.WRAP_UP,
    (CallState.CLOSE, Trigger.OBJECTION_RAISED):         CallState.OBJECTION,

    (CallState.WRAP_UP, Trigger.WRAP_UP_COMPLETE):       CallState.END,

    (CallState.VOICEMAIL, Trigger.WRAP_UP_COMPLETE):     CallState.END,
}


@dataclass
class StateConfig:
    description: str
    objective: str
    knowledge_categories: list[str]
    allowed_triggers: list[Trigger]
    max_agent_turns: int = 5  # --> dont get stuck in a state forever
    guidelines: list[str] = field(default_factory=list) # Behavior rules for each state
    timeout_trigger: Trigger | None = None  # trigger to fire automatically when max_agent_turns exceeded


STATE_CONFIGS: dict[CallState, StateConfig] = {

    CallState.GREETING: StateConfig(
        description="Initial contact with the prospect",
        objective="Introduce yourself, state the company, confirm you're speaking to the right person.",
        knowledge_categories=["company_specific"],
        allowed_triggers=[
            Trigger.CORRECT_PERSON,
            Trigger.WRONG_PERSON,
            Trigger.NOT_INTERESTED_EARLY,
            Trigger.NO_ANSWER,
        ],
        guidelines=[
            "Keep it under 15 seconds",
            "Use prospect's first name",
            "Don't pitch yet — just confirm identity",
            "If gatekeeper, ask to be connected",
        ],
        timeout_trigger=Trigger.NO_ANSWER,
    ),

    CallState.RAPPORT: StateConfig(
        description="Build brief connection before business talk",
        objective="Reference a personalization hook (news, LinkedIn, hiring) to show you did your homework. Transition naturally to business.",
        knowledge_categories=["company_specific"],
        allowed_triggers=[
            Trigger.RAPPORT_ESTABLISHED,
            Trigger.NOT_INTERESTED_EARLY,
        ],
        max_agent_turns=3,
        guidelines=[
            "Keep it to 1-2 exchanges max — don't force small talk",
            "Use the pre-call research hook",
            "Transition with: 'The reason I'm calling is...'",
        ],
        timeout_trigger=Trigger.RAPPORT_ESTABLISHED,
    ),

    CallState.DISCOVERY: StateConfig(
        description="Qualify the prospect by asking about their situation",
        objective="Understand their current pain, budget, authority, timeline (BANT). Determine if they're a fit.",
        knowledge_categories=["qualifying_criteria"],
        allowed_triggers=[
            Trigger.QUALIFIED,
            Trigger.DISQUALIFIED,
            Trigger.OBJECTION_RAISED,
        ],
        max_agent_turns=5,
        guidelines=[
            "Ask open-ended questions",
            "Let the prospect talk — aim for 60%+ prospect talk time",
            "Listen for pain signals to use in the pitch",
            "Don't pitch prematurely — gather info first",
        ],
        timeout_trigger=Trigger.QUALIFIED,
    ),

    CallState.PITCH: StateConfig(
        description="Present value proposition tailored to discovered pain",
        objective="Connect their specific pain to your solution. Use case studies if relevant.",
        knowledge_categories=["product_knowledge", "case_studies", "competitor_intelligence"],
        allowed_triggers=[
            Trigger.OBJECTION_RAISED,
            Trigger.BUYING_SIGNAL,
        ],
        guidelines=[
            "Lead with their pain, not your features",
            "Use specific numbers from case studies",
            "Keep it concise — no monologues",
            "Ask a check-in question after key claims",
        ],
        timeout_trigger=Trigger.BUYING_SIGNAL,
    ),

    CallState.OBJECTION: StateConfig(
        description="Handle prospect pushback or concerns",
        objective="Acknowledge the concern, address it with evidence, and guide back to value.",
        knowledge_categories=["objection_handling", "case_studies", "competitor_intelligence"],
        allowed_triggers=[
            Trigger.OBJECTION_RESOLVED,
            Trigger.BUYING_SIGNAL,
            Trigger.OBJECTION_UNRESOLVED,
        ],
        guidelines=[
            "Never argue — acknowledge first ('I hear you')",
            "Use the feel-felt-found pattern when appropriate",
            "Back claims with specific data from knowledge base",
            "If objection persists after 2 attempts, gracefully move on",
        ],
        timeout_trigger=Trigger.OBJECTION_UNRESOLVED,
    ),

    CallState.CLOSE: StateConfig(
        description="Ask for commitment (meeting, demo, next step)",
        objective="Propose a specific next step with a specific time. Make it easy to say yes.",
        knowledge_categories=["product_knowledge"],
        allowed_triggers=[
            Trigger.COMMITMENT_YES,
            Trigger.COMMITMENT_NO,
            Trigger.OBJECTION_RAISED,
        ],
        guidelines=[
            "Offer a specific time: 'How about Thursday at 2pm?'",
            "Keep the ask small: 15-minute demo, not a 1-hour meeting",
            "If they hesitate, offer an alternative (send info first)",
        ],
        timeout_trigger=Trigger.COMMITMENT_NO,
    ),

    CallState.WRAP_UP: StateConfig(
        description="End the call gracefully",
        objective="Confirm any next steps, thank the prospect, leave a positive impression.",
        knowledge_categories=["compliance_rules"],
        allowed_triggers=[Trigger.WRAP_UP_COMPLETE],
        guidelines=[
            "Summarize what was agreed",
            "Confirm email for follow-up",
            "Thank them for their time",
            "Always end positively, even on a rejection",
        ],
    ),

    CallState.VOICEMAIL: StateConfig(
        description="Leave a voicemail message",
        objective="Leave a short, compelling voicemail that gives a reason to call back.",
        knowledge_categories=["company_specific", "product_knowledge"],
        allowed_triggers=[Trigger.WRAP_UP_COMPLETE],
        max_agent_turns=1,
        guidelines=[
            "Under 30 seconds",
            "Name, company, one hook, callback number",
            "Don't pitch the full product",
        ],
    ),

    CallState.END: StateConfig(
        description="Call is complete",
        objective="N/A — terminal state",
        knowledge_categories=[],
        allowed_triggers=[],
    ),
}

# This class allegedly (allegedly because i cant code so im not sure) tracks the curent callstate and apply the correct transition

# UPDATE: i love claude code <3
class StateMachine():
    def __init__(self, initial_state: CallState = CallState.GREETING):
        self.current_state = initial_state
        self.history: list[tuple[CallState, Trigger, CallState]] = []
        self._turns_in_state: int = 0

    @property
    def config(self) -> StateConfig:
        # Get the config for the current state.
        return STATE_CONFIGS[self.current_state]

    @property
    def is_terminal(self) -> bool:
        return self.current_state == CallState.END

    def get_valid_triggers(self) -> list[Trigger]:
        """What triggers are valid from the current state? 
        Pass these to the LLM so it only picks from valid options."""
        return self.config.allowed_triggers

    def transition(self, trigger: Trigger) -> CallState:
        """
        Apply a trigger and move to the next state.
        Raises ValueError if the transition is invalid — this means
        the LLM classified a trigger that doesn't make sense for the
        current state. Your brain.py should handle this gracefully.
        """
        key = (self.current_state, trigger)

        if key not in TRANSITIONS:
            raise ValueError(
                f"Invalid transition: {self.current_state.value} + {trigger.value}. "
                f"Valid triggers from {self.current_state.value}: "
                f"{[t.value for t in self.get_valid_triggers()]}"
            )

        old_state = self.current_state
        self.current_state = TRANSITIONS[key]
        self.history.append((old_state, trigger, self.current_state))
        self._turns_in_state = 0

        return self.current_state

    def increment_turn(self) -> CallState | None:
        """
        Call this each time the agent speaks in the current state.
        If max turns exceeded and timeout_trigger is configured,
        automatically fires the transition and returns the new state.
        Returns None if no timeout occurred.
        """
        self._turns_in_state += 1
        if self._turns_in_state < self.config.max_agent_turns:
            return None

        timeout = self.config.timeout_trigger
        if timeout is not None and self.can_transition(timeout):
            return self.transition(timeout)

        return None

    def can_transition(self, trigger: Trigger) -> bool:
        """Check if a trigger is valid without applying it."""
        return (self.current_state, trigger) in TRANSITIONS


# --- Consistency validation ---
# Runs at import time: catches mismatches between STATE_CONFIGS and TRANSITIONS early.

def _validate_consistency() -> None:
    for state, config in STATE_CONFIGS.items():
        for trigger in config.allowed_triggers:
            if (state, trigger) not in TRANSITIONS:
                raise AssertionError(
                    f"STATE_CONFIGS[{state.value}] lists trigger {trigger.value} "
                    f"in allowed_triggers but TRANSITIONS has no entry for "
                    f"({state.value}, {trigger.value})"
                )
        if config.timeout_trigger is not None:
            if config.timeout_trigger not in config.allowed_triggers:
                raise AssertionError(
                    f"STATE_CONFIGS[{state.value}] has timeout_trigger "
                    f"{config.timeout_trigger.value} which is not in its allowed_triggers"
                )

    for (state, trigger) in TRANSITIONS:
        if state not in STATE_CONFIGS:
            raise AssertionError(
                f"TRANSITIONS contains state {state.value} "
                f"which has no entry in STATE_CONFIGS"
            )
        if trigger not in STATE_CONFIGS[state].allowed_triggers:
            raise AssertionError(
                f"TRANSITIONS has entry ({state.value}, {trigger.value}) "
                f"but {trigger.value} is not in "
                f"STATE_CONFIGS[{state.value}].allowed_triggers"
            )

#_validate_consistency()
