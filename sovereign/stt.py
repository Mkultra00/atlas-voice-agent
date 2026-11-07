"""ElevenLabs Scribe STT — reuses the existing ELEVEN_API_KEY, no new account."""
from __future__ import annotations

import httpx

from .config import SovereignConfig

STT_URL = "https://api.elevenlabs.io/v1/speech-to-text"


async def transcribe(audio: bytes, filename: str = "utterance.webm", cfg: SovereignConfig | None = None) -> str:
    cfg = cfg or SovereignConfig.resolve()
    files = {"file": (filename, audio, "application/octet-stream")}
    data = {"model_id": "scribe_v1"}
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(STT_URL, files=files, data=data, headers={"xi-api-key": cfg.eleven_api_key})
        r.raise_for_status()
        body = r.json()
    return (body.get("text") or "").strip()
