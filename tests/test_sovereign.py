"""Sovereign stack unit tests — offline, no network, no keys."""
from pathlib import Path

from sovereign.config import PRESETS, SovereignConfig
from sovereign.retrieval import retrieve, keyword_score
from sovereign import memory
from sovereign import llm as llm_mod


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
