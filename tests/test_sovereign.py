"""Sovereign stack unit tests — offline, no network, no keys."""
from pathlib import Path

import asyncio

from sovereign.config import PRESETS, SovereignConfig
from sovereign.retrieval import retrieve, keyword_score
from sovereign import memory
from sovereign import llm as llm_mod
from sovereign import operator as op


def test_preset_resolution_defaults_to_ollama(monkeypatch):
    monkeypatch.delenv("ATLAS_LLM_PRESET", raising=False)
    cfg = SovereignConfig.resolve(env_file=Path("/nonexistent/.env.local"))
    assert cfg.llm_base_url == PRESETS["ollama"]["base_url"]
    assert cfg.llm_model == "glm-5.3-flash:cloud"


def test_env_override_wins(monkeypatch):
    monkeypatch.setenv("ATLAS_LLM_PRESET", "tinfoil")
    monkeypatch.setenv("ATLAS_LLM_BASE_URL", "https://custom.example/v1")
    monkeypatch.setenv("ATLAS_LLM_MODEL", "custom-model")
    cfg = SovereignConfig.resolve(env_file=Path("/nonexistent/.env.local"))
    assert cfg.llm_base_url == "https://custom.example/v1"
    assert cfg.llm_model == "custom-model"


def test_preset_tinfoil_pulls_api_key_from_env(monkeypatch):
    monkeypatch.setenv("ATLAS_LLM_PRESET", "tinfoil")
    monkeypatch.setenv("TINFOIL_API_KEY", "tk_test")
    cfg = SovereignConfig.resolve(env_file=Path("/nonexistent/.env.local"))
    assert cfg.llm_api_key == "tk_test"


def test_retrieval_hits_motorbike():
    hits = retrieve("I fell off a motorbike in Paris and my arm is bleeding", k=3)
    assert hits, "motorbike query must retrieve something"
    assert any("Motorbike" in h["title"] or "emergency" in h["body"] for h in hits[:2])


def test_retrieval_empty_on_gibberish():
    assert retrieve("zzz qqq xxx", k=3) == []


def test_keyword_score_basic():
    assert keyword_score("bleeding arm", "bleeding arm apply pressure") > 0
    assert keyword_score("zzz wwww", "bleeding arm") == 0


def test_memory_roundtrip(tmp_path):
    path = tmp_path / "incidents.jsonl"
    a = memory.remember("incident: motorbike crash in Paris", path=path)
    b = memory.remember("incident: lost passport in Rome", path=path)
    assert path.exists() and a["text"].startswith("incident:")
    hits = memory.recall("Rome passport", k=1, path=path)
    assert hits[0]["text"] == b["text"]
    assert memory.recall("zzz nothing", k=2, path=path)  # fallback returns freshest


def test_build_messages_injects_context():
    msgs = llm_mod.build_messages(
        [{"role": "user", "content": "help"}], ["Relevant procedures:\n- X: do Y"]
    )
    assert msgs[0]["role"] == "system"
    assert "do Y" in msgs[0]["content"]
    assert msgs[1]["role"] == "user"


# ---- operator bridge (offline) ----

def _cfg(**kw) -> SovereignConfig:
    import os
    saved = {k: os.environ.pop(k, None) for k in
             ("ATLAS_OPERATOR", "ATLAS_OPERATOR_ALLOW", "ATLAS_CLAUDE_BIN")}
    try:
        cfg = SovereignConfig.resolve(env_file=Path("/nonexistent/.env.local"))
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
    return cfg


def test_parse_directive_valid():
    spoken, d = op.parse_directive(
        'Checking the calendar now.'
        '<<OPERATOR>>{"tool":"calendar_today","args":{},"confirm":false}<</OPERATOR>>'
    )
    assert spoken == "Checking the calendar now."
    assert d and d["tool"] == "calendar_today" and d["confirm"] is False


def test_parse_directive_absent_and_malformed():
    t, d = op.parse_directive("Just answer plainly.")
    assert d is None and t == "Just answer plainly."
    t, d = op.parse_directive('a <<OPERATOR>>not json<</OPERATOR>> b')
    assert d is None  # no braces → marker not even matched, text passes through
    t, d = op.parse_directive('<<OPERATOR>>{"tool": <}<</OPERATOR>>')  # nothing spoken
    assert "malformed" in t.lower() and d is None


def test_registry_shapes():
    caps = op.describe()
    names = {c["tool"] for c in caps}
    assert {"calendar_today", "gmail_search", "inbox_check", "mail_send",
            "machine_status"} <= names
    by = {c["tool"]: c for c in caps}
    assert by["mail_send"]["mutates"] is True
    assert by["calendar_today"]["mutates"] is False


def test_execute_rejects_unknown_tool():
    cfg = _cfg()
    out = asyncio.run(op.execute(cfg, "rm_rf_home", {}, False))
    assert "no such capability" in out


def test_execute_honors_allowlist(monkeypatch):
    monkeypatch.setattr(SovereignConfig, "resolve", classmethod(
        lambda cls, env_file=None: type("C", (), {
            "operator_enabled": True, "operator_allow": "calendar_today",
            "operator_claude_bin": "/nonexistent/claude"})()))
    cfg = SovereignConfig.resolve()
    out = asyncio.run(op.execute(cfg, "mail_send", {}, False))
    assert "no such capability" in out
    out2 = asyncio.run(op.execute(cfg, "machine_status", {}, False))
    assert "no such capability" in out2


def test_execute_disabled_bridge():
    cfg = type("C", (), {"operator_enabled": False, "operator_allow": "",
                         "operator_claude_bin": ""})()
    out = asyncio.run(op.execute(cfg, "calendar_today", {}, False))
    assert "disabled" in out


def test_mail_send_blocked_without_confirm():
    cfg = _cfg()
    out = asyncio.run(op.execute(cfg, "mail_send",
                                 {"to": "chanyu@agentmail.to", "text": "hi"}, False))
    assert "SEND BLOCKED" in out and "confirm" in out


def test_operator_block_lists_capabilities():
    block = llm_mod.operator_block(op.describe())
    for tool in ("calendar_today", "mail_send", "machine_status"):
        assert tool in block


def test_build_messages_with_ops_injects_rules():
    msgs = llm_mod.build_messages_with_ops([], [], op.describe())
    assert "OPERATOR BRIDGE" in msgs[0]["content"]
    assert msgs[0]["content"].count("<<OPERATOR>>") == 2  # protocol template + worked example
