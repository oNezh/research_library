"""Thin service layer shared by the CLI and the MCP server.

Background: previously the MCP server invoked CLI ``main()`` functions while
temporarily replacing ``sys.stdout`` / ``sys.stderr`` with ``StringIO`` to
capture their output. That breaks under concurrent MCP tool calls (stdout is
process-global) and silently re-parses formatted strings.

The functions in this module return **structured Python objects** so MCP can
hand them directly to ``json.dumps`` and CLI can render them however it
likes — with no global stream redirection. They are deliberately tiny
wrappers; the heavy lifting still lives in the existing modules.
"""

from __future__ import annotations

import os
from dataclasses import asdict
from typing import Any, Dict, List, Optional

from research_library.config import load_env
from research_library.log import get_logger

_log = get_logger(__name__)


def lookup_title_search(title: str, *, top_n: int = 8) -> Dict[str, Any]:
    """ADS + arXiv title search, returning a JSON-able dict.

    Mirrors ``research-lib lookup title --json`` but skips arg parsing and
    stdout writes.
    """
    load_env()
    from research_library import lookup as _l

    ads_enabled = bool(os.environ.get("ADS_API_TOKEN"))
    nt = _l.normalize_title(title)
    primary = _l.ads_search_title(nt) if ads_enabled else []
    fallback = _l.fallback_arxiv_for_title(nt)
    results = _l.merge_and_rank(primary, fallback, top_n=top_n)
    return {
        "mode": "title",
        "query": nt,
        "results": [asdict(r) for r in results],
        "ads_enabled": ads_enabled,
    }


def lookup_query_search(text: str, *, top_n: int = 8) -> Dict[str, Any]:
    load_env()
    from research_library import lookup as _l

    ads_enabled = bool(os.environ.get("ADS_API_TOKEN"))
    nt = _l.normalize_title(text)
    primary = _l.ads_search_query(nt) if ads_enabled else []
    fallback = _l.fallback_arxiv_for_query(nt)
    results = _l.merge_and_rank(primary, fallback, top_n=top_n)
    return {
        "mode": "query",
        "query": nt,
        "results": [asdict(r) for r in results],
        "ads_enabled": ads_enabled,
    }


def lookup_ref_search(text: str, *, top_n: int = 8) -> Dict[str, Any]:
    load_env()
    from research_library import lookup as _l

    ads_enabled = bool(os.environ.get("ADS_API_TOKEN"))
    parsed = _l.parse_reference(text)
    primary = _l.ads_search_reference(parsed) if ads_enabled else []
    fallback = _l.fallback_arxiv_for_reference(parsed)
    results = _l.merge_and_rank(primary, fallback, top_n=top_n)
    return {
        "mode": "ref",
        "query": parsed.raw,
        "parsed_reference": asdict(parsed),
        "results": [asdict(r) for r in results],
        "ads_enabled": ads_enabled,
    }


def lookup_bibtex(
    bibcode: Optional[str] = None,
    arxiv_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Resolve BibTeX for a bibcode (or arXiv id via ADS) without printing."""
    load_env()
    from research_library import lookup as _l

    ads_enabled = bool(os.environ.get("ADS_API_TOKEN"))
    bc = (bibcode or "").strip() or None
    arx = (arxiv_id or "").strip() or None
    if arx and not bc:
        if ads_enabled:
            try:
                r = _l.ads_query(f"arxiv:{arx}", rows=1)
                docs = r.get("response", {}).get("docs", [])
                if docs:
                    bc = docs[0].get("bibcode") or None
            except Exception as e:
                _log.warning("ads_resolve_arxiv_failed: %s", e)
        if not bc:
            return {
                "ok": False,
                "error": "bibtex_arxiv_lookup_requires_ads_token_or_bibcode",
            }
    if not bc:
        return {"ok": False, "error": "missing_bibcode_or_arxiv"}
    bibtex = _l.fetch_bibtex(bc)
    if bibtex:
        return {"ok": True, "bibcode": bc, "bibtex": bibtex}
    return {"ok": False, "error": "fetch_bibtex_failed", "bibcode": bc}


def chain_summary_from_state(state: Dict[str, Any]) -> Dict[str, Any]:
    """Tiny helper for MCP/CLI to surface a compact summary from a chain state."""
    trace = state.get("trace") or []
    return {
        "nodes": len(trace),
        "unresolved": sum(1 for t in trace if isinstance(t, dict) and t.get("unresolved")),
        "with_excerpts": sum(
            1 for t in trace if isinstance(t, dict) and (t.get("excerpts") or [])
        ),
        "budget_exhausted": bool(state.get("budget_exhausted")),
        "llm_usage_totals": state.get("llm_usage_totals") or {},
        "session_dir": state.get("session_dir"),
        "library_ingested_ok": state.get("library_ingested_ok"),
    }


def library_search(query: str, *, limit: int = 20) -> Dict[str, Any]:
    """FTS + optional remote search via :func:`research_library.library.search`."""
    from research_library.library import db as _db
    from research_library.library import search as _search

    conn = _db.connect()
    try:
        out = _search.search_papers(conn, query, limit=limit)
        if isinstance(out, list):
            return {"results": out, "count": len(out)}
        return out
    finally:
        try:
            conn.close()
        except Exception:
            pass


def library_stats() -> Dict[str, Any]:
    from research_library.library import db as _db

    conn = _db.connect()
    try:
        return _db.stats(conn)
    finally:
        try:
            conn.close()
        except Exception:
            pass


__all__ = [
    "lookup_title_search",
    "lookup_query_search",
    "lookup_ref_search",
    "lookup_bibtex",
    "library_search",
    "library_stats",
    "chain_summary_from_state",
]
