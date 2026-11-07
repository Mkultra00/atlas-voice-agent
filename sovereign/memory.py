"""Agent memory, sovereign edition. Slot: Cognee knowledge graph.

v1: JSONL incident store + keyword recall — zero deps, fully local, survives
offline. When cognee (pip install cognee) is wired, `cognee_remember` /
`cognee_recall` become primary; this store stays as the audit trail.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

STORE = Path(__file__).resolve().parent / "memory" / "incidents.jsonl"


def remember(text: str, path: Path = STORE) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": time.time(), "text": text}
    with path.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def recall(query: str, k: int = 3, path: Path = STORE) -> list[dict]:
    if not path.exists():
        return []
    entries = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if not entries:
        return []
    from .retrieval import keyword_score

    scored = sorted(((keyword_score(query, e["text"]), e) for e in entries), key=lambda p: -p[0])
    top = [e for score, e in scored[:k] if score > 0.02]
    if not top:  # nothing relevant — return the freshest entries so context stays real
        top = entries[-k:]
    return top


def memory_block(query: str) -> str:
    hits = recall(query)
    if not hits:
        return ""
    lines = [f"- [{time.strftime('%m-%d %H:%M', time.localtime(e['ts']))}] {e['text']}" for e in hits]
    return "Prior incidents involving this traveler:\n" + "\n".join(lines)
