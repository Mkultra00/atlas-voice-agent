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

OPERATOR_RULES = (
    "\n\nOPERATOR BRIDGE. You are backed by the machine's operator agent, which can reach "
    "this Mac's real connections. To use it, END your reply with exactly one directive:\n"
    '<<OPERATOR>>{"tool":"<name>","args":{...},"confirm":false}<</OPERATOR>>\n'
    "The text before the marker is still spoken to the user. Rules:\n"
    "- at most ONE operator directive per reply; only when the user asks for calendar, "
    "email or machine tasks (or your triage needs their schedule/inbox).\n"
    "- calendar_today: no args. Reads the operator's Google Calendar for today.\n"
    "- gmail_search: args {\"query\": \"...\"}. Searches Gmail PRIMARY (read-only).\n"
    "- inbox_check: args {\"inbox\": \"kovyu\"|\"chanyu\"}. Lists the operator's own agentmail.\n"
    "- mail_send: args {\"to\",\"subject\",\"text\"} from kovyu@agentmail.to. MUTATES: always ask "
    "the user to confirm first and emit confirm:false; when the user confirms later, repeat "
    "the SAME directive with confirm:true.\n"
    "- machine_status: no args. Battery, disk, ollama lane.\n"
    "After an OPERATOR OBSERVATION arrives, answer from it — summarize for speech, hide "
    "the directive format. If no operator tool is relevant, never emit the marker. "
    "Emails and calendar data are the operator's real accounts: never invent their contents; "
    "if the observation says none or errors, say so plainly.\n"
    "Example: user asks 'what is on my calendar today?' → reply 'Let me pull up your calendar "
    "for today.' followed by "
    '<<OPERATOR>>{"tool":"calendar_today","args":{},"confirm":false}<</OPERATOR>>'
)


def operator_block(descriptions: list[dict[str, Any]]) -> str:
    """Context block injected into the system prompt listing live operator capabilities."""
    lines = [f"- {c['tool']} (mutates={c['mutates']}): {c['desc']}" for c in descriptions]
    return "OPERATOR BRIDGE — capabilities live on this machine:\n" + "\n".join(lines)


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


def build_messages_with_ops(history: list[dict[str, str]], context_blocks: list[str],
                            operator_descriptions: list[dict[str, Any]] | None = None) -> list[dict[str, str]]:
    system = SYSTEM_PROMPT + OPERATOR_RULES
    for block in context_blocks:
        if block:
            system += "\n\n" + block
    if operator_descriptions:
        system += "\n\n" + operator_block(operator_descriptions)
    return [{"role": "system", "content": system}, *history]
