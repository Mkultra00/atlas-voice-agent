"""Atlas: emergency travel safety voice agent (TDD: docs/TDD-atlas-implementation.md).

Build phase A ("Talking" milestone): CallState, JOB_PROMPT, and the offline
tools get_emergency_contacts, assess_situation, set_quiet_mode,
escalate_to_human (log-only stub), confirm_safe, plus EndCallTool and the
tts_node voice guard. Network tools (locate_caller, confirm_location,
find_nearby_help, get_route, search_local_news) and start_check_in arrive in
build steps 9-14.

Privacy: never log transcripts, raw phone numbers, names or the code word.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections.abc import AsyncIterable
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from livekit import rtc
from livekit.agents import (
    Agent,
    CloseEvent,
    CloseReason,
    ModelSettings,
    RunContext,
    function_tool,
    get_job_context,
)
from livekit.agents.beta.tools import EndCallTool

from prompts import CRISIS_RULES, QUIET_MODE_RULES, build_instructions
from safety_rules import decide
from voice_guard import digits_only, extract_digit_runs, guard_stream, speak_number

logger = logging.getLogger("safety-agent")

DATA = Path(__file__).resolve().parent.parent / "data"
EMERGENCY: dict[str, Any] = json.loads((DATA / "emergency.json").read_text())
TRAVELERS: dict[str, Any] = json.loads((DATA / "travelers.json").read_text())

MAX_SILENT_MINUTES = float(os.getenv("ATLAS_MAX_SILENT_MINUTES", "30"))
ESCALATION_DEDUP_SECONDS = 60.0


class Phase(str, Enum):
    SAFETY_CHECK = "safety_check"
    LOCATE = "locate"
    SITUATION = "situation"
    DECIDED = "decided"
    CHECK_IN = "check_in"
    RESOLVED = "resolved"


@dataclass
class Location:
    label: str
    lat: float
    lon: float
    country_code: str  # ISO 3166-1 alpha-2, upper case


@dataclass
class CallState:
    phase: Phase = Phase.SAFETY_CHECK
    name: str = ""  # first name, from travelers.json or caller
    nationality: str = ""  # ISO alpha-2 passport country
    phone_hash: str = ""  # sha256(sip.phoneNumber)[:12], never the raw number
    profile_key: str = ""  # key into travelers.json, memory only
    profile_country: str = ""  # country the profile says they're in
    profile_hotel: str = ""
    pending_location: Location | None = None
    pending_location_turn: int = -1
    location: Location | None = None  # set only by confirm_location
    situation: str = ""
    decision: str = ""  # SHELTER | LEAVE | CALL_AUTHORITIES
    decision_reason: str = ""
    plan: str = ""
    pending_check_in: tuple[int, str] | None = None
    pending_check_in_turn: int = -1
    check_in_deadline: float | None = None
    nearby_cache: dict[str, list[dict]] = field(default_factory=dict)
    quiet_mode: bool = False
    urgent: bool = False
    escalated: bool = False
    safe_confirmed: bool = False
    allowed_numbers: set[str] = field(default_factory=set)  # digits-only
    user_turns: int = 0

    def plan_active(self) -> bool:
        return self.phase in (Phase.DECIDED, Phase.CHECK_IN) and not self.safe_confirmed

    def ops_payload(self) -> dict:
        d = asdict(self)
        for k in (
            "nearby_cache",
            "allowed_numbers",
            "pending_location",
            "pending_location_turn",
            "pending_check_in",
            "pending_check_in_turn",
            "profile_key",
        ):
            d.pop(k, None)
        return d


JOB_PROMPT = """
You are Atlas, an AI travel safety assistant. Travelers call you when something
feels wrong abroad. You do one job: help the caller decide whether to shelter,
leave, or call the authorities, and keep checking on them until they are safe.

Your goal on every call: the caller ends with one clear next action, the right
emergency number from get_emergency_contacts, and either a check-in timer, a
human alerted, or confirmation they are safe.

1. Ask if they are safe to talk and if anyone is hurt. If someone is hurt or in
   immediate danger, call get_emergency_contacts and say the number in your
   first reply, then call escalate_to_human.
2. If it is a fire, give the fire steps first, one per turn: stairs not lift,
   feel the door before opening it, take your key. Then continue.
