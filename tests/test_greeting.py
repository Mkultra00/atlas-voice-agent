"""First-turn greeting: Atlas identifies itself + asks where you are/how it
can help. Emergency turns keep speed (intro + answer, no forced question)."""
import pytest

import sovereign.server as srv


@pytest.fixture()
def fresh_history(monkeypatch):
    monkeypatch.setattr(srv, "HISTORY", [])
    return srv


def test_greeting_hi_replaces(fresh_history):
    out = srv._greeting_reply("hi", "Hello! How can I help today?")
    assert out is not None
    assert "Atlas" in out and "Where are you" in out and "help" in out


def test_greeting_emergency_with_place_fast(fresh_history):
    fb = "Head to the nearest hospital and call 119."
    out = srv._greeting_reply("There is an earthquake in Tokyo, where is the nearest hospital?", fb)
    assert out is not None
    assert out.startswith("Atlas here")
    assert fb in out
    assert "where exactly are you" not in out.lower()


def test_greeting_emergency_without_place_asks(fresh_history):
    fb = "Call 911 and get to open ground."
    out = srv._greeting_reply("There has been an earthquake, what should I do?", fb)
    assert out is not None
    assert out.startswith("Atlas here")
    assert "where exactly are you" in out.lower()
    assert fb in out


def test_greeting_substantive_nontopical_prefix_only(fresh_history):
    fb = "Check your airline's change rules before rebooking."
    out = srv._greeting_reply("My flight was cancelled after the strike", fb)
    assert out == "Atlas here — your travel-safety guide. " + fb


def test_greeting_second_turn_silent(monkeypatch):
    monkeypatch.setattr(
        srv, "HISTORY", [{"role": "user", "content": "hi"},
                          {"role": "assistant", "content": " Atlas here."}])
    assert srv._greeting_reply("what about Lisbon?", "x") is None
