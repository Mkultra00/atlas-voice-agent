"""Offline tests for src/safety_tools.py (TDD 9.3, AC4.5).

No public network: "dead" endpoints are a local server that never answers in
time, so every fallback path is exercised deterministically.
"""

import asyncio
import json
import time

import pytest
from aiohttp import web

import safety_tools as st

ANCHOR = (48.8769487, 2.3554685)  # Boulevard de Magenta by Gare du Nord


@pytest.fixture
async def slow_url():
    """A local HTTP server that sleeps past every timeout."""

    async def handler(request):
        await asyncio.sleep(3.5)
        return web.json_response({})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    await runner.cleanup()


# ---- formatters -------------------------------------------------------------


@pytest.mark.parametrize(
    ("meters", "spoken"),
    [
        (20, "fifty meters"),
        (420, "four hundred meters"),
        (430, "four hundred fifty meters"),
        (980, "one kilometer"),
        (1000, "one kilometer"),
        (1100, "one point one kilometers"),
        (2763, "two point eight kilometers"),
        (12000, "twelve kilometers"),
    ],
)
def test_speak_distance(meters, spoken):
    assert st.speak_distance(meters) == spoken


def test_speak_minutes():
    assert st.speak_minutes(1) == "one minute"
    assert st.speak_minutes(20) == "twenty minutes"
    assert st.speak_minutes(44) == "forty-four minutes"


def test_speak_number_reexport():
    assert st.speak_number("17") == "one seven"
    assert st.speak_number("+33 1 43 12 22 22").startswith("plus three three, one")


def test_haversine_and_compass():
    # Gare du Nord area -> Hôpital Lariboisière, about 680 m north
    d = st.haversine_m(*ANCHOR, 48.8829019, 2.3533116)
    assert 650 < d < 710
    assert st.compass(*ANCHOR, 48.8829019, 2.3533116) == "north"
    assert st.compass(0, 0, 0, 1) == "east"
    assert st.compass(0, 0, -1, -1) == "south-west"
    assert st.bearing_to_compass(326) == "north-west"


def _route(steps, distance=400.0, duration=300.0):
    return {"distance_m": distance, "duration_s": duration, "steps": steps}


def test_speak_route_max_three_steps_and_walk_minutes():
    steps = [
        {"name": "Rue A", "maneuver": {"type": "depart", "bearing_after": 270}},
        {"name": "Rue A", "maneuver": {"type": "continue"}},  # same street: skipped
        {"name": "Rue B", "maneuver": {"type": "turn", "modifier": "left"}},
        {"name": "Rue C", "maneuver": {"type": "turn", "modifier": "right"}},
        {"name": "Rue D", "maneuver": {"type": "turn", "modifier": "right"}},
        {"name": "", "maneuver": {"type": "arrive", "modifier": "right"}},
    ]
    out = st.speak_route(_route(steps), "walk", "the hotel")
    # 400 m / 1.2 m/s = 5.6 min -> six, regardless of OSRM's duration
    assert out.startswith("Four hundred meters, about six minutes on foot.")
    assert "First, head west on Rue A." in out
    assert "Turn left onto Rue B." in out
    assert out.endswith("Then the hotel is on your right.")
    assert "Rue C" not in out  # max three steps: two moves + arrival
    assert not any(ch.isdigit() for ch in out)


def test_speak_route_drive_uses_duration():
    out = st.speak_route(_route([], 2000, 340), "drive")
    assert out == "Two kilometers, about six minutes by car."


def test_speak_route_long_unnamed_footpath():
    steps = [
        {"name": "Rue A", "maneuver": {"type": "depart", "bearing_after": 0}},
        {"name": "", "distance": 300, "maneuver": {"type": "turn", "modifier": "left"}},
        {"name": "", "distance": 5, "maneuver": {"type": "turn", "modifier": "right"}},
    ]
    out = st.speak_route(_route(steps), "walk")
    assert "Turn left and go three hundred meters." in out
    assert "right" not in out


def test_news_text_shape():
    out = st.news_text(
        answer="A march is passing the Angel. See https://x.example/1 for more. "
        "Third sentence."
    )
    assert out.startswith("Unconfirmed reports say a march")
    assert "http" not in out and "Third" not in out
    assert len(out.split()) <= 40


def test_ops_payload_wrapping():
    p = {"type": "atlas.escalation", "incident_id": "abc", "reason": "r"}
    assert "abc" in st.wrap_payload(p, "slack")["text"]
    assert "abc" in st.wrap_payload(p, "discord")["content"]
    j = st.wrap_payload(p, "json")
    assert j["incident_id"] == "abc" and j["call_id"] == "abc"
    assert j["event"] == "escalation" and j["message"] and j["location"]
    assert st.ops_format("https://hooks.slack.com/services/x") == "slack"
    assert st.ops_format("https://discord.com/api/webhooks/1/x") == "discord"
    assert st.ops_format("https://example.com/hook") == "json"


# ---- demo cache -----------------------------------------------------------------


def test_demo_cache_entries_are_sourced():
    dc = st.demo_cache()
    for key, entry in dc["geocode"].items():
        assert entry.get("_source"), key
    assert dc["nearby"]["_source"].startswith("live")
    assert dc["routes"]["_source"].startswith("live")
    assert all(n["demo_data"] for n in dc["news"])
    names = {
        p["name"]
        for kind in ("police", "hospital", "embassy")
        for p in dc["nearby"][kind]
    }
    assert "Commissariat de police de Paris 10e arrondissement" in names
    assert "Hôpital Lariboisière" in names
    assert "Ambassade des États-Unis" in names


