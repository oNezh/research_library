"""Local literature index: SQLite + FTS (`index/library.db`).

Use ``from research_library.library import db`` then ``db.connect()``, etc.

Unified reference pipeline: ``reference_parse`` → ``reference_acquire`` / export; ``reference_ingest`` for upserts.
"""

from __future__ import annotations

import importlib

from . import db
from . import reference_acquire
from . import reference_ingest
from . import reference_parse
from . import search

__all__ = [
    "db",
    "local_embedding",
    "reference_parse",
    "reference_acquire",
    "reference_ingest",
    "search",
    "semantic",
    "semantic_compare",
    "topic_dossier",
    "report",
]


_LAZY_SUBMODULES = frozenset(
    {"local_embedding", "topic_dossier", "semantic", "semantic_compare", "report"}
)


def __getattr__(name: str):
    if name in _LAZY_SUBMODULES:
        return importlib.import_module(f"{__name__}.{name}")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
