"""Unified paper search: local library (identifiers + FTS) then ADS + arXiv."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict, List, Optional

from research_library.config import get_data_dir, load_env
from research_library.library import db as library_db


def _enrich_local_row(row: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(row)
    out["provenance"] = "library"
    rel = out.get("pdf_relpath")
    if rel:
        p = (get_data_dir() / str(rel).lstrip("/")).resolve()
        if p.is_file():
            out["pdf_abspath"] = str(p)
    return out


def _dedupe_add(
    bucket: List[Dict[str, Any]], seen: set[int], rows: List[Dict[str, Any]]
) -> None:
    for row in rows:
        pid = row.get("id")
        if pid is not None:
            ip = int(pid)
            if ip in seen:
                continue
            seen.add(ip)
        bucket.append(_enrich_local_row(row))


def local_search_papers(
    conn: Any,
    query: str,
    *,
    limit: int = 15,
) -> List[Dict[str, Any]]:
    """Identifiers (bibcode / arXiv) then title+abstract FTS."""
    from research_library.library.reference_parse import (
        _classify_catalog_line,
        _extract_catalog_identifiers,
    )

    library_db.ensure_schema(conn)
    raw = (query or "").strip()
    if not raw:
        return []
    cat = _classify_catalog_line(raw)
    bib, arx, doi = _extract_catalog_identifiers(raw, cat)
    seen: set[int] = set()
    out: List[Dict[str, Any]] = []

    if cat == "bibcode" and bib:
        _dedupe_add(out, seen, library_db.fetch_paper_dicts_by_bibcode(conn, bib))
    elif cat == "arxiv" and arx:
        _dedupe_add(out, seen, library_db.fetch_paper_dicts_by_arxiv_id(conn, arx))
    elif cat == "doi" and doi:
        qdoi = doi.replace("/", " ").replace(":", " ")
        for row in library_db.search_fts(conn, qdoi, limit=limit):
            _dedupe_add(out, seen, [row])

    if out:
        return out[:limit]

    if cat == "freeform":
        from research_library.lookup import parse_reference

        pr = parse_reference(raw)
        if pr.first_author and pr.year:
            _dedupe_add(
                out,
                seen,
                library_db.fetch_paper_dicts_by_author_year(
                    conn, pr.first_author, pr.year, limit=limit
                ),
            )
        if out:
            return out[:limit]

        bits = [pr.first_author, pr.year, pr.journal, pr.volume, pr.page]
        sub = " ".join(str(b) for b in bits if b).strip()
        if sub:
            try:
                sub_rows = library_db.search_fts(conn, sub, limit=limit)
            except Exception:
                sub_rows = []
            _dedupe_add(out, seen, sub_rows)
            if out:
                return out[:limit]

    try:
        fts_rows = library_db.search_fts(conn, raw[:800], limit=limit)
    except Exception:
        fts_rows = []
    _dedupe_add(out, seen, fts_rows)
    return out[:limit]


def search_papers(
    query: str,
    conn: Optional[Any] = None,
    *,
    limit_local: int = 15,
    limit_remote: int = 10,
    force_remote: bool = False,
    include_remote_when_local: bool = False,
) -> Dict[str, Any]:
    """
    Standard order: local identifiers + FTS → if empty (and not ``force_remote``),
    ADS then arXiv via :func:`research_library.lookup.search_candidates_auto`.
    """
    load_env()
    q = (query or "").strip()
    payload: Dict[str, Any] = {
        "query": q,
        "tier": "none",
        "local": [],
        "remote": [],
    }
    if not q:
        return payload

    c = conn if conn is not None else library_db.connect()
    local: List[Dict[str, Any]] = []
    if not force_remote:
        local = local_search_papers(c, q, limit=limit_local)

    payload["local"] = local
    if local and not include_remote_when_local:
        payload["tier"] = "local"
        return payload

    if local and include_remote_when_local:
        payload["tier"] = "local+remote"
    elif not local:
        payload["tier"] = "remote"

    from research_library.lookup import search_candidates_auto

    try:
        cands = search_candidates_auto(q, top_n=limit_remote)
        payload["remote"] = [asdict(x) for x in cands]
    except Exception as e:
        payload["remote_error"] = str(e)
    return payload
