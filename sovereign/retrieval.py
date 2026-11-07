"""Triage-time retrieval. Sponsor slot: Superlinked. Fallback: keyword score.

Superlinked wiring (when pip install superlinked is in place): embed KB bodies
with a text-slotted schema and query with the transcript. v1 ships a TF
overlap scorer that needs zero model downloads and works offline.
"""
from __future__ import annotations

import re

from .kb import KB


def _tokens(text: str) -> set[str]:
    stop = {"the", "a", "an", "is", "are", "and", "or", "to", "of", "in", "on", "for", "my", "i", "me", "it", "at", "was", "be"}
    words = re.findall(r"[a-z]{3,}", text.lower())
    return {w for w in words if w not in stop}


def keyword_score(query: str, doc: str) -> float:
    qt, dt = _tokens(query), _tokens(doc)
    if not qt:
        return 0.0
    return len(qt & dt) / len(qt)


def retrieve(query: str, k: int = 3) -> list[dict[str, str]]:
    scored = sorted(
        ((keyword_score(query, e["body"] + " " + e["title"]), e) for e in KB),
        key=lambda pair: -pair[0],
    )
    return [e for score, e in scored[:k] if score > 0]


def context_block(query: str, k: int = 3) -> str:
    hits = retrieve(query, k)
    if not hits:
        return ""
    lines = [f"- {e['title']}: {e['body']}" for e in hits]
    return "Relevant procedures:\n" + "\n".join(lines)
