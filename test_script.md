# Cold Caller Agent — Test Conversation Script

Use this as a guide when testing the agent. Each scenario tells you exactly what to send as the "prospect" at each step, and what behavior to expect from the agent.

---

## Setup

**Start a new call** (run this first for every scenario):

```
curl.exe -X POST http://localhost:8080/call/start -H "Content-Type: application/json" -d '{"session_id": "test1", "context": {"prospect_name": "James", "prospect_company": "TechCorp", "prospect_title": "VP Sales", "prospect_industry": "SaaS", "prospect_company_size": "60", "personalization_hook": "TechCorp posted 3 SDR job openings on LinkedIn last week", "pain_hypothesis": "Scaling outbound with a small team is hard"}}'
```

**Send a turn** (use this template for every prospect message):

```
curl.exe -X POST http://localhost:8080/call/turn -H "Content-Type: application/json" -d '{"session_id": "test1", "prospect_message": "YOUR MESSAGE HERE"}'
```

**Change session_id** for each new scenario (test2, test3, etc.) to avoid state collision.

---

## Scenario A — Happy Path (Meeting Booked)

**Goal:** Walk the full flow from greeting to booked meeting.
**Session ID:** `test1`

| Step | You say (prospect_message) | Expected state | Expected behavior |
|------|---------------------------|----------------|-------------------|
| Start call | *(call /call/start)* | GREETING | Warm intro, confirms your name, asks how you're doing |
| 1 | `Yeah, this is James. Doing well, thanks!` | GREETING | Genuine small talk response, NOT yet moving to RAPPORT |
| 2 | `Pretty busy week but can't complain.` | RAPPORT | Mentions the LinkedIn SDR postings, curious and natural |
| 3 | `Yeah, we're trying to grow the outbound team fast. It's a lot.` | RAPPORT | Empathizes, then transitions: "That's actually why I'm calling..." |
| 4 | `Sure, what's up?` | DISCOVERY | Asks an open-ended question about their outbound setup |
| 5 | `We have 3 SDRs right now but they spend way too much time on manual research and writing emails.` | DISCOVERY | Digs deeper: "How long has that been an issue?" or "What's the impact on quota?" |
| 6 | `It means they're maybe hitting 40-50 calls a day instead of 100+. We're leaving a lot on the table.` | PITCH | Connects their pain to the product, keeps it short, asks a check-in |
| 7 | `How does that work exactly?` | CLOSE | Buying signal detected — proposes a 15-min demo with a specific time |
| 8 | `Thursday at 2pm works for me.` | WRAP_UP | Confirms the time, asks for email to send invite, wraps up warmly |
| 9 | `james@techcorp.com` | END | Confirms the invite, says goodbye positively |

---

## Scenario B — Objection Path (Overcome → Book)

**Goal:** Test objection handling and recovery.
**Session ID:** `test2`

| Step | You say | Expected state | Expected behavior |
|------|---------|----------------|-------------------|
| Start call | *(call /call/start)* | GREETING | Warm intro |
| 1 | `This is James, hey.` | GREETING | Brief small talk |
| 2 | `Yeah not bad, thanks.` | RAPPORT | Drops the LinkedIn hook |
| 3 | `Yeah the hiring push is real. What are you calling about?` | DISCOVERY | Transitions quickly since they seem ready — asks about outbound setup |
| 4 | `We handle it all internally. We have a process.` | DISCOVERY | Curious follow-up: "What does that look like day to day?" |
| 5 | `We use Salesforce sequences mostly. It works okay.` | PITCH | Qualifies and pivots — "okay" is a signal. Pitches on top of "okay" → "what if okay became great?" |
| 6 | `We already invested a lot in Salesforce, to be honest.` | OBJECTION | Acknowledges the investment concern, uses feel-felt-found or competitor data |
| 7 | `Hmm. What kind of results are you talking about?` | CLOSE | Buys back in — proposes the demo |
| 8 | `Alright, send me a calendar invite.` | WRAP_UP | Confirms meeting, asks for email, warm wrap-up |

---

## Scenario C — Callback Requested

**Goal:** Test CALLBACK_REQUESTED trigger at different stages.
**Session ID:** `test3`

### C1: Callback at Greeting

| Step | You say | Expected state | Expected behavior |
|------|---------|----------------|-------------------|
| Start call | *(call /call/start)* | GREETING | Warm intro |
| 1 | `Hey, I'm actually in the middle of something — can you call me back tomorrow?` | WRAP_UP | Gracefully accepts, confirms callback time, does NOT pitch |

### C2: Callback Mid-Pitch

| Step | You say | Expected state | Expected behavior |
|------|---------|----------------|-------------------|
| Start call | *(new session, call /call/start)* | GREETING | Warm intro |
| 1 | `James speaking, good thanks.` | GREETING | Small talk |
| 2 | `Pretty hectic. What's this about?` | RAPPORT → DISCOVERY | Moves quickly |
| 3 | `We do struggle with outbound volume honestly.` | PITCH | Agent pitches |
| 4 | `This is interesting but I have to jump on another call — can we pick this up?` | WRAP_UP | Schedules callback, does NOT keep pitching |

---

## Scenario D — Early Rejection

**Goal:** Test early rejection handling — graceful exit, no guilt trip.
**Session ID:** `test4`

| Step | You say | Expected state | Expected behavior |
|------|---------|----------------|-------------------|
| Start call | *(call /call/start)* | GREETING | Warm intro |
| 1 | `Not interested. Please take me off your list.` | WRAP_UP | Immediately complies, apologizes, ends warmly — does NOT argue or push |

---

## Scenario E — More Info Requested

**Goal:** Test MORE_INFO_REQUESTED — agent sends materials and wraps up professionally.
**Session ID:** `test5`

| Step | You say | Expected state | Expected behavior |
|------|---------|----------------|-------------------|
| Start call | *(call /call/start)* | GREETING | Warm intro |
| 1 | `James here. Good, thanks.` | GREETING | Small talk |
| 2 | `What's this about?` | RAPPORT → DISCOVERY | Quick transition |
| 3 | `Yeah outbound is definitely a challenge for us.` | PITCH | Agent pitches |
| 4 | `Can you send me something to read? I'd rather review it before committing to a call.` | WRAP_UP | Accepts positively, asks for email, sets follow-up expectation |
| 5 | `Sure, james@techcorp.com` | END | Confirms email, explains what to expect, warm close |

---

## Scenario F — Disqualified

**Goal:** Test graceful exit when prospect is clearly not a fit.
**Session ID:** `test6`

| Step | You say | Expected state | Expected behavior |
|------|---------|----------------|-------------------|
| Start call | *(call /call/start)* | GREETING | Warm intro |
| 1 | `James, yeah. Fine thanks.` | RAPPORT | Hook reference |
| 2 | `We're a 3-person startup, no sales team.` | DISCOVERY | Probes a bit more |
| 3 | `We literally do zero outbound, it's all inbound. And no budget for tools right now.` | WRAP_UP | Gracefully disqualifies — thanks them, no hard sell, leaves door open for when they grow |

---

## Tips for Testing

- **State transitions** show up in the `"state"` field of the API response — use this to track where the agent thinks it is
- **Check `is_call_over`** — should be `true` only after `END` state
- **If the agent moves too fast** through GREETING, it may be firing `CORRECT_PERSON` too early — the prompt now instructs it to wait for at least one exchange of small talk
- **If the agent is stuck**, after `max_agent_turns` the state machine will auto-fire the `timeout_trigger` — you'll see the state jump
- **Use different session IDs** for each scenario to avoid carry-over state
