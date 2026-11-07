"""Env-driven config. Every provider is swappable without code changes.

Env vars (win over .env.local):
  ATLAS_LLM_BASE_URL, ATLAS_LLM_API_KEY, ATLAS_LLM_MODEL
  ATLAS_LLM_PRESET = ollama | tinfoil | darkbloom
  SOVEREIGN_VOICE_ID
Loaded from repo .env.local: ELEVEN_API_KEY, ATLAS_OPS_WEBHOOK.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Preset base URLs. tinfoil/darkbloom exact endpoint docs to verify at wiring
# time; env override always wins.
PRESETS: dict[str, dict[str, str]] = {
    "ollama": {"base_url": "http://127.0.0.1:11434/v1", "api_key": "", "model": "glm-5.3-flash:cloud",
               "fallbacks": "qwen3:8b"},  # glm-cloud chokes (empty) on KB-context triage prompts
    # TODO(verify): confirm tinfoil.sh OpenAI-compat base URL when key arrives.
    "tinfoil": {"base_url": "https://api.tinfoil.sh/v1", "api_key": "env:TINFOIL_API_KEY", "model": "tinfoil-model"},
    # TODO(verify): confirm darkbloom OpenAI-compat base URL when key arrives.
    "darkbloom": {"base_url": "https://api.darkbloom.dev/v1", "api_key": "env:DARKBLOOM_API_KEY", "model": "darkbloom-model"},
}


def load_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        out[key.strip()] = val.strip().strip('"').strip("'")
    return out


@dataclass
class SovereignConfig:
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_fallbacks: str
    eleven_api_key: str
    ops_webhook: str
    voice_id: str

    @classmethod
    def resolve(cls, env_file: Path | None = None) -> "SovereignConfig":
        file_env = load_env_file(env_file or REPO_ROOT / ".env.local")
        env = {**file_env, **{k: v for k, v in os.environ.items() if v}}

        preset_name = env.get("ATLAS_LLM_PRESET", "ollama")
        preset = PRESETS.get(preset_name, PRESETS["ollama"])

        def pick(key: str, preset_key: str) -> str:
            if env.get(key):
                return env[key]
            val = preset.get(preset_key, "")
            if val.startswith("env:"):
                return env.get(val[4:], "")
            return val

        return cls(
            llm_base_url=pick("ATLAS_LLM_BASE_URL", "base_url").rstrip("/"),
            llm_api_key=pick("ATLAS_LLM_API_KEY", "api_key"),
            llm_model=pick("ATLAS_LLM_MODEL", "model"),
            llm_fallbacks=env.get("ATLAS_LLM_FALLBACKS", preset.get("fallbacks", "")),
            eleven_api_key=env.get("ELEVEN_API_KEY", ""),
            ops_webhook=env.get("ATLAS_OPS_WEBHOOK", "http://127.0.0.1:8787/alert"),
            voice_id=env.get("SOVEREIGN_VOICE_ID", "21m00Tcm4TlvDq8ikWAM"),
        )
