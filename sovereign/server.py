"""Atlas sovereign server — the whole voice loop, no LiveKit anywhere.

POST /api/turn  {text} | multipart audio -> {transcript?, reply, audio_b64?}
GET  /health
GET  /            -> tap-to-talk page (frontend/sovereign/index.html)
POST /alert       -> proxy to ATLAS_OPS_WEBHOOK (ops desk)

Run: .venv/bin/python -m uvicorn sovereign.server:app --port 8790
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

import httpx
from fastapi import Body, FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, JSONResponse

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from sovereign.config import REPO_ROOT, SovereignConfig  # noqa: E402
from sovereign import llm, memory, retrieval, stt, tts  # noqa: E402
from sovereign.kb import KB  # noqa: E402

try:
    from src.voice_guard import clean  # guarded-output reuse from the LiveKit era
except Exception:  # pragma: no cover
    MAX_CHARS = 240

    def clean(text: str) -> str:  # type: ignore[misc]
        return text[:MAX_CHARS].rsplit(".", 1)[0].strip() + "."

MAX_CHARS = 240

CFG = SovereignConfig.resolve()
app = FastAPI(title="Atlas Sovereign", version="0.1.0")
HISTORY: list[dict[str, str]] = []


def _trim(text: str) -> str:
    out = clean(text)
    if len(out) <= MAX_CHARS:
        return out
    cut = out[:MAX_CHARS].rsplit(".", 1)[0].strip()
    return cut + "." if cut else out


@app.get("/health")
async def health():
    return {"ok": True, "llm": CFG.llm_model, "llm_base": CFG.llm_base_url}


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
    messages = llm.build_messages(HISTORY[-8:], blocks)
    reply = _trim(await llm.chat(messages, CFG))

    HISTORY.append({"role": "user", "content": user_text})
    HISTORY.append({"role": "assistant", "content": reply})
    memory.remember(f"incident: {user_text}")

    result: dict = {"transcript": transcript, "reply": reply}
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
TODO: /alert proxy currently transparent — LiveKit-era ops desk (port 8787) expects full traveler-card dict (src/safety_agent.py ops_payload); build sovereign incident-card adapter when Cognee memory shapes it.
