"""Settings + services smoke tests (no network)."""

from __future__ import annotations

from research_library import services
from research_library.settings import get_settings, reload_settings


def test_settings_reads_env(monkeypatch):
    monkeypatch.setenv("RESEARCH_PDF_CHAIN_TOTAL_TOKEN_BUDGET", "12345")
    monkeypatch.setenv("RESEARCH_SEMANTIC_HYBRID", "0")
    s = reload_settings()
    assert s.chain_total_token_budget == 12345
    assert s.semantic_hybrid is False


def test_settings_cached(monkeypatch):
    monkeypatch.setenv("RESEARCH_PDF_CHAIN_ACQUIRE_WORKERS", "7")
    s1 = reload_settings()
    assert s1.chain_acquire_workers == 7
    monkeypatch.setenv("RESEARCH_PDF_CHAIN_ACQUIRE_WORKERS", "9")
    s2 = get_settings()
    # cache should still serve the older value until reload_settings
    assert s2.chain_acquire_workers == 7
    s3 = reload_settings()
    assert s3.chain_acquire_workers == 9


def test_chain_summary_from_state():
    state = {
        "trace": [
            {"depth": 0, "label": "root", "excerpts": ["x"]},
            {"depth": 1, "label": "ref[1]", "unresolved": True},
            {"depth": 1, "label": "ref[2]", "excerpts": []},
        ],
        "budget_exhausted": True,
        "llm_usage_totals": {"total_tokens": 42},
        "session_dir": "/tmp/x",
        "library_ingested_ok": 2,
    }
    out = services.chain_summary_from_state(state)
    assert out["nodes"] == 3
    assert out["unresolved"] == 1
    assert out["with_excerpts"] == 1
    assert out["budget_exhausted"] is True
    assert out["llm_usage_totals"] == {"total_tokens": 42}


def test_lookup_bibtex_missing_args_returns_error(monkeypatch):
    monkeypatch.delenv("ADS_API_TOKEN", raising=False)
    out = services.lookup_bibtex()
    assert out["ok"] is False
    assert out["error"]
