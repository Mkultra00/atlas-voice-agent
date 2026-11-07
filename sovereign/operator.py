"""Operator bridge: exposes Kov (the machine's operator agent) to the voice loop.

The triage LLM may request ONE operator capability per reply by ending its text with:

    <<OPERATOR>>{"tool":"calendar_today","args":{},"confirm":false}<</OPERATOR>>

server.py strips the directive from the spoken reply, executes the capability,
and feeds the observation back for a second LLM pass. Safety posture:
- read-only lanes (calendar/gmail/inbox) run directly via the claude CLI with
  per-lane --allowedTools (the calendar lane cannot touch gmail and vice versa);
- mutating lane (mail_send) requires confirm:true — the voice agent must ask
  the user and repeat the request with confirmation on the next turn;
- unknown tools, disabled lanes and oversized outputs are rejected here.
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess  # noqa: F401  (kept for parity with _run's exec model)
from pathlib import Path
from typing import Any, Awaitable, Callable

CAL_TOOLS = "mcp__claude_ai_Google_Calendar__list_events"
MAIL_TOOLS = "mcp__claude_ai_Gmail__search_threads,mcp__claude_ai_Gmail__get_message"

_DIRECTIVE_RE = re.compile(
    r"<<OPERATOR>>\s*(\{.*?\})\s*<</\s*OPERATOR\s*>>?", re.DOTALL | re.IGNORECASE)


def parse_directive(text: str) -> tuple[str, dict[str, Any] | None]:
    """Split a reply into (spoken_text, directive|None). None if valid no-op."""
    m = _DIRECTIVE_RE.search(text)
    if not m:
        return text, None
    spoken = _DIRECTIVE_RE.sub("", text).strip()
    try:
        d = json.loads(m.group(1))
    except json.JSONDecodeError:
        return spoken or "Requesting operator help — the directive was malformed.", None
    if not isinstance(d, dict) or not isinstance(d.get("tool"), str):
        return spoken, None
    return spoken, d


def _claude_bin(cfg) -> str:
    return getattr(cfg, "operator_claude_bin", "") or str(Path.home() / ".local/bin/claude")


async def _run(cmd: list[str], timeout: float, cwd: str | None = None) -> str:
    p = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=cwd
    )
    try:
        out, err = await asyncio.wait_for(p.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        p.terminate()
        return f"[operator: timed out after {timeout:.0f}s]"
    out_s = (out or b"").decode("utf-8", "replace").strip()
    err_s = (err or b"").decode("utf-8", "replace").strip()
    return out_s or err_s or "[operator: empty output]"


def _est_stamp(cfg) -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo  # stdlib zoneinfo, not datetime.ZoneInfo
    now = datetime.now(ZoneInfo("America/New_York"))
    return now.strftime("%a %b %-d %Y")


async def calendar_today(cfg, args: dict, confirm: bool) -> str:
    prompt = (f"List Frank's Google Calendar events for today, {_est_stamp(cfg)}, "
              "00:00 to 23:59 America/New_York. If none, say none.")
    out = await _run([_claude_bin(cfg), "-p", prompt, "--allowedTools", CAL_TOOLS], 150.0)
    return f"Calendar observation ({_est_stamp(cfg)}):\n{out[:2000]}"


async def gmail_search(cfg, args: dict, confirm: bool) -> str:
    q = str(args.get("query", "")).strip() or "recent unread messages"
    prompt = (f"Search Gmail PRIMARY inbox only for: {q}. Report sender, subject and date "
              "of the top matches. Do not open anything else.")
    out = await _run([_claude_bin(cfg), "-p", prompt, "--allowedTools", MAIL_TOOLS], 150.0)
    return f"Gmail observation (query: {q}):\n{out[:2000]}"


async def inbox_check(cfg, args: dict, confirm: bool) -> str:
    inbox = str(args.get("inbox", "kovyu")).replace("@agentmail.to", "")
    if inbox not in ("kovyu", "chanyu"):
        return "[operator: inbox must be kovyu or chanyu]"
    out = await _run([shutil.which("python3") or "python3", str(Path.home() / ".agentmail/am.py"),
                      "threads", inbox], 45.0)
    return f"AgentMail inbox {inbox}@ threads:\n{out[:1500]}"


async def mail_send(cfg, args: dict, confirm: bool) -> str:
    to = str(args.get("to", "")).strip()
    subject = str(args.get("subject", "Message from Atlas voice")).strip()
    text = str(args.get("text", "")).strip()
    if not to or not text:
        return "[operator: mail_send needs to + text]"
    if not confirm:
        return ("[operator: SEND BLOCKED — ask the user to confirm, then repeat the same "
                "directive with confirm:true]")
    out = await _run([shutil.which("python3") or "python3", str(Path.home() / ".agentmail/am.py"),
                      "send", to, subject, text], 60.0)
    if out.startswith("200"):
        return f"[operator: email delivered to {to}]"
    return f"Send result from AgentMail (status line below):\n{out[:500]}"


async def machine_status(cfg, args: dict, confirm: bool) -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo  # stdlib zoneinfo, not datetime.ZoneInfo
    now = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M EST")
    batt = await _run(["pmset", "-g", "batt"], 10.0)
    disk = await _run(["df", "-h", "/"], 10.0)
    m = re.search(r"(\d+%)", batt)
    free = next((l for l in disk.splitlines() if l.strip().endswith("/")), "")
    parts = [f"Local machine {now}"]
    if m:
        parts.append(f"battery {m.group(1)}")
    if free:
        parts.append("root fs: " + " ".join(free.split()[-3:]))
    ollama_ok = (await _run(["/usr/bin/nc", "-z", "127.0.0.1", "11434"], 5.0)) != "[operator: empty output]"
    parts.append("ollama lane " + ("up" if ollama_ok else "down"))
    return "\n".join(parts)


REGISTRY: dict[str, dict[str, Any]] = {
    "calendar_today": {"handler": calendar_today, "mutates": False,
                       "desc": "read Frank's Google Calendar for today via the operator's connector"},
    "gmail_search": {"handler": gmail_search, "mutates": False,
                     "desc": "search the operator's Gmail PRIMARY inbox (read-only)"},
    "inbox_check": {"handler": inbox_check, "mutates": False,
                    "desc": "list Kov's own agentmail inboxes (kovyu/chanyu)"},
    "mail_send": {"handler": mail_send, "mutates": True,
                  "desc": "send an email from Kov's kovyu@agentmail.to — REQUIRES user confirmation"},
    "machine_status": {"handler": machine_status, "mutates": False,
                       "desc": "local machine vitals: time, battery, disk, ollama lane"},
}


async def execute(cfg, tool: str, args: dict[str, Any], confirm: bool) -> str:
    if not getattr(cfg, "operator_enabled", True):
        return "[operator: bridge disabled]"
    # tolerate LLM tool-id typos: "gmailsearch" → "gmail_search"
    flat = re.sub(r"[^a-z0-9]", "", str(tool).lower())
    if tool not in REGISTRY:
        for name in REGISTRY:
            if flat == re.sub(r"[^a-z0-9]", "", name):
                tool = name
                break
    allow = [s.strip() for s in getattr(cfg, "operator_allow", "").split(",") if s.strip()]
    if tool not in REGISTRY or (allow and tool not in allow):
        return (f"[operator: no such capability '{tool}' — known tools: "
                f"{', '.join(sorted(REGISTRY))}]")
    try:
        result = await REGISTRY[tool]["handler"](cfg, args or {}, bool(confirm))
    except Exception as e:  # operator failures must never crash the voice loop
        result = f"[operator: {tool} failed: {type(e).__name__} {e}]"
    if len(result) > 4000:
        result = result[:4000] + "\n[operator: output truncated]"
    return result


def describe() -> list[dict[str, Any]]:
    return [{"tool": t, **{k: v[k] for k in ("mutates", "desc")}} for t, v in REGISTRY.items()]
