"""Phase B SafetyAgent tools, offline (TDD AC3.1, AC3.2, AC5.7, step 14).

Network helpers are stubbed or pointed at a local aiohttp server; the
"mocked webhook" is a real local HTTP endpoint, so retries, status codes and
idempotency headers are exercised end to end.
"""

import asyncio
import json
import os
from types import SimpleNamespace

import pytest
from aiohttp import web

import safety_agent as sa
import safety_tools as st
from safety_agent import NO_LOCATION, NOT_ANSWERED, Phase, SafetyAgent

ANCHOR = (48.8769487, 2.3554685)
GEO = {
    "label": "Hotel Le Nord, Boulevard de Magenta, Paris 10e Arrondissement, Paris",
    "lat": ANCHOR[0],
    "lon": ANCHOR[1],
    "country_code": "FR",
    "city": "Paris",
    "source": "demo",
}


@pytest.fixture
def offline(monkeypatch):
    """geocode -> demo hotel; Overpass -> demo cache; OSRM -> demo route."""

    async def geocode(q):
        return dict(GEO) if "nord" in q.lower() or "magenta" in q.lower() else None

    async def nearby(lat, lon, radius_m=2000, nationality="", timeout=2.5):
        return st.demo_nearby(lat, lon, nationality) or None

    async def route(src, dst, mode="walk", dest_name=""):
        r = st.demo_route(src, dest_name, mode)
        return {**r, "source": "demo"} if r else None

    monkeypatch.setattr(sa.st, "geocode", geocode)
    monkeypatch.setattr(sa.st, "overpass_nearby", nearby)
    monkeypatch.setattr(sa.st, "osrm_route", route)


def _agent() -> SafetyAgent:
    a = SafetyAgent()
    a.state.nationality = "US"
    a._incident_id = "inc0000001"
    return a


def _caller_speaks(a: SafetyAgent) -> None:
    a.state.user_turns += 1


async def _located(a: SafetyAgent) -> None:
    await a.locate_caller(None, "Hotel Le Nord, Boulevard de Magenta, Paris")
    _caller_speaks(a)
    assert await a.confirm_location(None, True) == "Location confirmed."
    await asyncio.gather(*a._bg)  # prefetch


# ---- location gate (AC3.1) ---------------------------------------------------------


async def test_locate_reads_back(offline):
    a = _agent()
    out = await a.locate_caller(None, "Hotel Le Nord, Paris")
    assert out.startswith("Found: Hotel Le Nord, Boulevard de Magenta")
    assert "ask if it is right" in out
    assert a.state.pending_location and a.state.location is None
    assert a.state.phase == Phase.LOCATE


async def test_locate_unknown_place(offline):
    a = _agent()
    out = await a.locate_caller(None, "the blue house")
    assert "couldn't find" in out and a.state.pending_location is None


async def test_confirm_location_needs_caller_turn(offline):
    a = _agent()
    assert "locate_caller first" in await a.confirm_location(None, True)
    await a.locate_caller(None, "Hotel Le Nord, Paris")
    assert await a.confirm_location(None, True) == NOT_ANSWERED
    assert a.state.location is None
    _caller_speaks(a)
    assert await a.confirm_location(None, True) == "Location confirmed."
    assert a.state.location.country_code == "FR"
    assert a.state.phase == Phase.SITUATION
    assert {"112", "17", "15", "18"} <= a.state.allowed_numbers
    await asyncio.gather(*a._bg)
    assert a.state.nearby_cache["police"]  # prefetch filled the cache


async def test_confirm_location_no_clears_pending(offline):
    a = _agent()
    await a.locate_caller(None, "Hotel Le Nord, Paris")
    _caller_speaks(a)
    assert "again" in await a.confirm_location(None, False)
    assert a.state.pending_location is None and a.state.location is None


async def test_gate_counts_text_mode_user_messages(offline):
    """Debugger/simulations send text: no transcripts, but user messages."""
    a = _agent()
    await a.locate_caller(None, "Hotel Le Nord, Paris")
    assert await a.confirm_location(None, True) == NOT_ANSWERED
    ctx = a.chat_ctx.copy()
    ctx.add_message(role="user", content="yes")
    await a.update_chat_ctx(ctx)
    assert await a.confirm_location(None, True) == "Location confirmed."


# ---- nearby + route ------------------------------------------------------------------


async def test_find_nearby_needs_location(offline):
    assert await _agent().find_nearby_help(None, "police") == NO_LOCATION
    assert await _agent().get_route(None, "police station") == NO_LOCATION


async def test_find_nearby_hospital_and_embassy(offline):
    a = _agent()
    await _located(a)
    out = await a.find_nearby_help(None, "hospital")
    assert out.startswith(
        "Nearest hospital: Hôpital Lariboisière, seven hundred meters north"
    )
    out = await a.find_nearby_help(None, "embassy")
    assert "Ambassade des États-Unis" in out and "kilometers west" in out
    assert not any(ch.isdigit() for ch in out)


