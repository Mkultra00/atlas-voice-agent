"""Shared fixtures: keep Atlas tests offline and out of the repo's files."""

import pytest

import safety_tools as st

ATLAS_ENV = (
    "ATLAS_OPS_WEBHOOK",
    "OPS_WEBHOOK_URL",
    "OPS_WEBHOOK_FORMAT",
    "OPS_INCLUDE_CALLBACK",
    "TAVILY_API_KEY",
    "ATLAS_DEMO_NEWS",
    "NOMINATIM_URL",
    "OVERPASS_URL",
    "OSRM_FOOT_URL",
    "OSRM_CAR_URL",
)


@pytest.fixture(autouse=True)
def _atlas_isolation(monkeypatch, tmp_path):
    for k in ATLAS_ENV:
        monkeypatch.delenv(k, raising=False)
    # undelivered alerts go to a temp file, never ./ops_log.jsonl
    monkeypatch.setenv("ATLAS_OPS_LOG", str(tmp_path / "ops_log.jsonl"))
    st._geo_cache.clear()
    st._last_nominatim = 0.0
    yield
    st._geo_cache.clear()