3. Find out where they are. Call locate_caller, read the place back, and after
   they say yes, call confirm_location.
4. Find out what is happening, how close it is, and whether it is getting closer.
   If it is a public event, say "Checking local news" and call search_local_news.
5. Call assess_situation. Say the decision and the reason in two sentences at most.
6. If LEAVE: call find_nearby_help, then get_route, then give one step per turn.
   If SHELTER: give shelter steps one per turn. If CALL_AUTHORITIES: give the
   number from get_emergency_contacts.
7. Read the plan and the minutes back. After they say yes, call start_check_in.
8. If they ask for a person, call escalate_to_human, tell them a person has been
   alerted, and stay on the line. Never promise a time.
9. If they whisper, say they can't talk, or give one-word answers, call
   set_quiet_mode and ask only yes or no questions.
10. When they say they are safe, call confirm_safe, remind them of the emergency
    number, say goodbye and end the call.

You only do crisis triage and safety check-ins. For anything else, such as
bookings, directions for sightseeing, translation, trivia or general chat, say
"I can only help with your safety right now," then ask one safety question.

You never give medical advice beyond basic first aid prompts like pressing
firmly on a wound. You never give legal advice; for arrests or detention, give
the embassy line. You never claim to be the police, an embassy, a government or
a rescue service, and you never say help is on the way. You never help anyone
track, follow, find or harm another person, or get around police, checkpoints
or border controls. You never say a phone number, address, distance, route or
news report that did not come from a tool in this call.
"""

# TODO(build steps 10/14): remove once locate_caller, confirm_location,
# search_local_news, find_nearby_help, get_route and start_check_in exist.
PHASE_A_NOTE = """
# Tools not available yet on this line

