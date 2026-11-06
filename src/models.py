"""Model presets for the voice pipeline.

Every model here runs through LiveKit Inference, so your LiveKit Cloud keys are
the only credentials you need. No Deepgram, Google or Cartesia accounts.
Exception: the `safety` preset (Atlas) uses ElevenLabs through its plugin with
the owner's ELEVEN_API_KEY, falling back to Cartesia on LiveKit Inference.

Pick a preset with MODEL_PRESET in .env.local, or edit one below.
Full model lists: https://docs.livekit.io/agents/models/
Prices:           https://livekit.com/pricing/inference

Rough cost per minute of conversation (our estimate, not LiveKit's):
  budget    ~ $0.015 / min   -> the free $2.50 credit lasts ~150 min
  quality   ~ $0.03  / min   -> the free $2.50 credit lasts ~80 min
Text-to-speech is usually the biggest line item.
"""

import logging
import os
from dataclasses import dataclass

from livekit.agents import inference, tts

logger = logging.getLogger("models")


@dataclass
class Models:
    stt: inference.STT
    llm: inference.LLM
    tts: tts.TTS  # widened from inference.TTS: plugin TTS and FallbackAdapter too
    # Expressive mode lets the LLM add delivery tags (pacing, emotion) that the
    # TTS renders. Only turn it on for TTS models that support markup.
    expressive: bool


def budget() -> Models:
    """LiveKit's starter defaults. Cheapest, and good enough to win with."""
    return Models(
        stt=inference.STT(model="assemblyai/universal-3-6-pro", language="en"),
        llm=inference.LLM(model="google/gemma-4-31b-it"),
        tts=inference.TTS(
            model="fishaudio/s2.1-pro", voice="fa4c9eb3dccc4806b382b40d61c6b10a"
        ),
        expressive=True,
    )


def quality() -> Models:
    """Deepgram + Gemini + Cartesia. About twice the cost of `budget`."""
    return Models(
        stt=inference.STT(model="deepgram/nova-3", language="en"),
        llm=inference.LLM(model="google/gemini-3.1-flash-lite"),
        tts=inference.TTS(
            model="cartesia/sonic-3", voice="9626c31c-bec5-4cca-baa8-f8ba9e84c8bc"
        ),
        expressive=False,
    )


# Same Cartesia voice as `quality`; the Atlas fallback when ElevenLabs fails.
CARTESIA_MODEL = "cartesia/sonic-3"
CARTESIA_VOICE = "9626c31c-bec5-4cca-baa8-f8ba9e84c8bc"
# ElevenLabs premade voice "George": warm, low, unhurried. Override per env.
DEFAULT_ELEVEN_VOICE = "JBFqnCBsd6RMkjVDRZzb"


def _elevenlabs_tts() -> tts.TTS | None:
    """ElevenLabs Flash v2.5 (lowest-latency family), or None without a key.

    Params checked against the installed plugin
    (livekit/plugins/elevenlabs/tts.py TTS.__init__ and VoiceSettings).
    Streaming is the plugin's default (multi-stream-input WebSocket); the
    session uses stream() automatically. auto_mode stays unset (True), which
    flushes per sentence for the lowest latency on short turns.
    """
    try:
        from livekit.plugins import elevenlabs

        return elevenlabs.TTS(
            model="eleven_flash_v2_5",
            voice_id=(
                os.getenv("ELEVEN_VOICE_ID")
                or os.getenv("ATLAS_ELEVEN_VOICE_ID")
                or DEFAULT_ELEVEN_VOICE
            ),
            voice_settings=elevenlabs.VoiceSettings(
                stability=0.65,  # steadier, calmer delivery
                similarity_boost=0.75,
                style=0.0,
                speed=0.95,  # a touch slower for a frightened listener
                use_speaker_boost=False,
            ),
            language="en",
            encoding="pcm_16000",  # phone audio is narrowband; skips MP3 decode
            enable_logging=os.getenv("ELEVEN_ZERO_RETENTION", "0") != "1",
            # api_key: read from ELEVEN_API_KEY by the plugin
        )
    except ValueError:
        # plugin raises ValueError when ELEVEN_API_KEY is missing
        logger.warning("elevenlabs_disabled: ELEVEN_API_KEY not set, using Cartesia")
        return None


def safety() -> Models:
    """Atlas: Flux STT, Gemini Flash, ElevenLabs with a Cartesia fallback."""
    backup = inference.TTS(model=CARTESIA_MODEL, voice=CARTESIA_VOICE)
    primary = _elevenlabs_tts()
    voice: tts.TTS = (
        # On a 401/429/quota/stall from ElevenLabs, fail over to Cartesia for
        # the rest of the call. The voice changes once; better than silence.
        tts.FallbackAdapter([primary, backup], max_retry_per_tts=1)
        if primary is not None
        else backup
    )
    return Models(
        stt=inference.STT(model="deepgram/flux-general-en", language="en"),
        llm=inference.LLM(model="google/gemini-3.1-flash"),
        tts=voice,
        expressive=False,  # calm, not dramatic; expressive tags off
    )


PRESETS = {"budget": budget, "quality": quality, "safety": safety}


def load_models() -> Models:
    name = os.getenv("MODEL_PRESET", "budget").strip().lower()
    if name not in PRESETS:
        raise ValueError(
            f"Unknown MODEL_PRESET '{name}'. Choose one of: {', '.join(PRESETS)}"
        )
    return PRESETS[name]()
