"""Atlas sovereign stack — LiveKit-free voice pipeline.

Pipeline: browser mic -> POST /api/turn -> STT (scribe_v1) -> guarded triage LLM
-> TTS (multilingual_v2) -> mp3 back. LLM slot is env-swappable:
ollama (local) | tinfoil (private enclave) | darkbloom (people-powered Mac grid).
"""
