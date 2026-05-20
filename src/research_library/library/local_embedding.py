"""Local embedding models (SentenceTransformers / Qwen3-Embedding).

Configure via ``.env`` (see ``.env.example``):

- ``RESEARCH_EMBEDDING_PROVIDER=local_sentence_transformer``
- ``RESEARCH_LOCAL_EMBEDDING_HOME`` — root of your ``qwen`` checkout (contains ``.cache/huggingface``)
- ``RESEARCH_SEMANTIC_BACKEND=vector`` and ``pip install -e ".[semantic,semantic-local]"``

Implementation lives in :mod:`research_library.analysis.embeddings.local_sentence_transformer`.
"""

from __future__ import annotations

from research_library.analysis.embeddings.local_sentence_transformer import (
    LocalSentenceTransformerEmbeddings,
)

__all__ = ["LocalSentenceTransformerEmbeddings"]