locate_caller, confirm_location, search_local_news, find_nearby_help, get_route
and start_check_in are not available. Instead: ask where they are, read the place
back in their own words and wait for yes. Skip the news check. For a check-in,
read the plan and minutes back and tell them you will stay on the line.
"""


def _hash(value: str, n: int) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:n]


def _country(cc: str) -> dict[str, Any] | None:
    return EMERGENCY["countries"].get(cc.upper()) if cc else None


def _embassy(nationality: str, cc: str) -> dict[str, Any] | None:
    by_host = EMERGENCY.get("embassy_hotlines", {}).get(nationality.upper(), {})
    return by_host.get(cc.upper()) if cc else None


def _numbers_text(cc: str, nationality: str) -> tuple[str, set[str]]:
    """Spoken emergency numbers for a country (+ embassy line) and their digits."""
    c = _country(cc)
    if c is None:
        return "", set()
    raw = [c.get("emergency", ""), c["police"], c["ambulance"], c["fire"]]
    parts = []
    if c.get("emergency") and c["emergency"] not in (c["police"],):
        parts.append(f"emergency {speak_number(c['emergency'])}")
    parts += [
        f"police {speak_number(c['police'])}",
        f"ambulance {speak_number(c['ambulance'])}",
        f"fire {speak_number(c['fire'])}",
    ]
    text = f"{c['name'][0].upper()}{c['name'][1:]}: " + ", ".join(parts) + "."
    emb = _embassy(nationality, cc)
    if emb:
        raw.append(emb["number"])
        text += f" {emb['label']} emergency line: {speak_number(emb['number'])}."
    return text, {digits_only(n) for n in raw if n}


class SafetyAgent(Agent):
    hang_up_on_silence = False  # read by agent.py handle_silence

    def __init__(self) -> None:
        self.state = CallState()
        self._check_in_task: asyncio.Task[None] | None = None
        self._bg: set[asyncio.Task[Any]] = set()
        self._profile_extra = ""
        self._incident_id = ""
        self._last_escalation: dict[tuple[str, str], float] = {}
        self._silent_since: float | None = None
        self._drop_alerted = False
        super().__init__(
            instructions=self._compose_instructions(),
            tools=[
                EndCallTool(
                    ignore_on_enter=True,
                    extra_description="Only after confirm_safe, or after the caller "
                    "has been told the emergency number and chooses to hang up.",
                )
            ],
        )

    # ---- instructions / profile -------------------------------------------

    def _compose_instructions(self) -> str:
        extra = CRISIS_RULES + PHASE_A_NOTE + self._profile_extra
        if self.state.escalated:
            extra += (
                "\n# Escalation\nA person has been alerted. Don't alert again "
                "unless something new happens.\n"
            )
        if self.state.quiet_mode:
            extra += QUIET_MODE_RULES
        return build_instructions(JOB_PROMPT, extra)

    def _find_caller_number(self) -> str:
        """Caller E.164 from the SIP participant; ATLAS_DEMO_CALLER for debugger."""
        participant: rtc.RemoteParticipant | None = None
        try:
            participant = self.session.room_io.linked_participant
        except Exception:
            participant = None
        if participant is None:
            try:
                room = get_job_context().room
                participant = next(
                    (
                        p
                        for p in room.remote_participants.values()
                        if p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP
                    ),
                    None,
                )
            except Exception:
                participant = None
        if participant is not None:
            num = participant.attributes.get("sip.phoneNumber", "")
            if num:
                return num
        return os.getenv("ATLAS_DEMO_CALLER", "")

    def _load_caller_profile(self) -> None:
        try:
            self._incident_id = _hash(get_job_context().room.name, 10)
        except Exception:
            self._incident_id = _hash(str(time.time()), 10)

        number = self._find_caller_number()
        if number:
            self.state.phone_hash = _hash(number, 12)
        profile = TRAVELERS.get(number) if number else None
        logger.info(
            "profile_found=%s phone_hash=%s", profile is not None, self.state.phone_hash
        )
        if not profile:
            return
        self.state.profile_key = number
        self.state.name = profile.get("first_name", "")
        self.state.nationality = profile.get("nationality", "").upper()
        self.state.profile_country = profile.get("country_code", "").upper()
        self.state.profile_hotel = profile.get("hotel", "")
        # Code word and emergency contact are never put into prompts.
        line = (
            f"\n# Caller\nKnown caller: first name {self.state.name}, passport "
            f"{self.state.nationality}, staying at {self.state.profile_hotel}, "
            f"{profile.get('city', '')} ({self.state.profile_country})."
        )
        text, digits = _numbers_text(self.state.profile_country, self.state.nationality)
        if text:
            line += f" Emergency numbers from the verified table: {text}"
            self.state.allowed_numbers |= digits
        self._profile_extra = line + "\n"

    async def on_enter(self) -> None:
        self._load_caller_profile()
        await self.update_instructions(self._compose_instructions())
        self.session.on("user_input_transcribed", self._on_transcript)
        self.session.on("close", self._on_close)
        tts = getattr(self.session, "tts", None)
        if tts is not None and hasattr(tts, "prewarm"):
            try:
                tts.prewarm()
            except Exception:
                logger.debug("tts prewarm failed", exc_info=True)
        greeting = (
            f"Greet {self.state.name} by first name"
            if self.state.name
            else "Greet the caller"
        )
        await self.session.generate_reply(
            instructions=(
                f"{greeting}. Say you are Atlas, an AI safety assistant. Then ask: "
                "are you safe to talk right now, and is anyone hurt? Two sentences."
            )
        )

    def _on_transcript(self, ev: Any) -> None:
        if ev.is_final:
            self.state.user_turns += 1
            self._silent_since = None
            # caller-spoken digits may be read back (room number, plate)
            self.state.allowed_numbers |= extract_digit_runs(ev.transcript)
            # NEVER log ev.transcript

    def tts_node(self, text: AsyncIterable[str], model_settings: ModelSettings):
        guarded = guard_stream(
            text, self.state.allowed_numbers, logger, quiet_mode=self.state.quiet_mode
        )
        return Agent.default.tts_node(self, guarded, model_settings)

    # ---- code paths used by agent.py --------------------------------------

    async def enter_quiet_mode(self) -> None:
        if not self.state.quiet_mode:
            self.state.quiet_mode = True
            await self.update_instructions(self._compose_instructions())
            logger.info("quiet_mode on")

    async def escalate_from_code(
        self, reason: str, source: str, urgency: Literal["now", "soon"] = "now"
    ) -> bool:
        return await self._escalate(reason, urgency, source)

    def note_silence(self, away_count: int) -> None:
        """T16: after escalation, close the line after MAX_SILENT_MINUTES silent."""
        now = time.time()
        if away_count == 1 or self._silent_since is None:
            self._silent_since = now
            return
        if self.state.escalated and now - self._silent_since > MAX_SILENT_MINUTES * 60:
            logger.info("line closed after %.0f min of silence", MAX_SILENT_MINUTES)
            # TODO(step 14): final webhook update before shutdown.
            self.session.shutdown()

    async def on_dtmf(self, digit: str) -> None:
        """T21/T22: keypad in quiet mode. 1 = yes, 2 = no, 0 = get me a human."""
        await self.enter_quiet_mode()
        if digit == "1":
            self.session.generate_reply(user_input="yes")
        elif digit == "2":
            self.session.generate_reply(user_input="no")
        elif digit == "0":
            delivered = await self._escalate(
                "caller pressed 0 for a person", "now", "dtmf"
            )
            self.session.say(
                "I've alerted a person on our duty desk. I'm still here."
                if delivered
                else "I'm trying to reach a person on our duty desk. I'm still here."
            )

    def _on_close(self, ev: CloseEvent) -> None:
        self.handle_disconnect(ev.reason)

    def handle_disconnect(self, reason: CloseReason | str) -> None:
        """T20: dropped call during an active plan (or urgent) alerts at once."""
        s = self.state
        if self._drop_alerted or s.safe_confirmed:
            return
        if not (s.plan_active() or s.urgent or s.escalated):
            return
        if reason in (
            CloseReason.PARTICIPANT_DISCONNECTED,
            CloseReason.ERROR,
            "participant_disconnected",
        ):
            self._drop_alerted = True  # close + room event both fire: alert once
            self._spawn(
                self._escalate("call dropped during active plan", "now", "dropped_call")
            )

    async def flush_pending_alerts(self) -> None:
        """Shutdown callback: let pending alert tasks finish, 3 s cap."""
        if self._bg:
            await asyncio.wait(set(self._bg), timeout=3.0)

    def _spawn(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)
        return task

    async def _escalate(self, reason: str, urgency: str, source: str) -> bool:
        """Alert the human desk. Returns True only if delivery succeeded.

        TODO(step 14): POST st.post_ops_webhook(payload) with retries; append to
        ops_log.jsonl on failure. Log-only stub for now, so it never reports
        delivered and the agent never claims a person was alerted.
        """
        key = (reason, source)
        now = time.time()
        repeat = now - self._last_escalation.get(key, 0.0) < ESCALATION_DEDUP_SECONDS
        self._last_escalation[key] = now
        if urgency == "now":
            self.state.urgent = True
        logger.warning(
            "escalation incident_id=%s urgency=%s source=%s update=%s decision=%s",
            self._incident_id,
            urgency,
            source,
            repeat,
            self.state.decision or "-",
        )
        delivered = False
        if delivered and not self.state.escalated:
            self.state.escalated = True
            await self.update_instructions(self._compose_instructions())
        return delivered

    # ---- tools --------------------------------------------------------------

    @function_tool
    async def get_emergency_contacts(
        self, context: RunContext, country_code: str = "", nationality: str = ""
    ) -> str:
        """Get the local police, ambulance and fire numbers, and the caller's embassy
        emergency line. Call this every time before you say any phone number. Never
        say a phone number that did not come from this tool. Read numbers digit by digit.

        Args:
            country_code: Two-letter code of the country the caller is in, e.g. "MX".
                Empty to use their confirmed location.
            nationality: Two-letter code of the caller's passport, e.g. "US". Empty if unknown.
        """
        s = self.state
        cc = (
            country_code
            or (s.location.country_code if s.location else "")
            or s.profile_country
        ).upper()
        nat = (nationality or s.nationality).upper()
        default = EMERGENCY["_default"]
        s.allowed_numbers |= {digits_only(n) for n in default.get("numbers", [])}
        if not cc:
            logger.info("get_emergency_contacts outcome=unknown_country")
            return f"{default['text']} Ask which country they are in."
        text, digits = _numbers_text(cc, nat)
        if not text:
            logger.info("get_emergency_contacts outcome=no_table cc=%s", cc)
            self._spawn(self._escalate(f"no verified numbers for {cc}", "soon", "rule"))
            return (
                f"{default['text']} I don't have verified numbers for that country. "
                "Let me alert a person."
            )
        s.allowed_numbers |= digits
        logger.info("get_emergency_contacts outcome=table cc=%s", cc)
        if nat and not _embassy(nat, cc):
            text += " No verified embassy line for their passport in this country."
        return text

    @function_tool
    async def assess_situation(
        self,
        context: RunContext,
        threat: Literal[
            "unrest",
            "fire",
            "crime_followed",
            "crime_vehicle",
            "violence_nearby",
            "natural_disaster",
            "medical",
            "lost_documents",
            "transport",
            "other",
        ],
        threat_distance: Literal[
            "inside_building", "same_street", "within_1km", "far", "unknown"
        ],
        threat_trend: Literal["approaching", "stable", "moving_away", "unknown"],
        shelter_secure: bool,
        exit_route_clear: bool,
        anyone_injured: bool,
        caller_can_move: bool = True,
        door_hot: bool = False,
    ) -> str:
        """Decide the caller's next move: shelter, leave, or call authorities. Call
        this once you know the threat, how close it is, and whether they are
        somewhere secure. Call it again whenever the situation changes, for example
        the threat gets closer. Tell the caller the decision and the reason in one or
        two sentences.
        """
        s = self.state
        cc = (s.location.country_code if s.location else "") or s.profile_country
        country = _country(cc)
        trust_police = bool(country.get("trust_police", True)) if country else True
        d = decide(
            threat,
            threat_distance,
            threat_trend,
            shelter_secure,
            exit_route_clear,
            anyone_injured,
            caller_can_move,
            door_hot=door_hot,
            trust_police=trust_police,
        )
        s.decision = d.action
        s.decision_reason = d.reason
        s.situation = f"{threat}, {threat_distance}, {threat_trend}"
        s.phase = Phase.DECIDED
        logger.info("decision=%s rule=%s", d.action, d.rule)
        if d.escalate:
            s.urgent = True
            self._spawn(self._escalate(f"rule {d.rule}: {threat}", "now", "rule"))
        return d.spoken()

    @function_tool
    async def set_quiet_mode(self, context: RunContext, on: bool) -> str:
        """Turn quiet mode on when the caller whispers, says they can't talk, or
        only gives one-word answers. In quiet mode you only ask yes or no questions.
        Turn it off only if the caller says they can talk normally again."""
        self.state.quiet_mode = on
        await self.update_instructions(self._compose_instructions())
        logger.info("quiet_mode %s", "on" if on else "off")
        if on:
            return (
                "Quiet mode on. Ask only yes or no questions, ten words at most. "
                "Tell them they can press one for yes and two for no."
            )
        return "Quiet mode off."

    @function_tool
    async def escalate_to_human(
        self,
        context: RunContext,
        reason: str,
        urgency: Literal["now", "soon"] = "now",
    ) -> str:
        """Alert the human duty officer with the caller's location, situation and
        plan. Call when the caller asks for a person, when someone is injured, when
        the caller stops responding, or when the situation is beyond what you can help
        with. Tell the caller a person has been alerted and that you are staying on
        the line. Never promise a time, and never say help is on the way.

        Args:
            reason: A few words, e.g. "caller asked for a person".
            urgency: "now" for danger or injury, "soon" otherwise.
        """
        delivered = await self._escalate(reason, urgency, "llm")
        if delivered:
            return (
                "A person on the duty desk has been alerted. Stay on the line with me."
            )
        return "I'm still trying to reach the duty desk. Stay on the line with me."

    @function_tool
    async def confirm_safe(self, context: RunContext) -> str:
        """Call when the caller says they reached safety or the threat has passed.
        Cancels the check-in timer. Then remind them of the emergency number, say
        goodbye and end the call."""
        s = self.state
        s.safe_confirmed = True
        s.phase = Phase.RESOLVED
        if self._check_in_task is not None:
            self._check_in_task.cancel()
            self._check_in_task = None
        s.check_in_deadline = None
        logger.info("confirm_safe escalated=%s", s.escalated)
        if s.escalated:
            # TODO(step 14): post "caller reports safe" update to the webhook.
            logger.info("ops update: caller reports safe")
        return "Timer cancelled. Remind them of the local emergency number, then say goodbye."