async def test_find_nearby_map_down_is_honest(monkeypatch, offline):
    async def down(*a, **kw):
        return None

    monkeypatch.setattr(sa.st, "overpass_nearby", down)
    a = _agent()
    await _located(a)
    out = await a.find_nearby_help(None, "police")
    assert "can't reach the map service" in out
    out = await a.find_nearby_help(None, "embassy")
    assert "emergency line" in out


async def test_get_route_to_police_station(offline):
    a = _agent()
    await _located(a)
    out = await a.get_route(None, "Commissariat de police de Paris 10e arrondissement")
    assert "minutes on foot" in out
    assert out.startswith("One point one kilometers")
    assert "Boulevard de Magenta" in out
    assert out.endswith("Give one step per turn.")


async def test_get_route_failure_uses_cached_distance(monkeypatch, offline):
    async def no_route(*a, **kw):
        return None

    monkeypatch.setattr(sa.st, "osrm_route", no_route)
    a = _agent()
    await _located(a)
    out = await a.get_route(None, "the hospital")
    assert out.startswith(
        "I can't get the route right now. It is seven hundred meters north."
    )


# ---- news ------------------------------------------------------------------------------


async def test_news_runs_in_background_then_reports_honestly(monkeypatch):
    a = _agent()
    captured: list[dict] = []

    class S:
        async def generate_reply(self, **kw):
            captured.append(kw)

    monkeypatch.setattr(SafetyAgent, "session", property(lambda self: S()))
    out = await a.search_local_news(None, "riot Magenta Paris")
    assert "background" in out  # immediate status: the voice keeps talking
    tasks = list(a._bg)
    if tasks:
        await asyncio.gather(*tasks)  # drain the research lane
    assert captured, "findings must arrive as a proactive reply"
    assert "No new reports found" in captured[0]["instructions"]  # honest, no key


# ---- check-in (AC3.2) -----------------------------------------------------------------


async def test_start_check_in_two_phase():
    a = _agent()
    plan = "Sheltering in my room, door locked, away from the window."
    out = await a.start_check_in(None, 20, plan, False)
    assert out.startswith("Read back:") and "twenty minutes" in out
    # same turn: refuse
    assert await a.start_check_in(None, 20, plan, True) == NOT_ANSWERED
    assert a._check_in_task is None
    _caller_speaks(a)
    # args differ from the read-back: no timer, new read-back
    out = await a.start_check_in(None, 10, plan, True)
    assert out.startswith("Read back:") and a._check_in_task is None
    _caller_speaks(a)
    out = await a.start_check_in(None, 10, plan, True)
    assert out == "Check-in set for ten minutes."
    assert a.state.phase == Phase.CHECK_IN and a.state.check_in_deadline
    assert a._check_in_task is not None
    await a.confirm_safe(None)  # cancels the timer
    assert a._check_in_task is None


async def test_start_check_in_clamps_minutes():
    a = _agent()
    out = await a.start_check_in(None, 90, "Walking to the station.", False)
    assert "thirty minutes" in out


# ---- escalation (step 14, AC5.7) ------------------------------------------------------


