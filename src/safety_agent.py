"""Atlas: emergency travel safety voice agent (TDD: docs/TDD-atlas-implementation.md).

Phase A: CallState, JOB_PROMPT, get_emergency_contacts, assess_situation,
set_quiet_mode, confirm_safe, EndCallTool and the tts_node voice guard.
Phase B (steps 10/14): locate_caller, confirm_location (+ Overpass prefetch),
find_nearby_help, get_route, search_local_news, two-phase start_check_in with
its timer, and the real escalate_to_human (ops webhook, retries, idempotency).

Privacy: never log transcripts, raw phone numbers, names or the code word.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import time
from collections.abc import AsyncIterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
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

import safety_tools as st
from prompts import CRISIS_RULES, QUIET_MODE_RULES, build_instructions
from safety_rules import decide
from voice_guard import (
    StyleGuard,
    digits_only,
    extract_digit_runs,
    guard_stream,
    speak_number,
)

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
    city: str = ""


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

1. Ask if they are safe to talk — that is your ONLY first question. After they
   answer, ask if anyone is hurt. If someone is hurt or in immediate danger
   (for example someone is trying to get into their room), call
   get_emergency_contacts and say the number in one sentence, then call
   escalate_to_human.
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
7. Say the plan and the minutes back as ONE yes-or-no question, for example
   "Check in every 20 minutes, say yes to confirm." Do NOT call start_check_in
   with confirmed=true until they explicitly say yes to that read-back. If they
   confirm something else, do the read-back again.
8. If they ask for a person, call escalate_to_human and stay on the line. Say a
   person has been alerted only if the tool result says so; otherwise say you
   are still trying to reach them. Never promise a time.
9. If they whisper, say they can't talk, or give one-word answers, call
   set_quiet_mode and ask only yes or no questions.
10. When they say they are safe, call confirm_safe, remind them of the emergency
    number, say goodbye and end the call.

Never repeat a sentence you have already said in this call. If you said it once,
move to the next step or ask the one question you still need answered.

You only do crisis triage and safety check-ins. For anything else, such as
bookings, directions for sightseeing, translation, trivia or general chat, say
"I can only help with your safety right now," then ask one safety question.

You never give medical advice beyond basic first aid prompts like applying
firmly on a wound. You never give legal advice; for arrests or detention, give
the embassy line. You never claim to be the police, an embassy, a government or
a rescue service, and you never say help is on the way. You never help anyone
track, follow, find or harm another person, or get around police, checkpoints
or border controls. You never say a phone number, address, distance, route or
news report that did not come from a tool in this call.

SPEAKING STYLE — NON-NEGOTIABLE. Every reply is ONE or TWO short sentences
you can say in one breath, and at most ONE question per reply. Never lists,
never numbered steps spoken in one breath: give one step per turn. Read
phone numbers digit by digit ("one one two"). Keep the location read-back
to one short sentence. A reply longer than two sentences is a mistake.
"""

