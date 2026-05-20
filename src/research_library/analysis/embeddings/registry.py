"""Resolve ``EmbeddingClient`` from ``RESEARCH_EMBEDDING_PROVIDER`` (default: minimax)."""

from __future__ import annotations

import os

from research_library.analysis.llm.base import LLMError
from research_library.config import load_env

from .base import EmbeddingClient
from .local_sentence_transformer import LocalSentenceTransformerEmbeddings
from .minimax_embeddings import MiniMaxOpenAIEmbeddings
from .openai_compat import OpenAICompatibleEmbeddings


def get_embedding_client(provider: str | None = None) -> EmbeddingClient:
    load_env()
    p = (provider or os.environ.get("RESEARCH_EMBEDDING_PROVIDER") or "").strip().lower()
    if not p:
        qf = (os.environ.get("QF_LLM_PROVIDER") or "").strip().lower()
        if qf in ("openai", "openai_compat", "openai-compatible"):
            p = "openai_compat"
        else:
            p = "minimax"
    if p in ("minimax", "mini_max", "min_max"):
        return MiniMaxOpenAIEmbeddings.from_env()
    if p in ("openai_compat", "openai", "openai_compat_api"):
        return OpenAICompatibleEmbeddings.from_env()
    if p in (
        "local_sentence_transformer",
        "local_st",
        "sentence_transformers",
        "sentence_transformer",
        "qwen_local",
        "local",
    ):
        return LocalSentenceTransformerEmbeddings.from_env()
    raise LLMError(
        f"Unknown RESEARCH_EMBEDDING_PROVIDER: {p!r} "
        f"(supported: minimax, openai_compat, local_sentence_transformer)"
    )
