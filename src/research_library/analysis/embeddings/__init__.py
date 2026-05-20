"""Pluggable text embeddings for semantic index / search."""

from __future__ import annotations

from .base import EmbeddingClient
from .local_sentence_transformer import LocalSentenceTransformerEmbeddings
from .minimax_embeddings import MiniMaxOpenAIEmbeddings
from .openai_compat import OpenAICompatibleEmbeddings
from .registry import get_embedding_client

__all__ = [
    "EmbeddingClient",
    "LocalSentenceTransformerEmbeddings",
    "MiniMaxOpenAIEmbeddings",
    "OpenAICompatibleEmbeddings",
    "get_embedding_client",
]