NOT_ANSWERED = "The caller has not answered the read-back yet. Ask them first."
NO_LOCATION = "Location not confirmed yet. Confirm where they are first."
KIND_LABEL = {
    "police": "police",
    "hospital": "hospital",
    "embassy": "embassy",
    "hotel": "hotel",
}


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
        self._caller_number = ""  # memory only: ops callback if opted in
        self._alert_seq = 0
        self._escalated_at = ""
        self._prefetch_task: asyncio.Task[Any] | None = None
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
        extra = CRISIS_RULES + self._profile_extra
        if self.state.escalated:
            extra += (
                f"\n# Escalation\nA person has been alerted at {self._escalated_at} "
                "UTC. Don't alert again unless something new happens.\n"
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
        self._caller_number = number
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
                f"{greeting}. Say you are Atlas, an AI safety assistant. "
                "Then ask exactly one question: are you safe to talk right now? "
                "Two short sentences total, nothing more. Do not ask about injuries yet."
            )
        )

    def _on_transcript(self, ev: Any) -> None:
        if ev.is_final:
            self.state.user_turns += 1
            self._silent_since = None
            # caller-spoken digits may be read back (room number, plate)
            self.state.allowed_numbers |= extract_digit_runs(ev.transcript)
            # NEVER log ev.transcript

    async def llm_node(self, chat_ctx: Any, tools: Any, model_settings: ModelSettings):
        """Style-guard upstream of TTS and text forwarding (voice AND sim).

        The base llm_node yields ChatChunk | str | FlushSentinel. Text content is
        fed through StyleGuard; tool-call chunks and sentinels pass untouched.
        """
        from dataclasses import replace

        inspect = __import__("inspect")
        node = Agent.default.llm_node(self, chat_ctx, tools, model_settings)
        if inspect.iscoroutine(node):
            node = await node
        seen = getattr(self, "_spoken_norms", None)
        if seen is None:
            seen = []
            self._spoken_norms = seen
        guard = StyleGuard(
            self.state.allowed_numbers,
            logger,
            quiet_mode=self.state.quiet_mode,
            seen=seen,
        )
        async for item in node:
            if isinstance(item, str):
                piece = guard.feed(item)
                if piece:
                    yield piece
                continue
            text = getattr(item, "text", None)
            if isinstance(text, str) and text:
                piece = guard.feed(text)
                if piece:
                    try:
                        item = replace(item, text=piece)
                    except Exception:
                        item.text = piece
                has_calls = bool(
                    getattr(item, "delta", None) and getattr(item.delta, "tool_calls", None)
                )
                if piece or has_calls:
                    yield item
            else:
                yield item
        tail = guard.finalize()
        if tail:
            yield tail

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
            # final card first; flush_pending_alerts (shutdown callback) waits
            self._spawn(
                self._post(
                    "atlas.update",
                    f"line closed after {MAX_SILENT_MINUTES:.0f} min of silence",
                    "now",
                    "silence",
                )
            )
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

    # ---- ops desk -------------------------------------------------------------

    def _turns(self) -> int:
        """Caller turns so far. Voice: final transcripts. Text (debugger,
        simulations) emits no transcripts, so also count user messages."""
        n = self.state.user_turns
        with contextlib.suppress(Exception):
            n = max(
                n,
                sum(
                    1
                    for it in self.chat_ctx.items
                    if getattr(it, "type", "") == "message"
                    and getattr(it, "role", "") == "user"
                ),
            )
        return n

    def _payload(
        self,
        kind: str,
        reason: str,
        urgency: str,
        source: str,
        update: bool = False,
    ) -> dict[str, Any]:
        s = self.state
        self._alert_seq += 1
        caller: dict[str, Any] = {
            "name": s.name,
            "nationality": s.nationality,
            "phone_hash": s.phone_hash,
        }
        if os.getenv("OPS_INCLUDE_CALLBACK", "0") == "1" and self._caller_number:
            caller["callback"] = self._caller_number
        loc = asdict(s.location) if s.location else None
        deadline = (
            datetime.fromtimestamp(s.check_in_deadline, timezone.utc).isoformat()
            if s.check_in_deadline
            else ""
        )
        return {
            "type": kind,
            "incident_id": self._incident_id,
            "idempotency_key": f"{self._incident_id}-{self._alert_seq}",
            "update": update,
            "urgency": urgency,
            "source": source,
            "reason": reason,
            "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "caller": caller,
            "location": loc,
            "hotel_on_file": s.profile_hotel if not loc else "",
            "situation": s.situation,
            "decision": s.decision,
            "plan": s.plan,
            "check_in_deadline": deadline,
            "quiet_mode": s.quiet_mode,
        }

    async def _mark_escalated(self) -> None:
        if not self.state.escalated:
            self.state.escalated = True
            self._escalated_at = datetime.now(timezone.utc).strftime("%H:%M")
            await self.update_instructions(self._compose_instructions())

    async def _retry_alert(self, payload: dict[str, Any], tries: int = 6) -> None:
        """Background retries every 10 s. Same idempotency key every time."""
        for _ in range(tries):
            await asyncio.sleep(10)
            if await st.post_ops_webhook(payload, attempts=1):
                if payload.get("type") == "atlas.escalation":
                    await self._mark_escalated()
                return
        logger.warning("ops alert gave up incident_id=%s", self._incident_id)

    async def _post(
        self, kind: str, reason: str, urgency: str, source: str, update: bool = True
    ) -> bool:
        """Non-escalation card (plan set, caller safe, line closed)."""
        payload = self._payload(kind, reason, urgency, source, update=update)
        ok = await st.post_ops_webhook(payload)
        if not ok:
            st.append_ops_log(payload, "undelivered")
        return ok

    async def _escalate(self, reason: str, urgency: str, source: str) -> bool:
        """Alert the human desk. Returns True only if the POST returned 2xx.

        Same reason+source within 60 s, or any alert after the first delivered
        one, is sent as an update to the same incident_id, not a new incident.
        Failure: payload appended to ops_log.jsonl and retried in the
        background; the caller only hears "alerted" after a 2xx.
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
        payload = self._payload(
            "atlas.escalation",
            reason,
            urgency,
            source,
            update=repeat or self.state.escalated,
        )
        delivered = await st.post_ops_webhook(payload)
        if delivered:
            await self._mark_escalated()
        else:
            st.append_ops_log(payload, "undelivered")
            if st.ops_webhook_url():
                self._spawn(self._retry_alert(payload))
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
        text += (
            " Your next sentence must be just that number, spoken digit by digit,"
            " and nothing else."
        )
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
                "Tell them just to say yes or no out loud (no keypad needed)."
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
            logger.info("ops update: caller reports safe")
            self._spawn(
                self._post("atlas.update", "caller reports safe", "soon", "llm")
            )
        return "Timer cancelled. Remind them of the local emergency number, then say goodbye."

    # ---- location (H3: read back, then confirm) --------------------------------

    @function_tool
    async def locate_caller(self, context: RunContext, description: str) -> str:
        """Find where the caller is. Call as soon as they name a hotel, address,
        landmark or cross streets. Then read the returned place back and ask them to
        confirm. Do not call confirm_location until they say yes.

        Args:
            description: Their words plus city if known, e.g.
                "Hotel Galeria, Paseo de la Reforma, Mexico City".
        """
        s = self.state
        if s.phase == Phase.SAFETY_CHECK:
            s.phase = Phase.LOCATE
        try:
            r = await st.geocode(description)
        except Exception:
            logger.exception("locate_caller failed")
            r = None
        if not r:
            return (
                "I couldn't find that place. Ask for the street name or a nearby "
                "landmark."
            )
        s.pending_location = Location(
            r["label"], r["lat"], r["lon"], r["country_code"], r.get("city", "")
        )
        s.pending_location_turn = self._turns()
        s.allowed_numbers |= extract_digit_runs(r["label"])
        return f"Found: {r['label']}. Read this back and ask if it is right."

    @function_tool
    async def confirm_location(self, context: RunContext, caller_said_yes: bool) -> str:
        """Call right after the caller answers your read-back of their location.
        Set caller_said_yes to true only if they clearly said yes. If they said no,
        set it to false and ask them where they are again.
        """
        s = self.state
        if s.pending_location is None:
            return "No place to confirm yet. Call locate_caller first."
        if self._turns() <= s.pending_location_turn:
            return NOT_ANSWERED
        if not caller_said_yes:
            s.pending_location = None
            s.pending_location_turn = -1
            return "Ask where they are again."
        loc = s.pending_location
        s.location = loc
        s.pending_location = None
        s.nearby_cache.clear()
        if s.phase in (Phase.SAFETY_CHECK, Phase.LOCATE):
            s.phase = Phase.SITUATION
        _, digits = _numbers_text(loc.country_code, s.nationality)
        s.allowed_numbers |= digits
        logger.info(
            "location confirmed at=%s,%s cc=%s",
            round(loc.lat, 3),
            round(loc.lon, 3),
            loc.country_code,
        )
        self._prefetch_task = self._spawn(self._prefetch_nearby(loc))
        return "Location confirmed."

    async def _prefetch_nearby(self, loc: Location) -> None:
        """Background Overpass fetch, so find_nearby_help is instant later."""
        try:
            res = await st.overpass_nearby(
                loc.lat,
                loc.lon,
                2000,
                self.state.nationality,
                timeout=st.PREFETCH_TIMEOUT_S,
            )
        except Exception:
            logger.exception("prefetch failed")
            return
        if res and self.state.location is loc:
            for kind, places in res.items():
                self.state.nearby_cache.setdefault(kind, places)

    # ---- places and routes ------------------------------------------------------

    async def _nearby(self, kind: str, radius_m: int) -> list[dict[str, Any]] | None:
        s = self.state
        loc = s.location
        assert loc is not None
        if (
            kind not in s.nearby_cache
            and self._prefetch_task is not None
            and not self._prefetch_task.done()
        ):
            await asyncio.wait({self._prefetch_task}, timeout=st.TIMEOUT_S)
        if kind in s.nearby_cache:
            return s.nearby_cache[kind]
        prefetch_running = (
            self._prefetch_task is not None and not self._prefetch_task.done()
        )
        if prefetch_running:
            # the slow live query is still in flight; don't start a second one
            res = st.demo_nearby(loc.lat, loc.lon, s.nationality) or None
        else:
            res = await st.overpass_nearby(loc.lat, loc.lon, radius_m, s.nationality)
        if not res:
            return None
        for k, places in res.items():
            s.nearby_cache.setdefault(k, places)
        return s.nearby_cache.get(kind)

    @function_tool
    async def find_nearby_help(
        self,
        context: RunContext,
        kind: Literal["police", "hospital", "embassy", "hotel"],
        radius_m: int = 2000,
    ) -> str:
        """Find the nearest place of this kind to the caller's confirmed location.
        Needs confirm_location first. Returns up to two places with distance and
        compass direction. Say a few words like "Finding the nearest police station"
        before calling.
        """
        s = self.state
        if s.location is None:
            return NO_LOCATION
        try:
            places = await self._nearby(kind, radius_m)
        except Exception:
            logger.exception("find_nearby_help failed")
            places = None
        if places is None:
            if kind == "embassy":
                return (
                    "I don't have the embassy building, but I can give you their "
                    "emergency line."
                )
            return (
                "I can't reach the map service right now. Is there a large hotel "
                "or a busy shop you can see?"
            )
        if not places:
            if kind == "embassy":
                return (
                    "I don't have the embassy building, but I can give you their "
                    "emergency line."
                )
            return f"No {KIND_LABEL[kind]} found nearby. Ask what they can see."
        loc = s.location
        parts = []
        for p in places[:2]:
            d = st.haversine_m(loc.lat, loc.lon, p["lat"], p["lon"])
            parts.append(
                f"{p['name']}, {st.speak_distance(d)} "
                f"{st.compass(loc.lat, loc.lon, p['lat'], p['lon'])}"
            )
        text = f"Nearest {KIND_LABEL[kind]}: {parts[0]}."
        if len(parts) > 1:
            text += f" Then {parts[1]}."
        s.allowed_numbers |= extract_digit_runs(text)
        return text + " Read the nearest back and ask if they want to go there."

    def _match_place(self, destination: str) -> dict[str, Any] | None:
        q = st.norm(destination)
        if not q:
            return None
        best = None
        for places in self.state.nearby_cache.values():
            for p in places:
                n = st.norm(p["name"])
                if n and (n in q or q in n):
                    return p
                overlap = len(set(n.split()) & set(q.split()) - {"de", "la", "le"})
                if overlap >= 2 and best is None:
                    best = p
        if best:
            return best
        for kind in ("police", "hospital", "embassy", "hotel"):
            if kind in q and self.state.nearby_cache.get(kind):
                return self.state.nearby_cache[kind][0]
        if "station" in q and self.state.nearby_cache.get("police"):
            return self.state.nearby_cache["police"][0]
        return None

    @function_tool
    async def get_route(
        self,
        context: RunContext,
        destination: str,
        mode: Literal["walk", "drive"] = "walk",
    ) -> str:
        """Get a route from the caller's confirmed location to a destination, with
        distance, minutes and the first three turns. Use only after assess_situation
        says LEAVE, or when the caller asks how to reach a place for their safety.
        Give the caller one step per turn, not all at once.

        Args:
            destination: A place name from find_nearby_help, or an address the caller gave.
            mode: "walk" unless the caller has a trusted car and driver.
        """
        s = self.state
        loc = s.location
        if loc is None:
            return NO_LOCATION
        place = self._match_place(destination)
        if place is None:
            g = await st.geocode(
                f"{destination}, {loc.city}" if loc.city else destination
            )
            if g is None:
                return (
                    "I couldn't find that place. Ask for another destination, or "
                    "find the nearest police station or hotel first."
                )
            place = {"name": g["label"].split(",")[0], "lat": g["lat"], "lon": g["lon"]}
        try:
            route = await st.osrm_route(
                (loc.lat, loc.lon), (place["lat"], place["lon"]), mode, place["name"]
            )
        except Exception:
            logger.exception("get_route failed")
            route = None
        if route is None:
            d = st.haversine_m(loc.lat, loc.lon, place["lat"], place["lon"])
            direction = st.compass(loc.lat, loc.lon, place["lat"], place["lon"])
            return (
                f"I can't get the route right now. It is {st.speak_distance(d)} "
                f"{direction}. Head {direction} on the main street and call out "
                "what you see."
            )
        text = st.speak_route(route, mode, place["name"])
        s.allowed_numbers |= extract_digit_runs(text)
        return text + " Give one step per turn."

    @function_tool
    async def search_local_news(self, context: RunContext, query: str) -> str:
        """Search news from the last day about an event near the caller, such as a
        protest, fire, flood, attack or strike. Say "Checking local news" first.
        Always tell the caller these are unconfirmed reports.

        The search runs in the background: this tool returns an immediate status.
        Keep talking to the caller (next steps, reassurance) while it runs; a
        proactive update will arrive when the search completes.

        Args:
            query: Event type plus neighborhood and city, e.g. "protest Reforma Mexico City".
        """
        self._spawn(self._deep_news(query))
        return (
            "The search is running in the background. In one short sentence, tell "
            "the caller you're pulling the latest reports and will update them in a "
            "moment. Do not wait on the result — keep them moving on their plan."
        )

    async def _deep_news(self, query: str) -> None:
        """Background research lane: fetch, then proactively voice the findings."""
        try:
            text = await st.tavily_news(query)
        except Exception:
            logger.exception("deep news search failed")
            text = None
        findings = text or (
            "No new reports found in the last day. We'll go on what you can "
            "see and hear."
        )
        with contextlib.suppress(Exception):
            await self.session.generate_reply(
                instructions=(
                    "BACKGROUND NEWS SEARCH FINISHED. Findings (unconfirmed reports): "
                    f"{findings} In one to two short sentences, tell the caller what "
                    "you found, always noting these are unconfirmed reports. If there "
                    "were no reports, say that plainly and go on with the plan."
                )
            )

    # ---- check-in (H3: two-phase) ------------------------------------------------

    @function_tool
    async def start_check_in(
        self,
        context: RunContext,
        minutes: int,
        plan: str,
        caller_confirmed: bool = False,
    ) -> str:
        """Start a safety check-in timer once the caller has a plan. First call it
        with caller_confirmed false: you will get a read-back to say. After the caller
        says yes to the read-back, call it again with the same values and
        caller_confirmed true.

        Args:
            minutes: 2 to 30. Use 2 to 5 while they are moving, 15 to 30 while sheltering.
            plan: One sentence, e.g. "Sheltering in room 1412, door locked, away from window."
            caller_confirmed: True ONLY after the caller said yes to your exact
            plan-and-minutes read-back. If they agreed to anything less, run
            another read-back instead.
        """
        s = self.state
        minutes = max(2, min(30, int(minutes)))
        plan = plan.strip()
        pending = s.pending_check_in
        matches = (
            pending is not None
            and pending[0] == minutes
            and st.norm(pending[1]) == st.norm(plan)
        )
        if not caller_confirmed or not matches:
            s.pending_check_in = (minutes, plan)
            s.pending_check_in_turn = self._turns()
            return (
                f"Read back: '{plan}', and I'll check on you in "
                f"{st.speak_minutes(minutes)}. Ask if that's right."
            )
        if self._turns() <= s.pending_check_in_turn:
            return NOT_ANSWERED
        if self._check_in_task is not None:
            self._check_in_task.cancel()
        s.plan = plan
        s.check_in_deadline = time.time() + minutes * 60
        s.phase = Phase.CHECK_IN
        s.pending_check_in = None
        self._check_in_task = asyncio.create_task(self._check_in_loop(minutes))
        logger.info("check_in set minutes=%d", minutes)
        self._spawn(self._post("atlas.plan", "plan set", "soon", "check_in"))
        return f"Check-in set for {st.speak_minutes(minutes)}."

    async def _check_in_loop(self, minutes: int) -> None:
        await asyncio.sleep(minutes * 60)
        if self.state.safe_confirmed:
            return
        self.session.generate_reply(
            instructions=(
                "The check-in time is up. In one short sentence, ask if they are "
                f"safe and still following the plan: {self.state.plan}"
                + (" Ask it as a yes or no question." if self.state.quiet_mode else "")
            )
        )
        # If they don't answer, user_away fires and handle_silence escalates.
