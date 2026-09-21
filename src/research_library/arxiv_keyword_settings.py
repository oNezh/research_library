"""Persisted arXiv keyword monitor phrases."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

from research_library.config import get_data_dir

# Default phrases (English + Chinese); display label defaults to the phrase itself.
_DEFAULT_PHRASES: List[str] = [
    "globular cluster",
    "dwarf galaxy",
    "dwarf galaxies",
    "stellar stream",
    "near-field cosmology",
    "galactic archaeology",
    "open cluster",
    "stellar population",
    "球状星团",
    "矮星系",
    "星流",
    "近场宇宙学",
    "星系考古学",
    "疏散星团",
    "星族",
]

_LEGACY_LABELS: Dict[str, str] = {
    "globular cluster": "球状星团",
    "dwarf galaxy": "矮星系",
    "dwarf galaxies": "矮星系",
    "stellar stream": "星流",
    "near-field cosmology": "近场宇宙学",
    "galactic archaeology": "星系考古学",
    "open cluster": "疏散星团",
    "stellar population": "星族",
    "球状星团": "球状星团",
    "矮星系": "矮星系",
    "星流": "星流",
    "近场宇宙学": "近场宇宙学",
    "星系考古学": "星系考古学",
    "疏散星团": "疏散星团",
    "星族": "星族",
}


def keywords_path() -> Path:
    return get_data_dir() / "arxiv_keywords.json"


def default_phrases() -> List[str]:
    return list(_DEFAULT_PHRASES)


def load_phrases() -> List[str]:
    path = keywords_path()
    if not path.is_file():
        return default_phrases()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default_phrases()
    if isinstance(raw, dict) and isinstance(raw.get("phrases"), list):
        items = [str(p).strip() for p in raw["phrases"] if str(p).strip()]
        return items or default_phrases()
    if isinstance(raw, list):
        items = [str(p).strip() for p in raw if str(p).strip()]
        return items or default_phrases()
    return default_phrases()


def save_phrases(phrases: List[str]) -> List[str]:
    cleaned: List[str] = []
    seen: set[str] = set()
    for p in phrases:
        s = str(p).strip()
        if not s:
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(s)
    if not cleaned:
        cleaned = default_phrases()
    path = keywords_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"phrases": cleaned}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return cleaned


def phrases_to_text(phrases: List[str] | None = None) -> str:
    return "\n".join(phrases if phrases is not None else load_phrases())


def text_to_phrases(text: str) -> List[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def keyword_map() -> Dict[str, str]:
    """Map match phrase -> display label for arxiv_keywords.match_keywords."""
    out: Dict[str, str] = {}
    for p in load_phrases():
        out[p] = _LEGACY_LABELS.get(p, p)
    return out
