"""Entry point: wires the voice pipeline and picks which agent answers.

You mostly edit my_agent.py. Come here to change models, silence handling or
turn-taking.

  .env.local
    AGENT_PROFILE = booking | screener | mine | safety   (which agent answers)
    MODEL_PRESET  = budget  | quality             (see models.py)
    AGENT_NAME    = take2-agent                   (must match your dispatch rule)
"""

import asyncio
import hashlib
import logging
import os

from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    STTContextOptions,
    TurnHandlingOptions,
    UserStateChangedEvent,
    cli,
    inference,
    room_io,
)
from livekit.plugins import ai_coustics

from booking_agent import BookingAgent
from models import load_models
from my_agent import MyAgent
from safety_agent import SafetyAgent
from screener_agent import ScreenerAgent

logger = logging.getLogger("agent")

load_dotenv(".env.local")

AGENTS: dict[str, type[Agent]] = {
    "booking": BookingAgent,
    "screener": ScreenerAgent,
    "mine": MyAgent,
    "safety": SafetyAgent,
}

AGENT_NAME = os.getenv("AGENT_NAME", "take2-agent")

# Silence: after this many quiet seconds the caller counts as "away".
SILENCE_SECONDS = 8.0
# Check in this many times, then say goodbye and hang up.
# (Agents with hang_up_on_silence = False, like SafetyAgent, never hang up.)
MAX_CHECK_INS = 2

# Privacy: 1 = full LiveKit recording (audio, transcript, traces). Debug only.
RECORD_FULL = os.getenv("ATLAS_RECORD", "0") == "1"
PRIVATE_RECORDING = {
    "audio": False,
    "transcript": False,
    "traces": False,
    "logs": True,
    "redaction": True,
}

# Words STT would otherwise misspell. Demo city terms for the Paris scenario.
BASE_KEYTERMS = ["Take2", "LiveKit"]
SAFETY_KEYTERMS = [
    "Atlas",
    "Gare du Nord",
    "Gare de l'Est",
    "arrondissement",
    "Rue de Dunkerque",
    "Boulevard de Magenta",
    "pompiers",
    "SAMU",
]


def pick_agent() -> Agent:
    profile = os.getenv("AGENT_PROFILE", "booking").strip().lower()
    if profile not in AGENTS:
        raise ValueError(
            f"Unknown AGENT_PROFILE '{profile}'. Choose one of: {', '.join(AGENTS)}"
        )
    return AGENTS[profile]()


def _call_id(room_name: str) -> str:
    """Room names may contain the caller's number, so logs only see a hash."""
    return hashlib.sha256(room_name.encode()).hexdigest()[:10]


def handle_silence(session: AgentSession) -> None:
    """Ask "are you still there?" when the caller goes quiet, then hang up.

    Agents that set `hang_up_on_silence = False` (SafetyAgent) never hang up:
    1st away -> quiet mode + fixed line, 2nd -> alert a human, then "I'm still
    here" every 4th away event.
    """
    check_ins = 0
    tasks: set[asyncio.Task[None]] = set()

    async def safety_on_away(agent: Agent, away_count: int) -> None:
        if away_count == 1:
            await agent.enter_quiet_mode()
            session.say(
                "If you can't talk, stay on the line. Just say yes, if you're okay."
            )
        elif away_count == 2:
            delivered = await agent.escalate_from_code(
                reason="caller unresponsive after quiet-mode check-in",
                source="silence",
            )
            session.say(
                "I've alerted a person on our duty desk. I'm still here."
                if delivered
                else "I'm trying to reach a person on our duty desk. I'm still here."
            )
        elif away_count % 4 == 0:
            session.say("I'm still here.")
        agent.note_silence(away_count)

    async def check_in() -> None:
        nonlocal check_ins
        check_ins += 1
        agent = session.current_agent
        if not getattr(agent, "hang_up_on_silence", True):
            await safety_on_away(agent, check_ins)
            return
        if check_ins <= MAX_CHECK_INS:
            await session.generate_reply(
                instructions="The caller has gone quiet. In a few words, ask if they are still there."
            )
        else:
            await session.generate_reply(
                instructions="The caller is not responding. Say a short, polite goodbye."
            )
            session.shutdown()

    @session.on("user_state_changed")
    def _on_user_state(ev: UserStateChangedEvent) -> None:
        nonlocal check_ins
        if ev.new_state == "speaking":
            check_ins = 0
        elif ev.new_state == "away":
            task = asyncio.create_task(check_in())
            tasks.add(task)
            task.add_done_callback(tasks.discard)


