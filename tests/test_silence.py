"""Offline tests for silence handling, dropped calls and SafetyAgent tools.

Fake session, no network, no inference credits. AC5.1 and AC5.5 from the TDD.
AC3.1 (confirm_location) and AC3.2 (start_check_in) live in test_safety_phase_b.py.
"""

import asyncio
from types import SimpleNamespace

import pytest
from livekit.agents import CloseEvent, CloseReason

from agent import MAX_CHECK_INS, handle_silence
from booking_agent import BookingAgent
from safety_agent import Phase, SafetyAgent


class FakeSession:
    def __init__(self, agent) -> None:
        self.current_agent = agent
        self.handlers: dict[str, list] = {}
        self.said: list[str] = []
        self.replies: list[dict] = []
        self.shutdowns = 0

    def on(self, event, cb=None):
        def reg(fn):
            self.handlers.setdefault(event, []).append(fn)
            return fn

        return reg(cb) if cb is not None else reg

    def emit(self, event, ev) -> None:
        for fn in self.handlers.get(event, []):
            fn(ev)

    def say(self, text, **kw):
        self.said.append(text)

    async def generate_reply(self, **kw):
        self.replies.append(kw)

    def shutdown(self, *a, **kw):
        self.shutdowns += 1


async def _away(session: FakeSession, times: int) -> None:
    for _ in range(times):
        session.emit("user_state_changed", SimpleNamespace(new_state="away"))
        # let the scheduled on_away task run to completion
        for _ in range(5):
            await asyncio.sleep(0)


def _safety_with_counter():
    agent = SafetyAgent()
    calls: list[tuple[str, str, str]] = []

    async def fake_escalate(reason, urgency, source):
        calls.append((reason, urgency, source))
        return True

    agent._escalate = fake_escalate
    agent.note_silence = lambda n: None  # T16 needs a live session
    return agent, calls


# --- AC5.1 -------------------------------------------------------------------


async def test_safety_first_away_enters_quiet_mode():
    agent, calls = _safety_with_counter()
    session = FakeSession(agent)
    handle_silence(session)
    await _away(session, 1)
    assert agent.state.quiet_mode
    assert "press one" in session.said[0]
    assert calls == []
    assert not any(ch.isdigit() for ch in session.said[0])


async def test_safety_second_away_escalates_once():
    agent, calls = _safety_with_counter()
    session = FakeSession(agent)
    handle_silence(session)
    await _away(session, 2)
    assert [c[2] for c in calls] == ["silence"]
    assert "alerted" in session.said[-1]


async def test_safety_never_hangs_up_on_silence():
    agent, calls = _safety_with_counter()
    session = FakeSession(agent)
    handle_silence(session)
    await _away(session, 8)
    assert session.shutdowns == 0
    assert len(calls) == 1
    # "I'm still here" on away events 4 and 8 only
    assert session.said.count("I'm still here.") == 2


async def test_safety_undelivered_escalation_never_says_alerted():
    agent = SafetyAgent()
    agent.note_silence = lambda n: None
    session = FakeSession(agent)
    handle_silence(session)
    await _away(session, 2)  # real stub: log-only, delivered=False
    assert "alerted" not in session.said[-1]
    assert "trying to reach" in session.said[-1]


async def test_speaking_resets_away_counter():
    agent, calls = _safety_with_counter()
    session = FakeSession(agent)
    handle_silence(session)
    await _away(session, 1)
    session.emit("user_state_changed", SimpleNamespace(new_state="speaking"))
    await _away(session, 1)
    assert calls == []


async def test_booking_still_hangs_up_on_silence():
    session = FakeSession(BookingAgent())
    handle_silence(session)
    await _away(session, MAX_CHECK_INS + 1)
    assert session.shutdowns == 1
    assert len(session.replies) == MAX_CHECK_INS + 1


# --- AC5.5 dropped call ---------------------------------------------------------


def _agent_with_spawn_capture():
    agent = SafetyAgent()
    spawned: list = []

    def fake_spawn(coro):
        spawned.append(coro)
        coro.close()

    agent._spawn = fake_spawn
    return agent, spawned


def test_dropped_call_with_active_plan_alerts_once():
    agent, spawned = _agent_with_spawn_capture()
    agent.state.phase = Phase.DECIDED
    agent._on_close(CloseEvent(reason=CloseReason.PARTICIPANT_DISCONNECTED))
    agent.handle_disconnect("participant_disconnected")  # room event, same call
    assert len(spawned) == 1


def test_dropped_call_after_safe_confirmed_no_alert():
    agent, spawned = _agent_with_spawn_capture()
    agent.state.phase = Phase.DECIDED
    agent.state.safe_confirmed = True
    agent._on_close(CloseEvent(reason=CloseReason.PARTICIPANT_DISCONNECTED))
    assert spawned == []


def test_dropped_call_without_plan_no_alert():
    agent, spawned = _agent_with_spawn_capture()
    agent._on_close(CloseEvent(reason=CloseReason.PARTICIPANT_DISCONNECTED))
    assert spawned == []


def test_user_initiated_close_no_alert():
    agent, spawned = _agent_with_spawn_capture()
    agent.state.phase = Phase.DECIDED
    agent._on_close(CloseEvent(reason=CloseReason.USER_INITIATED))
    assert spawned == []


# --- tools (offline) -------------------------------------------------------------


async def test_emergency_contacts_france_us_passport():
    agent = SafetyAgent()
    out = await agent.get_emergency_contacts(None, "FR", "US")
    assert "one one two" in out
    assert "police one seven" in out
    assert "ambulance one five" in out
    assert "fire one eight" in out
    assert "plus three three, one, four three, one two, two two, two two" in out
    assert {"112", "17", "15", "18", "33143122222"} <= agent.state.allowed_numbers
    assert not any(ch.isdigit() for ch in out)


async def test_emergency_contacts_unknown_country_escalates():
    agent, spawned = _agent_with_spawn_capture()
    out = await agent.get_emergency_contacts(None, "ZZ", "")
    assert "don't have verified numbers" in out
    assert len(spawned) == 1


async def test_assess_situation_sets_decision():
    agent = SafetyAgent()
    out = await agent.assess_situation(
        None, "unrest", "same_street", "stable", True, True, False
    )
    assert out.startswith("SHELTER")
    assert agent.state.phase == Phase.DECIDED
    assert agent.state.plan_active()


async def test_set_quiet_mode_updates_instructions():
    agent = SafetyAgent()
    await agent.set_quiet_mode(None, True)
    assert "Quiet mode is ON" in agent.instructions
    await agent.set_quiet_mode(None, False)
    assert "Quiet mode is ON" not in agent.instructions


async def test_confirm_safe_resolves():
    agent = SafetyAgent()
    agent.state.phase = Phase.CHECK_IN
    await agent.confirm_safe(None)
    assert agent.state.safe_confirmed and agent.state.phase == Phase.RESOLVED
    assert not agent.state.plan_active()


async def test_escalate_stub_never_claims_alerted():
    agent = SafetyAgent()
    out = await agent.escalate_to_human(None, "caller asked for a person", "soon")
    assert "alerted" not in out


@pytest.mark.parametrize("profile", ["booking", "screener", "mine"])
def test_other_prompts_unchanged(profile):
    import booking_agent
    import my_agent
    import screener_agent
    from prompts import build_instructions

    mod = {"booking": booking_agent, "screener": screener_agent, "mine": my_agent}[
        profile
    ]
    text = build_instructions(mod.JOB_PROMPT)
    assert "Crisis call rules" not in text
    assert text == build_instructions(mod.JOB_PROMPT, "")
