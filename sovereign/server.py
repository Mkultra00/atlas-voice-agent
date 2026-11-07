"""Atlas sovereign server — the whole voice loop, no LiveKit anywhere.

POST /api/turn  {text} | multipart audio -> {transcript?, reply, audio_b64?}
GET  /health
GET  /            -> tap-to-talk page (frontend/sovereign/index.html)
POST /alert       -> proxy to ATLAS_OPS_WEBHOOK (ops desk)

Run: .venv/bin/python -m uvicorn sovereign.server:app --port 8790
"""
from __future__ import annotations

import base64
import re
import sys
from pathlib import Path

import httpx
from fastapi import Body, FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, JSONResponse

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from sovereign.config import REPO_ROOT, SovereignConfig  # noqa: E402
from sovereign import llm, memory, operator, retrieval, stt, tts  # noqa: E402
from sovereign.kb import KB  # noqa: E402

try:
    from src.voice_guard import clean  # guarded-output reuse from the LiveKit era
except Exception:  # pragma: no cover
    MAX_CHARS = 240

    def clean(text: str) -> str:  # type: ignore[misc]
        return text[:MAX_CHARS].rsplit(".", 1)[0].strip() + "."

MAX_CHARS = 240
MAX_OPERATOR_ROUNDS = 2
CONN_HINTS = re.compile(
    r"\b(calendar|schedule|meetings?|emails?|inbox|mail\b|battery|vitals?)\b", re.I)


def _suggest(user_text: str) -> str | None:
    """Best capability guess from phrasing — deterministic hint for the nudge."""
    t = user_text.lower()
    email = bool(re.search(r"\b(e?mails?|gmail)\b", t))
    readverb = bool(re.search(r"\b(check|search|read|look|scan|find|any|show|list|from)\b", t))
    if email and not readverb:
        return "mail_send"  # email mentioned with no read verbs → send request
    if re.search(r"\b(send|write|draft)\b.*\b(e?mails?|gmail)\b|\b(e?mails?|gmail)\b.*\bsend\b", t) \
            or re.search(r"email\s+\S+@|\bto\s+\S+@agentmail", t):
        return "mail_send"
    if "inbox" in t and "check" in t:
        return "inbox_check"
    if email:
        return "gmail_search"
    if re.search(r"calendar|schedule|meetings?", t):
        return "calendar_today"
    if re.search(r"battery|disk|machine|vitals|system status", t):
        return "machine_status"
    return None


# pending mutating op awaiting user confirmation (single-operator demo server)
PENDING: dict[str, str] = {}
AFFIRM = re.compile(r"\b(yes|yep|yeah|sure|confirm(ed)?|go ahead|send it|do it|ok(ay)?)\b", re.I)


def _parse_mail_intent(text: str) -> dict[str, str] | None:
    """Deterministic mail_send args from spoken-style text: 'email X at Y saying Z'."""
    m = re.search(r"([a-z0-9._%+-]+)\s+(?:at|@)\s+([a-z0-9._%+-]+(?:\.[a-z]{2,})?)", text, re.I) \
        or re.search(r"([a-z0-9._%+-]+)@([a-z0-9._%+-]+\.[a-z]{2,})", text, re.I)
    if not m:
        return None
    dom = m.group(2).lower().rstrip(".")
    dom = {"agentmail": "agentmail.to"}.get(dom, dom)  # spoken shorthand
    if "." not in dom:
        return None
    body = re.search(r"\bsaying\s+(.+)$", text, re.I | re.S)
    message = body.group(1).strip().rstrip(".!?") if body else "Hello from Atlas."
    return {"to": f"{m.group(1).lower()}@{dom}", "subject": f"Atlas: {message[:38]}", "text": message}

CFG = SovereignConfig.resolve()
app = FastAPI(title="Atlas Sovereign", version="0.1.0")


@app.on_event("startup")
async def _warm_llm() -> None:
    """Warm the cloud LLM lane at boot, then refresh every 3 min so demo turns never
    eat the ~60-100s cold start (ollama session expires / unloads when idle)."""
    import asyncio

    async def _warm():
        while True:
            try:
                await llm.chat([{"role": "user", "content": "warm"}], CFG, timeout=120.0)
            except Exception:
                pass
            await asyncio.sleep(180.0)

    asyncio.create_task(_warm())
