"""Runtime guards on what the caller hears (TDD sections 6.3-6.5).

- lint_reply(): H2 short-turn check. Logged at runtime, asserted in tests.
- number_guard(): H4. Blocks any spoken digit run that no tool or caller produced.
- clean(): strips markdown, list markers and emoji before TTS.
- guard_stream(): sentence-buffered wrapper used in SafetyAgent.tts_node.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterable
from dataclasses import dataclass, field

BLOCKED_SENTENCE = "Let me get that number from my verified list."

SENTENCE_END = re.compile(r"[.!?](\s|$)")

_DIGIT_WORDS = {
    "zero": "0",
    "oh": "0",
    "o": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
}
# Tokens that sit inside a digit run without breaking it.
_JOINERS = {"plus", "+", "-", "dash", "(", ")"}
# Tokens that continue a run but mark a group boundary ("plus three three, one").
_SEPARATORS = {",", "."}

_TOKEN = re.compile(r"[A-Za-z]+|\d+|[+\-.,()]")

_EMOJI = re.compile(
    "[\U0001f300-\U0001faff\U00002600-\U000027bf\U0001f000-\U0001f2ff\U0000fe0f]"
)
_MARKDOWN = re.compile(r"[*#`_~]|\[|\]")
_LIST_MARKER = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+", re.MULTILINE)

_EMPATHY = re.compile(r"\b(that sounds|i'm sorry|i am sorry|i understand)\b", re.I)


def _digit_runs(text: str) -> list[tuple[str, list[str]]]:
    """Spelled or numeric digit runs of 3+ digits, each with its comma groups.

    "nine one one" -> ("911", ["911"]);
    "plus three three, one, four three" -> ("33143", ["33", "1", "43"]);
    "double two" -> "22". Spaces, dashes and "plus" don't break a run.
    """
    runs: list[tuple[str, list[str]]] = []
    groups: list[str] = [""]

    def flush() -> None:
        full = "".join(groups)
        if len(full) >= 3:
            runs.append((full, [g for g in groups if g]))
        groups.clear()
        groups.append("")

    tokens = _TOKEN.findall(text)
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        low = tok.lower()
        in_run = bool("".join(groups))
        if tok.isdigit():
            groups[-1] += tok
        elif low in ("double", "triple") and i + 1 < len(tokens):
            nxt = tokens[i + 1].lower()
            d = _DIGIT_WORDS.get(nxt)
            if d is None and nxt.isdigit() and len(nxt) == 1:
                d = nxt
            if d is not None:
                groups[-1] += d * (2 if low == "double" else 3)
                i += 2
                continue
            flush()
        elif low in _DIGIT_WORDS and (low not in ("o", "oh") or in_run):
            # "o"/"oh" count as zero only inside a run ("four oh four")
            groups[-1] += _DIGIT_WORDS[low]
        elif low in _JOINERS:
            pass
        elif low in _SEPARATORS:
            if in_run and groups[-1]:
                groups.append("")
        else:
            flush()
        i += 1
    flush()
    return runs


def extract_digit_runs(text: str) -> set[str]:
    """Digit runs (3+ digits) in text, plus their 3+ digit groups, digits only."""
    out: set[str] = set()
    for full, groups in _digit_runs(text):
        out.add(full)
        out.update(g for g in groups if len(g) >= 3)
    return out


_SPOKEN = [
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
]


def speak_number(number: str) -> str:
    """'+33 1 43 12 22 22' -> 'plus three three, one, four three, one two, ...'.

    Space-separated groups become comma-separated spoken groups. Round-trips
    through _digit_runs, so the guard accepts what this produces.
    TODO(step 9): TDD puts this in safety_tools.py; re-export from there.
    """
    prefix = "plus " if number.strip().startswith("+") else ""
    groups = [re.sub(r"\D", "", g) for g in number.split()]
    words = [" ".join(_SPOKEN[int(c)] for c in g) for g in groups if g]
    return prefix + ", ".join(words)


def digits_only(number: str) -> str:
    return re.sub(r"\D", "", number)


def _allowed(run: str, allowed: set[str]) -> bool:
    return any(run in a for a in allowed)


def number_guard(
    sentence: str, allowed: set[str], log: logging.Logger | None = None
) -> str:
    """Return sentence unchanged if every digit run is in `allowed`, else a safe line."""
    for full, groups in _digit_runs(sentence):
        # Whole run verified, or each comma group verified ("one one two, one seven").
        if _allowed(full, allowed) or all(_allowed(g, allowed) for g in groups):
            continue
        if log is not None:
            log.warning("number_guard_block run_len=%d", len(full))
        return BLOCKED_SENTENCE + (" " if sentence.endswith((" ", "\n")) else "")
    return sentence


def clean(text: str) -> str:
    """Strip markdown, list markers at line start and emoji."""
    text = _LIST_MARKER.sub("", text)
    text = _MARKDOWN.sub("", text)
    text = _EMOJI.sub("", text)
    return text


@dataclass
class LintResult:
    ok: bool
    sentences: int
    questions: int
    words: int
    problems: list[str] = field(default_factory=list)


def lint_reply(text: str, *, quiet_mode: bool = False) -> LintResult:
    problems: list[str] = []
    stripped = text.strip()
    sentences = len([s for s in re.split(r"(?<=[.!?])\s+", stripped) if s.strip()])
    questions = stripped.count("?")
    words = len(stripped.split())
    has_number = bool(_digit_runs(stripped))

    max_sentences = 3 if has_number else 2
    if sentences > max_sentences:
        problems.append(f"sentences={sentences}")
    if questions > 1:
        problems.append(f"questions={questions}")
    max_words = 10 if quiet_mode else 40
    if words > max_words:
        problems.append(f"words={words}")
    if _LIST_MARKER.search(text):
        problems.append("list")
    if _MARKDOWN.search(text):
        problems.append("markdown")
    if _EMOJI.search(text):
        problems.append("emoji")
    if len(_EMPATHY.findall(text)) > 1:
        problems.append("empathy")
    return LintResult(not problems, sentences, questions, words, problems)


async def guard_stream(
    text: AsyncIterable[str],
    allowed: set[str],
    log: logging.Logger | None = None,
    *,
    quiet_mode: bool = False,
) -> AsyncIterable[str]:
    """Buffer to sentence ends, number-guard and clean each sentence, lint at the end.

    `allowed` is read live, so numbers added mid-reply (by a tool) count.
    """
    buf = ""
    spoken: list[str] = []
    async for chunk in text:
        buf += chunk
        while m := SENTENCE_END.search(buf):
            sentence, buf = buf[: m.end()], buf[m.end() :]
            out = clean(number_guard(sentence, allowed, log))
            spoken.append(out)
            yield out
    if buf.strip():
        out = clean(number_guard(buf, allowed, log))
        spoken.append(out)
        yield out
    if log is not None and spoken:
        res = lint_reply("".join(spoken), quiet_mode=quiet_mode)
        if not res.ok:
            log.warning(
                "short_turns_violation sentences=%d questions=%d words=%d problems=%s",
                res.sentences,
                res.questions,
                res.words,
                ",".join(res.problems),
            )
