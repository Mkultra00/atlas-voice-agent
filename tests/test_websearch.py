"""Unit tests for sovereign.websearch — offline; fetchers monkeypatched."""
import asyncio

import sovereign.websearch as ws
import pytest


# ---- detection ---------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "I think my friend is having a heart attack in Lisbon",
    "Where is the nearest hospital?",
    "There was an earthquake in Tokyo, what do I do?",
    "I need the police, someone stole my passport",
    "What should I do during a fire in a hotel?",
    "My daughter is missing at the concert",
])
def test_emergency_positive(text):
    assert ws.is_emergency(text)


@pytest.mark.parametrize("text", [
    "Translate thank you into Japanese",
    "Recommend a quiet cafe in Rome",
    "What does the word resilience mean?",
    "Book me a table for four tonight",
    "Remind me to email Priya about the deck",
])
def test_emergency_negative(text):
    assert not ws.is_emergency(text)


# ---- static numbers table ----------------------------------------------------

def test_numbers_japan():
    nums = ws.numbers_for("I'm in Osaka, Japan and need an ambulance")
    assert nums and "119" in nums


def test_numbers_tokyo_alias():
    nums = ws.numbers_for("earthquake in tokyo, what now?")
    assert nums and "119" in nums


def test_numbers_longest_token_wins():
    assert ws.numbers_for("call 911 in united states now") == "911"
    assert ws.numbers_for("usa help") == "911"


def test_numbers_fallback():
    assert ws.numbers_for("emergency in Lisbon, Portugal") == "112"
    assert ws.numbers_for("I need help at the airport") is None
    assert "112" in ws.DEFAULT_NUMBERS


# ---- extraction --------------------------------------------------------------

def test_place_in():
    assert ws.place_in("nearest hospital in Lisbon") == "Lisbon"
    assert ws.place_in("police near Tokyo Station now") in {"Tokyo Station", "Tokyo"}


def test_kind_in():
    assert ws.kind_in("where is a pharmacy") == "pharmacy"
    assert ws.kind_in("french embassy please") == "embassy"
    assert ws.kind_in("read me the news") is None


# ---- block assembly (fetchers faked) ---------------------------------------

@pytest.fixture()
def fake_sources(monkeypatch):
    async def facilities(client, place, kind):
        return [f"{kind.title()} da Ordem Terceira (38.7,-9.1)"]
    async def wiki(client, query):
        return ["Earthquake preparedness: drop, cover, hold on..."]
    async def recent_quakes(client):
        return ["M5.5 10 km NW of Hamakita, Japan at 2026-11-07 17 UTC"]
    monkeypatch.setattr(ws, "facilities", facilities)
    monkeypatch.setattr(ws, "wiki", wiki)
    monkeypatch.setattr(ws, "recent_quakes", recent_quakes)


def test_block_full(fake_sources):
    block = asyncio.run(ws.emergency_block(
        "There was an earthquake in Tokyo, where is the nearest hospital?"))
    assert block.startswith("LIVE WEB CONTEXT")
    assert "119" in block                      # japan numbers present
    assert "live OSM" in block                 # hospital lookup present
    assert "USGS" in block                     # quake feed present
    assert "live sources show" in block        # citation permission present


def test_block_numbers_only(fake_sources):
    block = asyncio.run(ws.emergency_block("Someone stole my wallet, help"))
    assert "112" in block                      # universal default numbers
    assert block.startswith("LIVE WEB CONTEXT") or block.startswith("LIVE WEB CHECK")


def test_block_offline(fake_sources, monkeypatch):
    async def dead(client, *a, **k):
        raise ConnectionError("offline demo")
    monkeypatch.setattr(ws, "facilities", dead)
    monkeypatch.setattr(ws, "wiki", dead)
    monkeypatch.setattr(ws, "recent_quakes", dead)
    block = asyncio.run(ws.emergency_block("earthquake near Lisbon, what now?"))
    assert block.startswith("LIVE WEB CHECK")  # graceful degradation
    assert "112" in block                      # portugal numbers still present


def test_non_emergency_is_empty(fake_sources):
    assert asyncio.run(ws.emergency_block("Recommend a pizza place")) == ""
