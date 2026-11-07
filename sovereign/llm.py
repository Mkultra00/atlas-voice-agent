"""OpenAI-compatible chat client — one seam for every sovereign LLM lane."""
from __future__ import annotations

from typing import Any

import httpx

from .config import SovereignConfig

SYSTEM_PROMPT = (
    "You are Atlas, a crisis-triage voice agent for travelers. "
    "Stay calm, be concrete: give next actions in priority order. "
    "One clarifying question at a time, never more. "
    "Escalate to the human duty desk for anything beyond first-response triage. "
    "Speak plainly; the reply is read aloud. No markdown, no lists longer than 3 items."
)


async def chat(messages: list[dict[str, Any]], cfg: SovereignConfig, timeout: float = 90.0) -> str:
    models = [cfg.llm_model] + [m for m in cfg.llm_fallbacks.split(",") if m.strip() and m.strip() != cfg.llm_model]
    last_err = "empty response from all models"
    async with httpx.AsyncClient(timeout=timeout) as client:
        for model in models:
            payload = {"model": model, "messages": messages, "temperature": 0.3}
            headers = {}
            if cfg.llm_api_key:
                headers["Authorization"] = f"Bearer {cfg.llm_api_key}"
            r = await client.post(f"{cfg.llm_base_url}/chat/completions", json=payload, headers=headers)
            r.raise_for_status()
            data = r.json()
            content = data["choices"][0]["message"]["content"].strip() if r.status_code == 200 else ""
            if content:
                return content
            last_err = f"empty response from {model}"
    raise RuntimeError(last_err)


def build_messages(history: list[dict[str, str]], context_blocks: list[str]) -> list[dict[str, str]]:
    system = SYSTEM_PROMPT
    for block in context_blocks:
        if block:
            system += "\n\n" + block
    return [{"role": "system", "content": system}, *history]
