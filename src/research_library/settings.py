"""Centralized settings: read every ``RESEARCH_*`` / ``QF_LLM_*`` / ``MINIMAX_*``
environment variable in one place so callers don't have to remember names or
default values.

We use a plain dataclass (no pydantic dependency required) but still expose a
single ``get_settings()`` accessor that is cached for the process lifetime so
the same values are observed everywhere unless ``reload_settings()`` is called.

The fields here are intentionally a flat mirror of the env-var sprawl from
``.env.example``; individual modules should keep accepting their own kwargs
for backwards compatibility, and only fall back to this object when no
explicit value was provided. This keeps the existing CLI / MCP surfaces stable
while giving us one definition of "what env vars exist".
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Optional

from research_library.config import load_env


def _int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _str(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


@dataclass(frozen=True)
class Settings:
    # --- ADS / arXiv ----------------------------------------------------------
    ads_api_token: str = field(default_factory=lambda: _str("ADS_API_TOKEN"))

    # --- LLM ------------------------------------------------------------------
    llm_provider: str = field(
        default_factory=lambda: _str("RESEARCH_LLM_PROVIDER")
        or _str("QF_LLM_PROVIDER")
    )
    llm_timeout: int = field(default_factory=lambda: _int("RESEARCH_LLM_TIMEOUT", 600))
    llm_max_completion_tokens: int = field(
        default_factory=lambda: _int("RESEARCH_LLM_MAX_COMPLETION_TOKENS", 32768)
    )
    llm_log_usage: bool = field(default_factory=lambda: _bool("RESEARCH_LLM_LOG_USAGE", False))

    # --- HTTP retries ---------------------------------------------------------
    http_retry_attempts: int = field(default_factory=lambda: _int("RESEARCH_HTTP_RETRY_ATTEMPTS", 3))
    http_retry_base_delay: float = field(
        default_factory=lambda: _float("RESEARCH_HTTP_RETRY_BASE_DELAY", 0.8)
    )

    # --- Semantic / retrieval -------------------------------------------------
    semantic_backend: str = field(default_factory=lambda: _str("RESEARCH_SEMANTIC_BACKEND", "vector"))
    semantic_chunk_size: int = field(default_factory=lambda: _int("RESEARCH_SEMANTIC_CHUNK_SIZE", 1200))
    semantic_chunk_overlap: int = field(default_factory=lambda: _int("RESEARCH_SEMANTIC_CHUNK_OVERLAP", 200))
    semantic_chunk_boundary: bool = field(default_factory=lambda: _bool("RESEARCH_SEMANTIC_CHUNK_BOUNDARY", True))
    semantic_embed_batch: int = field(default_factory=lambda: _int("RESEARCH_SEMANTIC_EMBED_BATCH", 16))
    semantic_hybrid: bool = field(default_factory=lambda: _bool("RESEARCH_SEMANTIC_HYBRID", True))
    semantic_mmr: bool = field(default_factory=lambda: _bool("RESEARCH_SEMANTIC_MMR", True))
    grounding_markers: bool = field(default_factory=lambda: _bool("RESEARCH_GROUNDING_MARKERS", True))

    # --- Embedding provider ---------------------------------------------------
    embedding_provider: str = field(default_factory=lambda: _str("RESEARCH_EMBEDDING_PROVIDER"))
    embedding_model: str = field(default_factory=lambda: _str("RESEARCH_EMBEDDING_MODEL"))
    embedding_minimax_type: str = field(default_factory=lambda: _str("RESEARCH_EMBEDDING_MINIMAX_TYPE"))
    embedding_query_input_type: str = field(default_factory=lambda: _str("RESEARCH_EMBEDDING_QUERY_INPUT_TYPE"))
    embedding_doc_input_type: str = field(default_factory=lambda: _str("RESEARCH_EMBEDDING_DOC_INPUT_TYPE"))

    # --- Local Sentence-Transformer ------------------------------------------
    local_embedding_home: str = field(default_factory=lambda: _str("RESEARCH_LOCAL_EMBEDDING_HOME"))
    local_embedding_hf_home: str = field(default_factory=lambda: _str("RESEARCH_LOCAL_EMBEDDING_HF_HOME"))
    local_embedding_model: str = field(default_factory=lambda: _str("RESEARCH_LOCAL_EMBEDDING_MODEL"))
    local_embedding_device: str = field(default_factory=lambda: _str("RESEARCH_LOCAL_EMBEDDING_DEVICE"))
    local_embedding_hf_offline: bool = field(default_factory=lambda: _bool("RESEARCH_LOCAL_EMBEDDING_HF_OFFLINE", False))
    local_embedding_normalize: bool = field(default_factory=lambda: _bool("RESEARCH_LOCAL_EMBEDDING_NORMALIZE", False))

    # --- PDF analyze ----------------------------------------------------------
    pdf_analyze_max_chars: int = field(default_factory=lambda: _int("RESEARCH_PDF_ANALYZE_MAX_CHARS", 100000))

    # --- PDF reference chain -------------------------------------------------
    chain_library_refs: bool = field(default_factory=lambda: _bool("RESEARCH_PDF_CHAIN_LIBRARY_REFS", True))
    chain_auto_ingest: bool = field(default_factory=lambda: _bool("RESEARCH_PDF_CHAIN_AUTO_INGEST", True))
    chain_max_follow_per_hop: int = field(default_factory=lambda: _int("RESEARCH_PDF_CHAIN_MAX_FOLLOW_PER_HOP", 0))
    chain_max_step_tokens: int = field(default_factory=lambda: _int("RESEARCH_PDF_CHAIN_MAX_STEP_TOKENS", 0))
    chain_max_synth_tokens: int = field(default_factory=lambda: _int("RESEARCH_PDF_CHAIN_MAX_SYNTH_TOKENS", 65536))
    chain_pass1_max_tokens: int = field(default_factory=lambda: _int("RESEARCH_PDF_CHAIN_STEP_PASS1_MAX_TOKENS", 16384))
    chain_total_token_budget: int = field(default_factory=lambda: _int("RESEARCH_PDF_CHAIN_TOTAL_TOKEN_BUDGET", 0))
    chain_acquire_workers: int = field(default_factory=lambda: _int("RESEARCH_PDF_CHAIN_ACQUIRE_WORKERS", 4))
    chain_sync_retry_attempts: int = field(default_factory=lambda: _int("RESEARCH_PDF_CHAIN_SYNC_RETRY_ATTEMPTS", 3))
    chain_sync_sleep_ms: int = field(default_factory=lambda: _int("RESEARCH_PDF_CHAIN_SYNC_SLEEP_MS", 500))
    chain_verify_excerpts: bool = field(default_factory=lambda: _bool("RESEARCH_PDF_CHAIN_VERIFY_EXCERPTS", True))
    chain_method_hints: bool = field(default_factory=lambda: _bool("RESEARCH_PDF_CHAIN_METHOD_HINTS", False))
    chain_tight: bool = field(default_factory=lambda: _bool("RESEARCH_PDF_CHAIN_TIGHT", False))
    chain_acquire_timeout: int = field(default_factory=lambda: _int("RESEARCH_PDF_ACQUIRE_TIMEOUT", 120))

    # --- Library ingest -------------------------------------------------------
    pdf_ingest_require_strong_id: bool = field(
        default_factory=lambda: _bool("RESEARCH_PDF_INGEST_REQUIRE_STRONG_ID", True)
    )
    pdf_ingest_sync_references: bool = field(
        default_factory=lambda: _bool("RESEARCH_PDF_INGEST_SYNC_REFERENCES", True)
    )

    # --- Data root ------------------------------------------------------------
    data_dir: str = field(default_factory=lambda: _str("RESEARCH_LIBRARY_DATA_DIR"))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    load_env()
    return Settings()


def reload_settings() -> Settings:
    """Drop the cached :class:`Settings` and rebuild from current ``os.environ``."""
    get_settings.cache_clear()
    return get_settings()


__all__ = ["Settings", "get_settings", "reload_settings"]