def test_demo_nearby_only_near_anchor():
    near = st.demo_nearby(*ANCHOR, "US")
    assert near["hospital"][0]["name"] == "Hôpital Lariboisière"  # A&E first
    assert near["embassy"][0]["name"] == "Ambassade des États-Unis"
    assert st.demo_nearby(19.43, -99.17) == {}  # Mexico City: no Paris data


def test_group_places_embassy_filters_nationality_and_residence():
    els = [
        {
            "tags": {
                "office": "diplomatic",
                "name": "Ambassade du Japon",
                "country": "JP",
            },
            "lat": 48.877,
            "lon": 2.304,
        },
        {
            "tags": {
                "office": "diplomatic",
                "name": "Résidence des États-Unis",
                "country": "US",
            },
            "lat": 48.869,
            "lon": 2.318,
        },
        {
            "tags": {
                "office": "diplomatic",
                "name": "Ambassade des États-Unis",
                "country": "US",
            },
            "lat": 48.8677,
            "lon": 2.3204,
        },
        {
            "tags": {"amenity": "police"},
            "lat": 48.877,
            "lon": 2.355,
        },  # unnamed: dropped
    ]
    g = st.group_places(els, *ANCHOR, "US")
    assert [p["name"] for p in g["embassy"]] == ["Ambassade des États-Unis"]
    assert g["police"] == []


# ---- fallbacks within 2.6 s (AC4.5) ---------------------------------------------


async def _timed(coro):
    t0 = time.monotonic()
    out = await coro
    return out, time.monotonic() - t0


async def test_geocode_falls_back_to_demo(monkeypatch, slow_url):
    monkeypatch.setenv("NOMINATIM_URL", slow_url)
    r, dt = await _timed(st.geocode("Hotel Le Nord, Rue de Dunkerque, Paris"))
    assert dt < 2.6
    assert r["source"] == "demo"
    assert (r["lat"], r["lon"]) == ANCHOR
    assert r["country_code"] == "FR"


async def test_geocode_unknown_place_returns_none(monkeypatch, slow_url):
    monkeypatch.setenv("NOMINATIM_URL", slow_url)
    r, dt = await _timed(st.geocode("Zzyzx Road somewhere"))
    assert dt < 2.6 and r is None


async def test_overpass_falls_back_to_demo(monkeypatch, slow_url):
    monkeypatch.setenv("OVERPASS_URL", slow_url)
    r, dt = await _timed(st.overpass_nearby(*ANCHOR, 2000, "US"))
    assert dt < 2.6
    assert any("Commissariat" in p["name"] for p in r["police"])


async def test_overpass_far_from_demo_returns_none(monkeypatch, slow_url):
    monkeypatch.setenv("OVERPASS_URL", slow_url)
    r, dt = await _timed(st.overpass_nearby(19.43, -99.17))
    assert dt < 2.6 and r is None


async def test_osrm_falls_back_to_demo_route(monkeypatch, slow_url):
    monkeypatch.setenv("OSRM_FOOT_URL", slow_url)
    r, dt = await _timed(
        st.osrm_route(
            ANCHOR,
            (48.8808, 2.3662),
            "walk",
            "Commissariat de police de Paris 10e arrondissement",
        )
    )
    assert dt < 2.6
    assert r["source"] == "demo" and r["distance_m"] > 500


async def test_osrm_demo_route_not_used_far_from_anchor(monkeypatch, slow_url):
    monkeypatch.setenv("OSRM_FOOT_URL", slow_url)
    r, _ = await _timed(
        st.osrm_route(
            (48.85, 2.29),
            (48.8808, 2.3662),
            "walk",
            "Commissariat de police de Paris 10e arrondissement",
        )
    )
    assert r is None


async def test_tavily_without_key_is_honest():
    r, dt = await _timed(st.tavily_news("riot Magenta Paris"))
    assert r is None and dt < 0.5


async def test_tavily_demo_news_only_when_flagged(monkeypatch):
    monkeypatch.setenv("ATLAS_DEMO_NEWS", "1")
    r = await st.tavily_news("riot Boulevard de Magenta Paris")
    assert r.startswith("Unconfirmed reports say")


async def test_tavily_timeout_falls_back(monkeypatch, slow_url):
    monkeypatch.setenv("TAVILY_API_KEY", "test")
    monkeypatch.setenv("TAVILY_URL", slow_url)
    r, dt = await _timed(st.tavily_news("protest Paris"))
    assert r is None and dt < 2.6


async def test_webhook_unset_is_log_only():
    assert await st.post_ops_webhook({"incident_id": "x"}) is False


async def test_webhook_timeout_within_deadline(monkeypatch, slow_url):
    monkeypatch.setenv("ATLAS_OPS_WEBHOOK", slow_url)
    ok, dt = await _timed(st.post_ops_webhook({"incident_id": "x"}))
    assert ok is False and dt < 5.0  # 4 s deadline + one backoff


# ---- Nominatim spacing (mock clock) ---------------------------------------------


async def test_nominatim_spacing(monkeypatch):
    now = [100.0]
    slept: list[float] = []

    async def fake_sleep(s):
        slept.append(s)
        now[0] += s

    monkeypatch.setattr(st, "_clock", lambda: now[0])
    monkeypatch.setattr(st, "_sleep", fake_sleep)
    await st._nominatim_wait()  # first request: no wait
    now[0] += 0.3
    await st._nominatim_wait()  # 0.3 s later: waits the remaining 0.7 s
    now[0] += 5
    await st._nominatim_wait()  # long gap: no wait
    assert slept == [pytest.approx(0.7)]


def test_demo_cache_is_valid_json():
    raw = (st.DATA / "demo_cache.json").read_text()
    assert json.loads(raw)["anchor"]["lat"] == ANCHOR[0]
