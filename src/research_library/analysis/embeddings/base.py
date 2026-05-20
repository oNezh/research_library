"""Embedding provider protocol (swappable: MiniMax, OpenAI-compatible, etc.)."""

from __future__ import annotations

from typing import List, Protocol, runtime_checkable


@runtime_checkable
class EmbeddingClient(Protocol):
    """Batch text → embedding vectors (same length list)."""

    embedding_dim: int

    def embed_texts(
        self,
        texts: List[str],
        *,
        model: str | None = None,
        for_query: bool = False,
    ) -> List[List[float]]:
        ...