@pytest.fixture
async def hook():
    """Local webhook. Set .status to control the reply; .hits records calls."""

    h = SimpleNamespace(status=200, hits=[], url="")

    async def handler(request):
        h.hits.append(
            {
                "body": await request.json(),
                "key": request.headers.get("Idempotency-Key"),
            }
        )
        return web.json_response({}, status=h.status)

    app = web.Application()
    app.router.add_post("/hook", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    h.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/hook"
    yield h
    await runner.cleanup()


def _ops_log() -> list[dict]:
    p = st.ops_log_path()
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


async def test_webhook_unset_log_only_honest():
    a = _agent()
    out = await a.escalate_to_human(None, "caller asked for a person", "soon")
    assert "alerted" not in out and "still trying" in out
    assert not a.state.escalated
    log = _ops_log()
    assert len(log) == 1 and log[0]["status"] == "undelivered"
    assert log[0]["incident_id"] == "inc0000001"
    assert not a._bg  # no background retries without a URL


async def test_webhook_200_alerted(monkeypatch, hook, offline):
    monkeypatch.setenv("ATLAS_OPS_WEBHOOK", hook.url)
    a = _agent()
    a._caller_number = "+15550102030"
    await _located(a)
    out = await a.escalate_to_human(None, "caller asked for a person", "soon")
    assert out.startswith("A person on the duty desk has been alerted")
    assert a.state.escalated
    assert "A person has been alerted at" in a.instructions
    body = hook.hits[0]["body"]
    assert body["type"] == "atlas.escalation" and body["incident_id"] == "inc0000001"
    assert body["location"]["country_code"] == "FR"
    assert "callback" not in body["caller"]  # raw number only if opted in
    assert "+15550102030" not in json.dumps(body)
    assert _ops_log() == []
    # second alert: same incident, flagged as an update, new idempotency key
    await a.escalate_to_human(None, "threat moved closer", "now")
    assert hook.hits[1]["body"]["update"] is True
    assert hook.hits[0]["key"] != hook.hits[1]["key"]


async def test_webhook_callback_opt_in(monkeypatch, hook):
    monkeypatch.setenv("ATLAS_OPS_WEBHOOK", hook.url)
    monkeypatch.setenv("OPS_INCLUDE_CALLBACK", "1")
    a = _agent()
    a._caller_number = "+15550102030"
    await a.escalate_to_human(None, "caller asked for a person", "soon")
    assert hook.hits[0]["body"]["caller"]["callback"] == "+15550102030"


async def test_webhook_500_retries_then_still_trying(monkeypatch, hook):
    monkeypatch.setenv("ATLAS_OPS_WEBHOOK", hook.url)
    hook.status = 500
    a = _agent()
    out = await a.escalate_to_human(None, "caller asked for a person", "now")
    assert "alerted" not in out
    assert not a.state.escalated
    assert len(hook.hits) == 3  # 3 attempts
    assert len({h["key"] for h in hook.hits}) == 1  # one idempotency key
    assert _ops_log()[0]["status"] == "undelivered"
    assert len(a._bg) == 1  # background retry scheduled
    for t in list(a._bg):
        t.cancel()


async def test_background_retry_marks_escalated(monkeypatch, hook):
    monkeypatch.setenv("ATLAS_OPS_WEBHOOK", hook.url)
    real_sleep = asyncio.sleep

    async def fast_sleep(s):
        await real_sleep(0)

    monkeypatch.setattr(sa.asyncio, "sleep", fast_sleep)
    a = _agent()
    payload = a._payload("atlas.escalation", "r", "now", "llm")
    await a._retry_alert(payload, tries=2)
    assert a.state.escalated and len(hook.hits) == 1


async def test_dropped_call_posts_one_alert(monkeypatch, hook):
    monkeypatch.setenv("ATLAS_OPS_WEBHOOK", hook.url)
    a = _agent()
    a.state.phase = Phase.CHECK_IN
    from livekit.agents import CloseEvent, CloseReason

    a._on_close(CloseEvent(reason=CloseReason.PARTICIPANT_DISCONNECTED))
    a.handle_disconnect("participant_disconnected")
    await a.flush_pending_alerts()
    assert [h["body"]["source"] for h in hook.hits] == ["dropped_call"]


async def test_check_in_posts_plan_card(monkeypatch, hook):
    monkeypatch.setenv("ATLAS_OPS_WEBHOOK", hook.url)
    a = _agent()
    await a.start_check_in(None, 3, "Walking to the police station.", False)
    _caller_speaks(a)
    await a.start_check_in(None, 3, "Walking to the police station.", True)
    await a.flush_pending_alerts()
    assert hook.hits[0]["body"]["type"] == "atlas.plan"
    assert hook.hits[0]["body"]["plan"] == "Walking to the police station."
    assert not a.state.escalated  # a plan card is not an escalation
    a._check_in_task.cancel()


async def test_confirm_safe_posts_update_when_escalated(monkeypatch, hook):
    monkeypatch.setenv("ATLAS_OPS_WEBHOOK", hook.url)
    a = _agent()
    await a.escalate_to_human(None, "caller asked for a person", "soon")
    await a.confirm_safe(None)
    await a.flush_pending_alerts()
    assert hook.hits[-1]["body"]["reason"] == "caller reports safe"


async def test_dtmf_zero_escalates_and_says_alerted(monkeypatch, hook):
    monkeypatch.setenv("ATLAS_OPS_WEBHOOK", hook.url)
    a = _agent()
    said: list[str] = []

    class S:
        def say(self, text, **kw):
            said.append(text)

        def generate_reply(self, **kw):
            said.append(kw.get("user_input"))

    monkeypatch.setattr(SafetyAgent, "session", property(lambda self: S()))
    await a.on_dtmf("1")
    await a.on_dtmf("0")
    assert said[0] == "yes"
    assert a.state.quiet_mode
    assert "alerted" in said[-1]
    assert hook.hits[0]["body"]["source"] == "dtmf"


def test_no_phase_a_note_left():
    assert not hasattr(sa, "PHASE_A_NOTE")
    text = SafetyAgent().instructions
    assert "not available" not in text


def test_ops_log_not_in_repo_root():
    assert os.environ["ATLAS_OPS_LOG"] != "ops_log.jsonl"