HISTORY: list[dict[str, str]] = []


def _trim(text: str) -> str:
    out = clean(text)
    if len(out) <= MAX_CHARS:
        return out
    cut = out[:MAX_CHARS].rsplit(".", 1)[0].strip()
    return cut + "." if cut else out


async def _resolve_operator(messages: list[dict[str, str]], reply_raw: str) -> tuple[str, str | None]:
    spoken, directive = operator.parse_directive(reply_raw)
    used: list[str] = []
    for _ in range(MAX_OPERATOR_ROUNDS):
        if not directive:
            break
        tool = str(directive.get("tool", ""))
        used.append(tool)
        obs = await operator.execute(CFG, tool, directive.get("args") or {}, bool(directive.get("confirm")))
        messages = messages + [
            {"role": "assistant", "content": reply_raw[:4000]},
            {"role": "system", "content": (
                f"OPERATOR OBSERVATION for {tool}:\n{obs}\nAnswer the traveler from this "
                "observation now, spoken style, no directive marker unless a further "
                "operator call is truly needed.")},
        ]
        reply_raw = await llm.chat(messages, CFG)
        spoken, directive = operator.parse_directive(reply_raw)
    return (spoken or reply_raw).strip(), ("+".join(used) if used else None)


@app.get("/health")
async def health():
    return {"ok": True, "llm": CFG.llm_model, "llm_base": CFG.llm_base_url,
            "operator": "on" if CFG.operator_enabled else "off"}


@app.get("/api/operator")
async def operator_api():
    """Capability registry for the UI / debugging."""
    return {"enabled": CFG.operator_enabled, "capabilities": operator.describe()}


@app.get("/")
async def index():
    page = REPO_ROOT / "frontend" / "sovereign" / "index.html"
    if page.exists():
        return FileResponse(page)
    return JSONResponse({"ok": True, "hint": "POST /api/turn or /alert"})