server = AgentServer()


@server.rtc_session(agent_name=AGENT_NAME)
async def entrypoint(ctx: JobContext) -> None:
    agent = pick_agent()
    is_safety = isinstance(agent, SafetyAgent)
    if is_safety:
        ctx.log_context_fields = {"call_id": _call_id(ctx.room.name)}
    else:
        ctx.log_context_fields = {"room": ctx.room.name}
    models = load_models()

    session = AgentSession(
        # Ears: speech-to-text
        stt=models.stt,
        # Bias transcription toward words it would otherwise misspell.
        stt_context_options=STTContextOptions(
            keyterms=BASE_KEYTERMS + (SAFETY_KEYTERMS if is_safety else []),
            keyterm_detection={"enabled": True},
        ),
        # Brain: the LLM
        llm=models.llm,
        # Voice: text-to-speech
        tts=models.tts,
        expressive=models.expressive,
        turn_handling=TurnHandlingOptions(
            # Decides when the caller has finished speaking, using both the words
            # and the audio (pitch, pauses). Much better than a fixed silence timer.
            turn_detection=inference.TurnDetector(),
            # Barge-in stays live, but a noise blip must not kill playout:
            # require ≥0.9s of speech or ≥2 recognized words to interrupt.
            interruption={"min_duration": 0.9, "min_words": 2},
            # Start drafting a reply before the turn is confirmed. Cuts latency.
            preemptive_generation={"enabled": True},
        ),
        user_away_timeout=SILENCE_SECONDS,
    )

    handle_silence(session)
    bg_tasks: set[asyncio.Task[None]] = set()

    async def log_usage() -> None:
        # Shows model usage per call so you can see what you're spending.
        logger.info("session usage: %s", session.usage)

    ctx.add_shutdown_callback(log_usage)
    if is_safety:
        ctx.add_shutdown_callback(agent.flush_pending_alerts)

    # SafetyAgent: no audio/transcript/traces upload unless ATLAS_RECORD=1.
    # Other agents keep the starter default.
    record_kwargs = (
        {"record": PRIVATE_RECORDING} if is_safety and not RECORD_FULL else {}
    )
    await session.start(
        agent=agent,
        room=ctx.room,
        **record_kwargs,
        room_options=room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(
                # Removes background noise and other voices before STT hears it.
                noise_cancellation=ai_coustics.audio_enhancement(
                    model=ai_coustics.EnhancerModel.QUAIL_VF_S
                ),
            ),
            # Hang up the phone line when the session ends.
            delete_room_on_close=True,
        ),
    )

    @ctx.room.on("sip_dtmf_received")
    def _on_dtmf(ev: rtc.SipDTMF) -> None:
        # Quiet-mode keypad: 1 = yes, 2 = no, 0 = get me a human.
        # TODO(U9): confirm inbound DTMF reaches the room on the free number.
        current = session.current_agent
        if hasattr(current, "on_dtmf"):
            task = asyncio.create_task(current.on_dtmf(ev.digit))
            bg_tasks.add(task)
            task.add_done_callback(bg_tasks.discard)

    @ctx.room.on("data_received")
    def _on_typed_text(dp: rtc.DataPacket) -> None:
        # Typed text from the demo phone acts exactly like a spoken turn.
        if dp.topic != "atlas-text":
            return
        text = dp.data.decode("utf-8", "ignore").strip()
        if not text or len(text) > 500:
            return
        task = asyncio.create_task(
            session.generate_reply(user_input=text, allow_interruptions=True)
        )
        bg_tasks.add(task)
        task.add_done_callback(bg_tasks.discard)

    @ctx.room.on("participant_disconnected")
    def _on_participant_disconnected(p: rtc.RemoteParticipant) -> None:
        # Backup for network drops where session "close" may not fire (U8).
        if p.kind != rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            return
        current = session.current_agent
        if hasattr(current, "handle_disconnect"):
            current.handle_disconnect("participant_disconnected")

    await ctx.connect()


if __name__ == "__main__":
    cli.run_app(server)
