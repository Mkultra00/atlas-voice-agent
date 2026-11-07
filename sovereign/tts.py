"""ElevenLabs TTS — guarded by the caller (MAX_CHARS) before it ever reaches here."""
from __future__ import annotations

import httpx

from .config import SovereignConfig

TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech"


async def speak(text: str, cfg: SovereignConfig) -> bytes:
    url = f"{TTS_URL}/{cfg.voice_id}?output_format=mp3_44100_128"
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(
            url,
            json={"text": text, "model_id": "eleven_multilingual_v2"},
            headers={"xi-api-key": cfg.eleven_api_key, "Content-Type": "application/json"},
        )
        r.raise_for_status()
        return r.content
