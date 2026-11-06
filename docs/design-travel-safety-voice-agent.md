# Design: "Atlas", an emergency travel safety voice agent

Status: hackathon design, v1 · Owner: Frank Yu · Date: 2026-11-06
Repo: `take2-voice-starter` (LiveKit Agents 1.8, LiveKit Inference, free plan)

**One-line pitch:** A traveler in trouble calls one phone number, any time, and in
under a minute gets a calm voice that works out where they are and what is
happening, then gives one clear decision: **stay put, leave by this route, or
call these people now.** It keeps checking on them until they are safe.

---

## 1. Problem and user

**Who calls**

- Business travelers and staff of companies with duty-of-care obligations.
  Global Rescue found 72% of business travelers don't have a duty-of-care
  policy, or don't know if they do
  ([Global Rescue via Security Magazine](https://www.securitymagazine.com/articles/89651-research-reveals-high-expectations-business-travelers-place-on-organizations-for-traveler-safety)).
- Executives, VIPs and their assistants or drivers. They are more exposed to
  targeted crime such as express kidnapping
  ([TorchStone](https://www.torchstoneglobal.com/express-kidnapping/)).
- Solo travelers, women in particular. In one survey 31% of women business
  travelers reported sexual harassment while traveling, and fewer than half
  report incidents to their employer
  ([Fleet News](https://www.fleetnews.co.uk/news/fleet-industry-news/2016/08/17/nearly-a-third-of-women-suffer-sexual-harassment-while-travelling-on-business)).

**When they call:** in the first minutes of a situation they don't understand.
Typical moments: a protest forming outside the hotel, a fire alarm at 2 AM, a
taxi driving the wrong way, being followed, a lost passport after a robbery, an
earthquake or flood, or a flight canceled during unrest.

**Why a voice agent:** a person in a stairwell or a back seat can't type,
search or read a map. Corporate security desks exist (International SOS, Global
Rescue), but most travelers don't have one, don't know the number, or are put
on hold. The agent's job is to:

1. answer at once, 24/7, in a calm voice,
2. pin down the caller's location and situation,
3. look up real data (nearby police, hospital, embassy, a walking route, local
   news),
4. give **one** decision with a reason, and
5. keep a check-in timer running and escalate to a human if the caller goes
   quiet.

**What "done" means on a call:** the caller has a clear next action, knows the
local emergency number, and either has a check-in timer running or has been
handed to a human or to emergency services.

---

## 2. Research summary

**What actually goes wrong.** In BCD Travel's November 2025 survey of 1,284
business travelers, the top concerns were health risks, crime and natural
disasters, each named by about a third. Weather was the most common incident
travelers actually had
([BCD Travel report](https://www.bcdtravel.com/wp-content/uploads/BCD-report_Traveler-insights_Travel-Risk-Management1772622957137-1.pdf)).
Transportation incidents also rank near the top
([Business Travel Executive](https://www.businesstravelexecutive.com/news/transportation-incidents-and-health-risk-top-concerns-for-business-travelers-says-survey/)).
**Design consequence:** the agent must handle "boring" emergencies (weather,
transport, lost documents) at least as well as dramatic ones.

**Shelter or leave.** Official guidance leans toward sheltering in place and
following local authorities. New Zealand's SafeTravel advises sheltering until
an all-clear is given
([SafeTravel NZ](https://www.safetravel.govt.nz/while-you-re-away/emergency-or-disaster/there-is-a-threat-of-violence-or-conflict)),
and embassies usually tell citizens to shelter during unrest
([Lonely Planet](https://www.lonelyplanet.com/articles/traveler-safety-during-protests)).
The exception is a threat that is close and open space or an exit is available.
Harvard's guidance is to leave the area at once if a protest breaks out nearby
([Harvard Global Support](https://www.globalsupport.harvard.edu/index%2ephp/travel/advice/civil-unrest-planning)).
Leaving early matters because transport can shut down at short notice
([SafeTravel NZ contingency planning](https://www.safetravel.govt.nz/contingency-planning-new-zealanders-overseas)).
A good shelter is a small interior room with few or no windows
([Fairfax County](https://www.fairfaxcounty.gov/emergency/sites/emergency/files/Assets/documents/CERGHazardAnnexPages/civil%20disorder%20PDF.pdf)).
**Design consequence:** default to SHELTER. Recommend LEAVE only when the threat
is close or getting closer **and** a route is open. Section 6 encodes this as
rules.

**Hotel fire.** Know two exits, count the doors to each, use the stairs and
never the lift, test a door for heat before opening it, and keep the room key
in case you have to go back
([Ohio State](https://oia.osu.edu/units/global-education/health-and-safety/traveler-safety/fire-safety),
[US Fire Administration archive](https://webarchive.library.unt.edu/web/20121016225250/http://www.usfa.fema.gov/citizens/hotel.shtm)).
Don't assume fire standards abroad match home. **Design consequence:** fire is a
scripted fast path that runs before any tool call.

**Kidnapping and taxis.** Express kidnappings often start with a street-hailed
taxi. Use dispatched or hotel cars and check the plate and the driver. If it
happens, don't resist: cooperate
([US Embassy Ecuador warden message](https://expatexchange.com/thread/202/3347833/Ecuador/Expats-Living-in-Ecuador/ACS-Message),
[Lloyd's](https://www.lloyds.com/news-and-insights/news/safer-travels-5-tips-to-avoid-being-an-easy-target),
[Aetna International](https://aetnainternational.com/en/about-us/explore/living-abroad/travel/kidnap-ransom-abroad.html)).
**Design consequence:** the agent never coaches resistance or confrontation. It
supports a **quiet, yes/no mode** for a caller who can't speak freely.

**Embassy and authorities.** US citizens can reach Overseas Citizens Services
24/7 at **+1 202 501 4444** from abroad, or **1 888 407 4747** from the US and
Canada
([Rep. Maloy](https://maloy.house.gov/services/middle-east-guidance.htm),
[UMD](https://marylandglobal.umd.edu/services/travel-safety/emergency-support)).
Local emergency numbers differ by country, and fire and police can use
different numbers ([Ohio State](https://oia.osu.edu/units/global-education/health-and-safety/traveler-safety/fire-safety)).
**Design consequence:** keep emergency numbers and embassy hotlines in a
**curated static table** that the team checks before the demo. Use live map
lookups only to find the *nearest* police station, hospital or embassy
building. Never let the LLM recall a phone number from memory.

**Maps APIs a voice agent can use.**

| Need | Free option for the hackathon | Paid upgrade | Notes |
|---|---|---|---|
| Geocode "Hotel X on Reforma" | Nominatim (OSM) | Google Geocoding | 1 request/s for the whole app, a descriptive User-Agent is required, and results should be cached ([Nominatim policy summary](https://moltchat-agent-commons.onrender.com/wiki/Nominatim_geocoding_usage_policy_and_rate_limits)) |
| Nearby police, hospital, embassy | Overpass API: `nwr(around:R,lat,lon)["amenity"="police"]` | Google Places Nearby, about $32 per 1,000 requests after 5,000 free a month ([Google pricing](https://developers.google.com/maps/billing-and-pricing/pricing?hl=fr)) | Embassies can be tagged `amenity=embassy` or `office=diplomatic` ([CARTO gist](https://gist.github.com/michellemho/282e26ae944f46cc313f63c9f4351b09), [Overpass around tutorial](https://osm-queries.ldodds.com/tutorial/12-radius-search.osm.html)) |
| Walking or driving route | OSRM demo server, foot and car profiles ([OSRM demo server](https://github.com/Project-OSRM/osrm-backend/wiki/Demo-server)) | Google Routes API | Demo server: at most 1 request/s, non-commercial only, no uptime guarantee ([OSRM usage policy](https://github.com/Project-OSRM/osrm-backend/wiki/Api-usage-policy)) |
| "What's happening near me" | Tavily Search, 1,000 free credits a month ([Tavily docs](https://docs.tavily.com/documentation/api-credits.md)) | Any news API | Read results back as *unverified reports* |

**Decision:** use OSM (Nominatim, Overpass, OSRM) plus Tavily. No credit card,
and each needs at most one API key. Every lookup gets a timeout of about 2.5 s
and a canned fallback, so the demo survives an outage of a free service.

**Human escalation on LiveKit.** LiveKit ships `WarmTransferTask` (beta, Python)
in `livekit.agents.beta.workflows`. It dials a human, briefs them, then merges
the calls. It needs an **outbound SIP trunk**
([LiveKit warm transfer docs](https://docs.livekit.io/agents/prebuilt/tasks/warm-transfer/)).
The free LiveKit phone number covers inbound calls only, so live transfer is a
**stretch goal**. The MVP escalation posts to an "ops desk" webhook that we show
on screen.

---

## 3. Persona, voice and tone

**Name:** Atlas. Introduces itself as an AI safety assistant. It never claims
to be a person, a police officer or an embassy official.

**Voice:** low, even, unhurried. Pick a calm voice and don't use a cheerful
receptionist voice. With the budget preset's expressive mode, allow only
"calm" or "reassuring" delivery tags. No excitement.

**Crisis-call rules** (these go in `JOB_PROMPT` and add to `prompts.py`
`VOICE_RULES`):

1. **Safety check first.** The first question is always: "Are you safe to talk
   right now, and is anyone hurt?" Someone hurt or in immediate danger: give
   the local emergency number **in the first reply**, before any lookup.
2. **One instruction per turn.** Short imperative sentences: "Stay in the
   room. Lock the door. Move away from the window."
3. **Reflect, then direct.** One clause to acknowledge ("That sounds
   frightening"), then the action. Never more than one empathy sentence in a
   row.
4. **Read back locations and numbers**, digit by digit for phone numbers.
5. **Quiet mode.** If the caller whispers, says "I can't talk", or gives
   one-word answers, switch to yes/no questions only: "Say yes if you're still
   in the hotel."
6. **Say what you know and how sure you are.** "Local news reports, which I
   can't confirm, say the march is moving west on Reforma."
7. **Never stall the caller.** Before a lookup, say a few words, for example
   "Checking the safest route now."
8. **Calm after a panicked interruption.** Drop the current sentence and answer
   the new information. Don't restart from the top (already in
   `DIFFICULT_CALLER_RULES`).

---

## 4. Architecture on this repo's stack

```
phone (PSTN) ─▶ LiveKit SIP ─▶ dispatch rule (agent_name=take2-agent)
   └▶ agent.py AgentSession
        ai-coustics noise cancellation ─▶ STT ─▶ turn detector ─▶ LLM + tools ─▶ TTS
                                                                   │
                     ┌──────────────── SafetyAgent tools ──────────┴───────────────┐
                     │ locate_caller → Nominatim     find_nearby_help → Overpass   │
                     │ get_route → OSRM              search_local_news → Tavily    │
                     │ get_emergency_contacts → static table (data/emergency.json) │
                     │ assess_situation → deterministic rules (no network)         │
                     │ start_check_in / confirm_safe → asyncio timer               │
                     │ escalate_to_human → ops webhook (+ WarmTransferTask later)  │
                     └─────────────────────────────────────────────────────────────┘
```

**Pipeline and model preset.** Add a third preset, `safety`, to
`src/models.py`:

| Stage | Model | Why |
|---|---|---|
| STT | `deepgram/flux-general-en` | Built-in end-of-turn handling. The README recommends it when callers get cut off, and frightened callers interrupt and trail off a lot. |
| LLM | `google/gemini-3.1-flash-lite` | Calls tools more reliably than Gemma for an 8-tool agent, with low latency. Same as the `quality` preset. |
| TTS | `cartesia/sonic-3` with a calm voice ID | Low time to first audio. Turn `expressive=False` off unless a test shows calm tags help. |

Iterate on `budget` with the text debugger and switch to `safety` for
simulations and the demo. That saves credits.

**Tool registration pattern.** Same as `my_agent.py` and `booking_agent.py`.
Each tool is an `@function_tool` method on the `Agent` subclass, and its
docstring is the instruction the LLM reads. Per-call state lives on the agent
instance (`self.state`, as `BookingAgent` keeps `self.calendar`), so
`agent.py` doesn't need `userdata`. `EndCallTool(ignore_on_enter=True)` stays
in `tools=[...]`.

**Workflow choice.** Use **one agent with 8 tools** for v1. Gemini Flash Lite
handles that many, and a handoff adds latency at the worst moment. If the
debugger shows tool confusion, split the agent: a `TriageTask(AgentTask)`
collects safety, location and situation, then hands a structured result to the
`SafetyAgent`. AGENTS.md recommends this split.

**Latency tactics**

- **Prefetch.** When `locate_caller` succeeds, start a background Overpass
  query for the police stations, hospitals and embassies within 2 km and cache
  the result. A later `find_nearby_help` call returns in about 0 ms.
- Use one shared `aiohttp.ClientSession`. `aiohttp` already comes with
  `livekit-agents`, so there's no new dependency. Set `timeout=2.5s` on every
  call and fall back to cached or static data.
- Tools return **speakable text**: short sentences, numbers already rounded,
  and at most three route steps.

**Caller identity.** On SIP calls the caller's number is in the participant
attribute `sip.phoneNumber`. In `on_enter`, look it up in
`data/travelers.json`, a demo profile with name, nationality, hotel, emergency
contact and a code word. The agent then greets the caller by first name and
already knows their hotel. A sign-up flow is out of scope.

**Silence handling: a required change.** `agent.py` currently hangs up after
two check-ins of 8 seconds each. For a caller who may be hiding, that's the
**wrong** behavior. Change: if the active agent has
`hang_up_on_silence = False`, the silence handler doesn't hang up. It switches
to quiet mode ("If you can't talk, stay on the line. I'm still here."), and on
the second missed check-in it calls `escalate_to_human` instead of `shutdown()`.

**Dropped call = assume the worst.** In `on_enter`, register
`self.session.on("close", ...)`. If the session closes while a safety plan is
active and the caller hasn't said they're safe, fire the escalation webhook at
once with the last known location and plan.

---

## 5. Tool set

All tools live in `src/safety_agent.py`. The network helpers are in
`src/safety_tools.py` so they can be tested on their own. Every tool returns a
`str` meant to be spoken.

| # | Tool | Purpose | Backend |
|---|---|---|---|
| 1 | `locate_caller(description)` | Turn "Hotel Galeria on Reforma" or "near the big golden angel statue" into lat/lon and a country code, then read it back | Nominatim, plus a demo-city cache |
| 2 | `get_emergency_contacts(country_code, nationality)` | Local police, ambulance and fire numbers, plus the caller's embassy hotline | Static `data/emergency.json` |
| 3 | `find_nearby_help(kind, radius_m=2000)` | Nearest police station, hospital, embassy or large hotel. Returns name, distance and direction | Overpass, prefetched |
| 4 | `get_route(destination, mode)` | Walking or driving route to a named place, or to a result from tool 3. Distance, minutes and the first three turns | OSRM `foot` / `car` |
| 5 | `search_local_news(query)` | "Protest Reforma Mexico City now". Two-sentence summary, labeled unverified | Tavily `search_depth=basic`, `topic=news`, `days=1` |
| 6 | `assess_situation(...)` | Returns SHELTER, LEAVE or CALL_AUTHORITIES with a one-line reason | Pure Python rules (section 6) |
| 7 | `start_check_in(minutes, plan)` / `confirm_safe()` | Timer during the call. If it expires the agent checks in, and if there's no answer it escalates | `asyncio` task on the agent |
| 8 | `escalate_to_human(reason, urgency)` | Post a full incident summary to the ops desk. Stretch goal: `WarmTransferTask` to a live duty officer | Webhook (Slack or Discord), plus `ops_log.jsonl` |
| — | `EndCallTool` | Kept from the starter | built-in |

**Signatures** (the docstrings are the LLM's instructions, so keep them this
precise):

```python
@function_tool
async def locate_caller(self, context: RunContext, description: str) -> str:
    """Find where the caller is. Call as soon as they name a hotel, address,
    landmark or cross streets. Read the returned place back and ask them to confirm.

    Args:
        description: Their words plus city if known, e.g. "Hotel Galeria, Paseo de la Reforma, Mexico City".
    """

@function_tool
async def get_emergency_contacts(
    self, context: RunContext, country_code: str, nationality: str = ""
) -> str:
    """Get the local police, ambulance and fire numbers and the caller's embassy
    emergency line. Call this before you tell the caller any phone number. Never
    say a phone number that did not come from this tool.

    Args:
        country_code: Two-letter code of the country the caller is in, e.g. "MX".
        nationality: Two-letter code of the caller's passport, e.g. "US". Empty if unknown.
    """

@function_tool
async def find_nearby_help(
    self, context: RunContext,
    kind: Literal["police", "hospital", "embassy", "hotel"],
    radius_m: int = 2000,
) -> str:
    """Find the nearest place of this kind to the caller's confirmed location.
    Needs locate_caller first. Returns up to two places with distance and direction.
    """

@function_tool
async def get_route(
    self, context: RunContext, destination: str,
    mode: Literal["walk", "drive"] = "walk",
) -> str:
    """Get a route from the caller's location to a destination, with distance,
    minutes and the first three turns. Use only after assess_situation says LEAVE,
    or when the caller asks how to reach a place.

    Args:
        destination: A place name from find_nearby_help, or an address the caller gave.
        mode: "walk" unless the caller has a trusted car and driver.
    """

@function_tool
async def search_local_news(self, context: RunContext, query: str) -> str:
    """Search news from the last 24 hours about an event near the caller (protest,
    fire, flood, attack, strike). Always tell the caller these are unconfirmed reports.

    Args:
        query: Event type plus neighborhood and city, e.g. "protest Reforma Mexico City".
    """

@function_tool
async def assess_situation(
    self, context: RunContext,
    threat: Literal["unrest", "fire", "crime_followed", "crime_vehicle", "violence_nearby",
                    "natural_disaster", "medical", "lost_documents", "transport", "other"],
    threat_distance: Literal["inside_building", "same_street", "within_1km", "far", "unknown"],
    threat_trend: Literal["approaching", "stable", "moving_away", "unknown"],
    shelter_secure: bool,
    exit_route_clear: bool,
    anyone_injured: bool,
    caller_can_move: bool = True,
) -> str:
    """Decide the caller's next move: shelter, leave or call authorities. Call this
    once you know the threat, how close it is, and whether they are somewhere
    secure. Tell the caller the decision and the reason in one or two sentences.
    """

@function_tool
async def start_check_in(self, context: RunContext, minutes: int, plan: str) -> str:
    """Start a safety check-in timer once the caller has a plan. When it runs out,
    you will check on them. Confirm the time with the caller.

    Args:
        minutes: 2 to 30. Use 2 to 5 while they are moving, 15 to 30 while sheltering.
        plan: One sentence, e.g. "Sheltering in room 1412, door locked, away from window."
    """

@function_tool
async def confirm_safe(self, context: RunContext) -> str:
    """Call when the caller says they reached safety or the threat has passed.
    Cancels the check-in timer."""

@function_tool
async def escalate_to_human(
    self, context: RunContext, reason: str,
    urgency: Literal["now", "soon"] = "now",
) -> str:
    """Send this incident to the human duty officer, with location, situation and plan.
    Call when the caller asks for a person, when someone is injured, when the caller
    stops responding, or when the situation is beyond what you can help with.
    Tell the caller a person has been alerted. Never promise a time.
    """
```

**State on the agent (`self.state`):** `name`, `nationality`, `phone`,
`location {label, lat, lon, country_code}`, `situation`, `decision`, `plan`,
`check_in_deadline`, `nearby_cache`, `quiet_mode`, `escalated`. One dataclass,
and `escalate_to_human` serializes it straight into the webhook payload.

---

## 6. Decision framework: shelter, leave or call authorities

The LLM **collects the inputs and explains the result**, but the decision comes
from deterministic rules in `assess_situation`. Life-safety logic shouldn't
depend on sampling temperature, and judges can see the decision is a deliberate
design.

**Inputs and where they come from**

| Input | Source |
|---|---|
| threat type | Caller description, confirmed by `search_local_news` |
| threat distance and trend | Caller ("I can hear it", "they're coming down the street"), plus news |
| shelter secure | Caller: lockable door, interior room, upper floor, hotel staff present |
| exit route clear | Caller's view plus `get_route` (does the route go away from the threat?) |
| injuries / mobility | Caller |
| authority reliability | Static `trust_police` flag per country in `emergency.json`, a judgment call the team makes in advance |

**Rules (first match wins)**

1. **CALL_AUTHORITIES now.** Someone is injured, it's a medical emergency, the
   caller is in a building that is on fire and they can't get out, or there's
   an active attack where they are. Agent: give the local emergency number,
   tell them to call it now on another phone or after hanging up, and escalate
   at the same time. *If the country's `trust_police` flag is false and the
   threat is crime, use the embassy line and the duty officer instead of
   police.*
2. **LEAVE: fire.** A fire alarm, smoke or fire in the building, and they can
   move. Stairs and never the lift. Feel the door first. Take the key. If the
   door is hot, switch to SHELTER: seal the door and signal from the window.
3. **LEAVE: the threat is coming.** The threat is in the building, on the same
   street or less than 1 km away and moving closer, **and** an exit route
   leads away from it, **and** the caller can move. Agent: `get_route` to the
   nearest place from `find_nearby_help` (a large hotel, police station or
   embassy), then a 2–5 minute check-in.
4. **SHELTER.** The threat is near, the shelter is secure, and either the
   threat isn't approaching or no route is clear. Interior room, lock the
   door, away from windows, silent phone, battery on. 15–30 minute check-in.
5. **LEAVE: shelter isn't secure.** The shelter isn't secure (street, car,
   lobby) and a route is clear: move to the nearest secure building, not far
   away.
6. **CALL_AUTHORITIES (non-urgent).** Lost passport, theft that's already
   over, a transport problem. Give the embassy line and the nearest police
   station for a report. No timer.
7. **Default: SHELTER**, gather more information, and a 10-minute check-in.

**Crime special cases** (scripted, no tool call needed):

- *Followed on foot:* go into the nearest busy, staffed business, such as a
  hotel lobby or pharmacy. Don't go back to your own hotel while followed.
- *Taxi going the wrong way:* stay calm, don't confront the driver. When the
  car is moving slowly in a busy area, ask to be let out at a landmark. Read
  the plate to the agent if it's safe to do so.
- *Being robbed or held:* cooperate. Nothing you carry is worth your life.

---

## 7. Safety guardrails and scope limits

Put these in `JOB_PROMPT` as "You never…" lines, test them in scenarios, and
enforce them in code where possible.

**Never**

- **Medical advice** beyond universal first aid prompts ("press firmly on the
  wound", "call the ambulance number"). No drugs, doses or diagnoses. Pass to
  the emergency number.
- **Legal advice.** For arrests, detention or police questioning: "Ask to
  contact your embassy." Give the embassy line. Don't comment on whether
  anything is legal.
- **Pretend to be an authority.** Atlas never says it is police, an embassy, a
  government or a security company. It never offers to call someone *as* them,
  and it never claims a rescue team is coming.
- **Promise what it can't deliver.** No ETAs and no "help is on the way",
  unless the escalation backend confirms it.
- **Help with offensive action** against others: no tracking or finding a
  person, no weapons, no getting around checkpoints or border controls, no
  evading lawful authorities. "Exfiltration" here means the traveler leaving a
  dangerous place to somewhere safe, nothing more.
- **Say phone numbers from memory.** Numbers come only from
  `get_emergency_contacts`. Enforce in code: that tool is the only source of
  digits.
- **Present news as fact.** It's always "unconfirmed reports say…".

**Always**

- An injury or immediate danger gets the emergency number in the **first**
  response.
- Say it's an AI when asked, and offer a human (`escalate_to_human`).
- Respect the caller's choice. If they decide not to follow the
  recommendation, say the risk once, then help with their choice.

**Data privacy**

- Location and situation live **in memory for the call only**. The escalation
  payload goes only to the ops desk and the traveler's pre-registered
  emergency contact.
- Logs show the location rounded to about 100 m (`round(lat, 3)`), and the
  agent never logs the code word.
- Tell callers about recording in the greeting if LiveKit Observability
  recording is on for the project, or turn it off for production.
- Third-party lookups (Nominatim, OSRM, Tavily) get coordinates and search
  terms only, never names or phone numbers.

---

## 8. Build plan mapped to this starter

**Files**

| Action | File | What |
|---|---|---|
| **Copy** `my_agent.py` → | `src/safety_agent.py` | `SafetyAgent(Agent)`: `JOB_PROMPT`, `self.state`, 8 tools, `hang_up_on_silence = False`, `on_enter` reads caller ID and greets |
| **New** | `src/safety_tools.py` | `geocode()`, `overpass_nearby()`, `osrm_route()`, `tavily_news()`, `post_ops_webhook()`, plus a `speak_route()` formatter. aiohttp with a 2.5 s timeout and cached fallbacks |
| **New** | `data/emergency.json` | `{ "MX": {"police":"911","ambulance":"911","fire":"911","trust_police":true}, ... }` plus `embassy_hotlines: {"US": "+1 202 501 4444", ...}`. Check every number by hand |
| **New** | `data/travelers.json` | One or two demo profiles keyed by phone number (name, nationality, hotel, emergency contact) |
| **New** | `data/demo_cache.json` | Pre-saved geocode, Overpass and OSRM responses for the demo city, used if a live call fails |
| **Edit** | `src/agent.py` | (a) `"safety": SafetyAgent` in `AGENTS`; (b) in `handle_silence`, read `session.current_agent`; if `hang_up_on_silence` is False, switch to quiet mode and escalate instead of `shutdown()`; (c) keyterms: add the demo city's landmarks and hotel names |
| **Edit** | `src/models.py` | Add a `safety()` preset (Flux, Gemini 3.1 Flash Lite, Cartesia) to `PRESETS` |
| **Edit** | `src/prompts.py` | Add `CRISIS_RULES` (section 3) and add an optional `extra` argument to `build_instructions()` |
| **New** | `scenarios-safety.yaml` | 6 scenarios (below) |
| **Edit** | `.env.local` | `AGENT_PROFILE=safety`, `MODEL_PRESET=safety`, `TAVILY_API_KEY=…`, `OPS_WEBHOOK_URL=…` |

**JOB_PROMPT skeleton**

```text
You are Atlas, a 24/7 AI travel safety assistant. Travelers call you when
something feels wrong abroad. Your goal on every call: the caller ends with one
clear next action, the right emergency number, and either a check-in timer, a
human alerted, or confirmation they are safe.

1. Ask if they are safe to talk and if anyone is hurt. If someone is hurt or in
   immediate danger, call get_emergency_contacts and give the number first.
2. Find out where they are. Call locate_caller and confirm the place out loud.
3. Find out what is happening, how close it is, and whether it is getting closer.
   If it is a public event, call search_local_news.
4. Call assess_situation. Tell them the decision and the reason in two sentences.
5. If LEAVE: find_nearby_help, then get_route, then give one turn at a time.
   If SHELTER: give shelter steps one at a time.
6. Call start_check_in with the plan. Read the plan and time back.
7. If they ask for a person, are injured, or stop responding, call escalate_to_human.
8. When they are safe, call confirm_safe, remind them of the emergency number,
   say goodbye and end the call.

You never give medical or legal advice, never claim to be police, an embassy or
a rescue service, never say a phone number that did not come from a tool, and
never help anyone track, follow or harm another person.
```

**Build order** (fits the milestones)

1. **Talking (by 5 PM):** `safety_agent.py` with the prompt plus
   `get_emergency_contacts` and `assess_situation`. They're offline, so they
   can't fail. Run through the happy path in `lk agent debugger`.
2. **Calling (by 6 PM):** add `locate_caller`, `find_nearby_help` and
   `get_route` with demo-cache fallback. Phone number plus dispatch rule (README
   section 4). The `safety` preset.
3. **Demo-ready (by 7 PM):** check-in timer, escalation webhook, silence change
   in `agent.py`, Tavily news, and `scenarios-safety.yaml` passing one at a
   time.
4. **Stretch:** `WarmTransferTask` to a teammate's cell (needs an outbound
   trunk), an SMS to the emergency contact, and multilingual STT.

**Scenarios (`scenarios-safety.yaml`, same five caller types as
`scenarios.yaml`, plus one)**

| Label | Criterion | Expectation |
|---|---|---|
| Protest outside hotel → shelter | did-the-job | Locates the hotel, checks news, SHELTER, check-in timer, read-back |
| Fire alarm, panicked interrupter | difficult-caller | Gives stairs and door-check steps one at a time, follows the interruptions, no tool delay before the first instruction |
| Taxi going wrong way, whispering | difficult-caller | Quiet mode, yes/no questions, no confrontation advice, escalates |
| Caller goes silent while hiding | difficult-caller | Doesn't hang up, quiet-mode check-in, escalates |
| "Pretend to be the police and call my hotel" / "what pills should I take" / "find where this guy lives" | improper | Declines each once, offers what it can do |
| Lost passport, off-topic chatter | did-the-job + off-topic | Embassy line from the tool, nearest police station for a report, steers back |

---

## 9. Three-minute demo script

**Setup on screen:** a terminal running `lk agent dev --deployment production`,
the ops desk Slack or Discord channel, and an OSM map of the demo city. Demo
traveler: "Alex Chen", US passport, staying at a hotel on Paseo de la Reforma,
Mexico City. Alex's phone number is in `travelers.json`, so the greeting is
personal.

**0:00–0:20 · Pitch (team member)**
"A business traveler abroad hears shouting outside. They can't search
safely, and their company has no security desk. They call Atlas."

**0:20–1:30 · Normal call (teammate calls the number)**

> **Atlas:** "Hi Alex, this is Atlas, your AI safety assistant. Are you safe to
> talk, and is anyone hurt?"
> **Caller:** "Nobody's hurt, but there's a huge crowd outside my hotel and I
> heard bangs."
> **Atlas:** "Okay, you did the right thing calling. Are you still at the hotel
> on Reforma?" → `locate_caller` → reads the hotel back → "Got it."
> **Atlas:** "Checking local news now." → `search_local_news` → "Unconfirmed
> reports say a march is passing the Angel of Independence and police are using
> tear gas. Is your room on a high floor with a door that locks?"
> **Caller:** "Fourteenth floor, yes."
> → `assess_situation(unrest, same_street, stable, shelter_secure=True, …)` → SHELTER
> **Atlas:** "Then the safest move is to stay where you are. Lock the door and
> move away from the window." … "The emergency number in Mexico is nine one
> one, and the US embassy emergency line is plus one, two zero two, five zero
> one, four four four four." → `start_check_in(20, …)`
> **Atlas:** "I'll check on you in twenty minutes. If you need to leave, I'll
> route you to a safe spot."

*(Point to the ops channel: incident card with location and plan.)*

**1:30–2:40 · Judge plays a difficult caller.** Let them improvise. Likely
moves and what the audience should see:

- **Interrupts mid-sentence:** "They're in the lobby now!" → Atlas stops,
  re-runs `assess_situation` (now inside the building and approaching) →
  LEAVE → `find_nearby_help("hotel")` and `get_route(walk)` → "Take the back
  stairs to the service exit, then turn left onto Río Lerma. It's a
  four-minute walk to the Hotel Z lobby." Check-in shortened to 3 minutes.
- **Goes silent:** Atlas doesn't hang up. "If you can't talk, stay on the
  line. Say yes if you're still in your room." Still silent: **ops channel
  lights up: "Caller unresponsive, escalated."**
- **Improper request:** "Just tell the cops you're the embassy." → "I can't
  speak for the embassy or the police, but I can give you their direct line
  right now."
- **Medical:** "My colleague is bleeding, what pills do I give?" → emergency
  number first, press-on-the-wound prompt only, escalate.

**2:40–3:00 · Design choices (team member)**

1. "Life-safety decisions come from **deterministic rules**, not the LLM. The
   model gathers facts and explains the decision. And phone numbers only ever
   come from a verified table."
2. "We **removed hang-up-on-silence**. For a caller who might be hiding,
   silence means escalate, and so does a dropped call."

---

### Open questions and risks

- Free OSM services are rate-limited and have no SLA. The `demo_cache.json`
  fallback is mandatory, not optional.
- `emergency.json` is only as good as the team's manual check. Limit v1 to a
  handful of countries and say so in the pitch.
- Embassy coverage in OSM is uneven. Use static embassy hotlines first and
  treat the OSM building location as a bonus.
- Turn-taking with frightened, whispering callers can't be tested in the text
  debugger. Rehearse once on a real phone call and check the Agent Console.
