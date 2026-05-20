"""Data paths and environment loading."""

from __future__ import annotations

import os
from pathlib import Path

_DEFAULT_DATA_ROOT = Path("/Users/zenn/program-data/research_library")


def find_repo_root() -> Path | None:
    """Directory containing pyproject.toml and src/research_library, or None if not in-tree."""
    here = Path(__file__).resolve()
    for d in here.parents:
        if (d / "pyproject.toml").is_file() and (d / "src" / "research_library").is_dir():
            return d
    return None


def get_data_dir() -> Path:
    root = os.environ.get("RESEARCH_LIBRARY_DATA_DIR", "").strip()
    p = Path(root).expanduser() if root else _DEFAULT_DATA_ROOT
    p.mkdir(parents=True, exist_ok=True)
    return p.resolve()


def get_index_dir() -> Path:
    d = get_data_dir() / "index"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_chroma_semantic_dir() -> Path:
    """Persistent Chroma directory for paper chunk embeddings."""
    d = get_index_dir() / "chroma_semantic"
    d.mkdir(parents=True, exist_ok=True)
    return d.resolve()


def get_semantic_backend() -> str:
    """Chunk retrieval: ``vector`` (Chroma + embeddings API) or ``fts`` (SQLite FTS5 on chunks, no embeddings)."""
    load_env()
    b = (os.environ.get("RESEARCH_SEMANTIC_BACKEND") or "vector").strip().lower()
    if b in ("fts", "bm25", "keyword", "text", "traditional"):
        return "fts"
    return "vector"


def effective_semantic_backend(override: str | None = None) -> str:
    """Resolve backend: explicit ``override`` wins; else :func:`get_semantic_backend`."""
    load_env()
    o = (override or "").strip().lower()
    if o in ("fts", "bm25", "keyword", "text", "traditional"):
        return "fts"
    if o in ("vector", "embedding", "embeddings", "chroma"):
        return "vector"
    return get_semantic_backend()


def get_pdfs_dir() -> Path:
    d = get_data_dir() / "pdfs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_env() -> None:
    """Load variables: project .env first, then current working directory .env (both optional).

    Neither overrides keys already set in the process environment.
    """
    try:
        from dotenv import load_dotenv

        root = find_repo_root()
        if root is not None:
            load_dotenv(root / ".env", override=False)
        load_dotenv(override=False)
    except ImportError:
        pass


def load_ads_token() -> str:
    load_env()
    return (os.environ.get("ADS_API_TOKEN") or "").strip()