@app.post("/api/turn")
async def turn(text: str | None = Form(None), audio: UploadFile | None = File(None)):
    transcript = ""
    if audio is not None:
        raw = await audio.read()
        if not raw:
            return JSONResponse({"error": "empty audio"}, status_code=400)
        transcript = await stt.transcribe(raw, filename=audio.filename or "utterance.webm", cfg=CFG)
    user_text = (text or transcript).strip()
    if not user_text:
        return JSONResponse({"error": "no input"}, status_code=400)

    blocks = [b for b in (retrieval.context_block(user_text), memory.memory_block(user_text)) if b]
    ops_desc = operator.describe() if CFG.operator_enabled else None
    messages = llm.build_messages_with_ops(HISTORY[-8:], blocks, ops_desc)
    hint = _suggest(user_text)
    if ops_desc and hint and CFG.operator_enabled:
        messages[0]["content"] += (
            f'\n\nCONNECTOR ROUTING: this request maps to capability "{hint}". If an operator '
            f"action fits, your directive must use exactly that tool id (confirm:false unless "
            f"the user already confirmed this action).")
    if bool(PENDING) and bool(AFFIRM.search(user_text)):
        # deterministic confirm: user already asked + just said yes → execute now
        obs = await operator.execute(CFG, "mail_send", dict(PENDING), True)
        PENDING.clear()
        messages[0]["content"] += (
            "\n\nOPERATOR OBSERVATION (mail_send executed after explicit user confirmation):\n"
            + obs + "\n\nSpeak the send outcome plainly; hide all directive machinery.")
        raw = await llm.chat(messages, CFG)
    elif ops_desc and hint and hint != "mail_send" and CFG.operator_enabled:
        # deterministic read lane: run the connector directly, phrase from its result
        # with NO history — kills stale echoes and LLM misrouting.
        obs = await operator.execute(CFG, hint, {}, False)
        messages = [
            messages[0],
            {"role": "user", "content": (
                f"{user_text}\n\n[TOOL RESULT — answer with this data; hide machinery]:\n{obs}")},
        ]
        raw = await llm.chat(messages, CFG)
    else:
        raw = await llm.chat(messages, CFG)
        if (ops_desc and CFG.operator_enabled
                and operator.parse_directive(raw)[1] is None
                and CONN_HINTS.search(user_text)):
            lane = f'Use EXACTLY the capability "{hint}"' if hint else "Pick the matching capability"
            nudge = messages + [
                {"role": "assistant", "content": raw[:4000]},
                {"role": "system", "content": (
                    "REMINDER: the user asked about a connector task. Re-issue the reply WITH "
                    f"one <<OPERATOR>> directive. {lane}; keep the spoken line before the marker.")},
            ]
            raw = await llm.chat(nudge, CFG)
    # GUARD: no connector intent in the user's line → strip any self-initiated
    # operator directive (memory-primed LLM would otherwise burn ~90s on tool runs).
    if hint is None and not CONN_HINTS.search(user_text):
        raw = re.sub(r"<<OPERATOR>>.*?(?:<<OPERATOR>>|$)", "", raw, flags=re.S)
    reply, ops_used = await _resolve_operator(messages, raw)
    if (ops_desc and CFG.operator_enabled and not PENDING
            and _suggest(user_text) == "mail_send"):
        # arm even if confirm:false attempts were blocked this turn — they sent nothing
        intent = _parse_mail_intent(user_text)
        if intent:
            PENDING.update(intent)  # arm the two-turn confirm flow
    reply = _trim(reply)
    # NARRATION SCRUB: plain chat with no tool run — never allow "let me check..."
    if (not ops_used and hint is None and not CONN_HINTS.search(user_text)
            and re.search(r"\blet me (check|pull|look|search|scan|grab|find)\b", reply, re.I)):
        retry = messages + [
            {"role": "assistant", "content": reply},
            {"role": "system", "content": (
                "Your reply announced a check you did not run. Re-answer with NO promises and "
                "NO announcements of actions — just answer or ask one short question. Spoken "
                "line only, nothing else.")},
        ]
        raw2 = await llm.chat(retry, CFG)
        raw2 = re.sub(r"<<OPERATOR>>.*?(?:(?:<<)?/OPERATOR>>|$)", "", raw2, flags=re.S)
        if raw2.strip():
            reply = _trim(raw2.strip())
            if re.search(r"\blet me (check|pull|look|search|scan|grab|find)\b", reply, re.I):
                raw3 = await llm.chat(retry, CFG)  # second and final attempt
                raw3 = re.sub(r"<<OPERATOR>>.*?(?:(?:<<)?/OPERATOR>>|$)", "", raw3, flags=re.S)
                if (raw3.strip() and not re.search(
                        r"\blet me (check|pull|look|search|scan|grab|find)\b", raw3, re.I)):
                    reply = _trim(raw3.strip())

    HISTORY.append({"role": "user", "content": user_text})
    HISTORY.append({"role": "assistant", "content": reply})
    memory.remember(f"incident: {user_text}")

    result: dict = {"transcript": transcript, "reply": reply, "operator": ops_used}
    if CFG.eleven_api_key:
        try:
            audio_bytes = await tts.speak(reply, CFG)
            result["audio_b64"] = base64.b64encode(audio_bytes).decode()
        except Exception as exc:  # voice lane optional; text still returns
            result["audio_error"] = f"{type(exc).__name__}: {exc}"
    return result


@app.post("/alert")
async def alert(payload: dict = Body(...)):
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            r = await client.post(CFG.ops_webhook, json=payload)
            return {"forwarded": r.status_code}
        except Exception as exc:
            return {"forwarded": 0, "error": f"{type(exc).__name__}: {exc}"}


@app.get("/kb")
async def kb():
    return {"entries": KB}
# TODO: /alert proxy currently transparent — LiveKit-era ops desk (port 8787) expects
# full traveler-card dict (src/safety_agent.py ops_payload); build sovereign incident-card
# adapter when Cognee memory shapes it.


if __name__ == "__main__":
    import os
    import uvicorn
    uvicorn.run(app, host=os.environ.get("ATLAS_HOST", "0.0.0.0"), port=8790)
