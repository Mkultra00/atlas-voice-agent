import logging

import pytest

from voice_guard import (
    BLOCKED_SENTENCE,
    clean,
    extract_digit_runs,
    guard_stream,
    lint_reply,
    number_guard,
    speak_number,
)

# --- AC4.1 number guard -----------------------------------------------------


def test_spelled_allowed_number_passes():
    assert number_guard("Call nine one one.", {"911"}) == "Call nine one one."


def test_unverified_number_blocked():
    assert number_guard("Call one one two.", {"911"}) == BLOCKED_SENTENCE


def test_caller_room_number_read_back_passes():
    allowed = extract_digit_runs("I'm in room 1412")
    assert number_guard("Room one four one two, is that right?", allowed).startswith(
        "Room"
    )


def test_numeric_digits_checked():
    assert number_guard("Dial 112 now.", {"112"}) == "Dial 112 now."
    assert number_guard("Dial 113 now.", {"112"}) == BLOCKED_SENTENCE


def test_short_numbers_and_counts_pass():
    # French short codes 17/15/18 are 2 digits; times and floors aren't runs.
    assert number_guard("Police is one seven.", set()) == "Police is one seven."
    s = "I'll check on you in twenty minutes from the fourteenth floor."
    assert number_guard(s, set()) == s


def test_comma_groups_of_separate_numbers():
    allowed = {"112", "17", "15", "18"}
    s = "Emergency one one two, police one seven, ambulance one five."
    assert number_guard(s, allowed) == s


def test_embassy_round_trip():
    spoken = speak_number("+33 1 43 12 22 22")
    assert spoken == "plus three three, one, four three, one two, two two, two two"
    allowed = {"33143122222"}
    assert number_guard(f"The embassy is {spoken}.", allowed).startswith("The embassy")
    fake = "plus three three, one, four three, one two, nine nine, nine nine"
    assert number_guard(f"The embassy is {fake}.", allowed) == BLOCKED_SENTENCE


def test_double_and_oh():
    assert extract_digit_runs("four oh four") == {"404"}
    assert extract_digit_runs("double two two") == {"222"}
    assert extract_digit_runs("Oh, one two") == set()


def test_block_logs_without_digits(caplog):
    log = logging.getLogger("test-guard")
    with caplog.at_level(logging.WARNING):
        number_guard("Call five five five one two.", {"911"}, log)
    assert "number_guard_block" in caplog.text
    assert "555" not in caplog.text


# --- AC2.1 lint_reply ----------------------------------------------------------


APPROVED = [
    "Hi, this is Atlas, an AI safety assistant. Are you safe to talk right now, and is anyone hurt?",
    "Okay, you did the right thing calling. Which hotel are you at?",
    "I have you at Hotel Le Nord, Rue de Dunkerque, Paris. Is that right?",
    "Checking local news now.",
    "Lock the door and move away from the window.",
    "The emergency number in France is one one two. Do you want me to repeat that?",
    "Use the stairs, not the lift. Before you open your door, feel it with the back of your hand.",
    "I can only help with your safety right now. Are you somewhere safe at the moment?",
    "I've alerted a person on our duty desk. I can't transfer you on this line, but I'll stay with you.",
    "So: sheltering in your room, door locked, and I'll check on you in twenty minutes. Is that right?",
]


@pytest.mark.parametrize("line", APPROVED)
def test_approved_lines_pass(line):
    res = lint_reply(line)
    assert res.ok, res.problems


def test_three_sentences_fail():
    assert not lint_reply("Stay calm. Lock the door. Move away from the window.").ok


def test_three_sentences_ok_with_number():
    assert lint_reply(
        "Call one one two now. Lock the door. Do you want me to repeat that?"
    ).ok


def test_two_questions_fail():
    res = lint_reply("Are you safe? Is anyone hurt?")
    assert not res.ok and res.questions == 2


def test_bullet_list_fails():
    assert "list" in lint_reply("- Lock the door\n- Hide").problems


def test_markdown_fails():
    assert "markdown" in lint_reply("**Lock the door** now.").problems


def test_41_words_fail():
    text = " ".join(["word"] * 41) + "."
    res = lint_reply(text)
    assert not res.ok and res.words == 41


def test_quiet_mode_word_limit():
    assert lint_reply("Say yes if you are safe.", quiet_mode=True).ok
    assert not lint_reply(
        "Say yes if you are still in the car with the driver now.", quiet_mode=True
    ).ok


def test_double_empathy_fails():
    assert "empathy" in lint_reply("I'm sorry. I understand, lock the door.").problems


def test_emoji_fails():
    assert "emoji" in lint_reply("Stay safe \U0001f64f").problems


# --- clean / guard_stream ---------------------------------------------------


def test_clean_strips_markdown_and_lists():
    assert clean("- **Lock** the door") == "Lock the door"
    assert clean("1. Go `now` \U0001f6a8") == "Go now "


async def _agen(chunks):
    for c in chunks:
        yield c


async def test_guard_stream_buffers_and_blocks():
    chunks = ["Lock the ", "door. Call one ", "one two now. Stay", " with me."]
    out = [s async for s in guard_stream(_agen(chunks), {"911"})]
    assert out == ["Lock the door. ", BLOCKED_SENTENCE + " ", "Stay with me."]


async def test_guard_stream_reads_allowed_live():
    allowed: set[str] = set()

    async def gen():
        yield "Hold on. "
        allowed.add("112")
        yield "Call one one two."

    out = [s async for s in guard_stream(gen(), allowed)]
    assert out[-1] == "Call one one two."


async def test_guard_stream_logs_lint(caplog):
    log = logging.getLogger("test-guard")
    with caplog.at_level(logging.WARNING):
        _ = [s async for s in guard_stream(_agen(["A. B. C. D?", " E?"]), set(), log)]
    assert "short_turns_violation" in caplog.text
