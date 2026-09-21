"""Refresh stored paper metadata from ADS (identifiers first, then fuzzy title/author)."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from research_library.library import db as library_db
from research_library.lookup import (
    ads_fetch_doc_by_bibcode,
    ads_query,
    ads_search_title,
    choose_identifier,
    similarity,
)
from research_library.library.reference_parse import strip_arxiv_version

METADATA_SYNCED_TAG = "metadata_synced"
_STRONG_METHODS = frozenset({"bibcode", "doi", "arxiv"})
_FUZZY_TITLE_MIN_SIM = 0.52
_FUZZY_AUTHOR_YEAR_MIN_SIM = 0.45


def _fetch_tags(conn: sqlite3.Connection, paper_id: int) -> List[Dict[str, str]]:
    rows = conn.execute(
        "SELECT tag_key, tag_value FROM paper_tags WHERE paper_id = ? ORDER BY tag_key",
        (paper_id,),
    ).fetchall()
    return [{"tag_key": r["tag_key"], "tag_value": r["tag_value"] or ""} for r in rows]


def mark_metadata_synced(
    conn: sqlite3.Connection,
    paper_id: int,
    match_method: str,
) -> None:
    now = library_db._now_iso()
    value = f"{match_method}:{now}"
    existing = conn.execute(
        "SELECT id FROM paper_tags WHERE paper_id = ? AND tag_key = ?",
        (paper_id, METADATA_SYNCED_TAG),
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE paper_tags SET tag_value = ?, source = 'metadata_refresh' WHERE id = ?",
            (value, existing["id"]),
        )
    else:
        conn.execute(
            "INSERT INTO paper_tags(paper_id, tag_key, tag_value, source, created_at) "
            "VALUES (?, ?, ?, 'metadata_refresh', ?)",
            (paper_id, METADATA_SYNCED_TAG, value, now),
        )


def count_pending_metadata_sync(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        f"""
        SELECT COUNT(*) FROM papers p
        WHERE NOT EXISTS (
            SELECT 1 FROM paper_tags t
            WHERE t.paper_id = p.id AND t.tag_key = ?
        )
        """,
        (METADATA_SYNCED_TAG,),
    ).fetchone()
    return int(row[0]) if row else 0


def list_pending_metadata_sync_ids(
    conn: sqlite3.Connection,
    *,
    limit: Optional[int] = None,
) -> List[int]:
    sql = """
        SELECT p.id FROM papers p
        WHERE NOT EXISTS (
            SELECT 1 FROM paper_tags t
            WHERE t.paper_id = p.id AND t.tag_key = ?
        )
        ORDER BY p.id
    """
    params: List[Any] = [METADATA_SYNCED_TAG]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()
    return [int(r[0]) for r in rows]


def refresh_pending_metadata(
    conn: sqlite3.Connection,
    *,
    delay: float = 1.0,
    limit: Optional[int] = None,
    report: Optional[Callable[[float, str], None]] = None,
) -> Dict[str, Any]:
    """Refresh metadata for papers missing ``metadata_synced`` tag."""
    if not (os.environ.get("ADS_API_TOKEN") or "").strip():
        return {"ok": False, "error": "ADS_API_TOKEN is not configured"}

    ids = list_pending_metadata_sync_ids(conn, limit=limit)
    total = len(ids)
    if total == 0:
        return {
            "ok": True,
            "total": 0,
            "synced": 0,
            "failed": 0,
            "stopped": False,
            "errors": [],
        }

    synced = 0
    failed = 0
    errors: List[Dict[str, Any]] = []
    consecutive_errors = 0
    stopped = False

    for i, pid in enumerate(ids):
        if report:
            report(100.0 * i / max(total, 1), f"同步元数据 {i + 1}/{total}")
        result = refresh_paper_metadata(conn, pid)
        if result.get("ok"):
            synced += 1
            consecutive_errors = 0
        else:
            failed += 1
            errors.append({"paper_id": pid, "error": result.get("error")})
            consecutive_errors += 1
            if consecutive_errors >= 5:
                stopped = True
                break
        if i + 1 < total and delay > 0:
            time.sleep(delay)

    if report:
        report(100.0, f"完成：{synced} 成功，{failed} 失败")

    return {
        "ok": True,
        "total": total,
        "synced": synced,
        "failed": failed,
        "stopped": stopped,
        "errors": errors[:20],
    }

def _ads_doc_fields(doc: Dict[str, Any]) -> Dict[str, Any]:
    title_l = doc.get("title")
    title = title_l[0] if isinstance(title_l, list) and title_l else (title_l or "") or ""
    abs_raw = doc.get("abstract")
    if isinstance(abs_raw, list):
        abstract = abs_raw[0] if abs_raw else ""
    else:
        abstract = (abs_raw or "") or ""
    _, arxiv_id = choose_identifier(doc.get("identifier") or [])
    year = doc.get("year")
    published = f"{year}-01-01" if year else None
    doi = None
    raw_doi = doc.get("doi")
    if isinstance(raw_doi, list) and raw_doi:
        doi = library_db._norm_doi_value(str(raw_doi[0]))
    elif isinstance(raw_doi, str):
        doi = library_db._norm_doi_value(raw_doi)
    return {
        "bibcode": (doc.get("bibcode") or "").strip() or None,
        "title": title,
        "abstract": abstract,
        "authors": list(doc.get("author") or []),
        "arxiv_id": arxiv_id,
        "published": published,
        "doi": doi,
    }


def _ads_query_doc(query: str) -> Optional[Dict[str, Any]]:
    if not (os.environ.get("ADS_API_TOKEN") or "").strip():
        return None
    try:
        result = ads_query(query, rows=1)
        docs = result.get("response", {}).get("docs", [])
        return docs[0] if docs else None
    except Exception:
        return None


def _fetch_full_doc(thin: Dict[str, Any]) -> Dict[str, Any]:
    bc = (thin.get("bibcode") or "").strip()
    if bc:
        full = ads_fetch_doc_by_bibcode(bc)
        if full:
            return full
    return thin


def _resolve_by_bibcode(bibcode: str) -> Optional[Dict[str, Any]]:
    bc = bibcode.strip()
    if not bc:
        return None
    doc = _ads_query_doc(f'bibcode:"{bc}"')
    return _fetch_full_doc(doc) if doc else None


def _resolve_by_doi(doi: str) -> Optional[Dict[str, Any]]:
    d = library_db._norm_doi_value(doi)
    if not d:
        return None
    doc = _ads_query_doc(f'doi:"{d}"')
    return _fetch_full_doc(doc) if doc else None


def _resolve_by_arxiv(arxiv_id: str) -> Optional[Dict[str, Any]]:
    ax = strip_arxiv_version(arxiv_id)
    if not ax:
        return None
    doc = _ads_query_doc(f"arxiv:{ax}")
    return _fetch_full_doc(doc) if doc else None


def _first_author_surname(author: str) -> str:
    a = (author or "").strip()
    if not a:
        return ""
    if "," in a:
        return a.split(",", 1)[0].strip()
    parts = a.split()
    return parts[-1] if parts else ""


def _published_year(published: Optional[str]) -> Optional[str]:
    p = (published or "").strip()
    if len(p) >= 4 and p[:4].isdigit():
        return p[:4]
    return None


def _pick_fuzzy_doc(
    docs: List[Dict[str, Any]],
    *,
    title: str,
    authors: List[str],
) -> Optional[Dict[str, Any]]:
    if not docs:
        return None
    ranked: List[Tuple[float, Dict[str, Any]]] = []
    local_title = (title or "").strip()
    want_author = _first_author_surname(authors[0]).lower() if authors else ""
    for doc in docs:
        fields = _ads_doc_fields(doc)
        cand_title = (fields.get("title") or "").strip()
        score = similarity(cand_title, local_title) if local_title else 0.0
        if want_author:
            ads_authors = [a.lower() for a in (fields.get("authors") or [])]
            if any(want_author in a for a in ads_authors):
                score += 0.12
        ranked.append((score, doc))
    ranked.sort(key=lambda x: x[0], reverse=True)
    best_score, best_doc = ranked[0]
    min_sim = _FUZZY_TITLE_MIN_SIM if local_title else _FUZZY_AUTHOR_YEAR_MIN_SIM
    if best_score < min_sim:
        return None
    return _fetch_full_doc(best_doc)


def _resolve_by_title(title: str, authors: List[str]) -> Optional[Dict[str, Any]]:
    t = (title or "").strip()
    if not t or not (os.environ.get("ADS_API_TOKEN") or "").strip():
        return None
    try:
        candidates = ads_search_title(t)
    except Exception:
        return None
    if not candidates:
        return None
    top = candidates[0]
    if top.bibcode:
        doc = ads_fetch_doc_by_bibcode(top.bibcode)
        if doc:
            fields = _ads_doc_fields(doc)
            if similarity(fields.get("title") or "", t) >= _FUZZY_TITLE_MIN_SIM:
                return doc
    docs: List[Dict[str, Any]] = []
    for c in candidates[:5]:
        if c.bibcode:
            doc = _ads_query_doc(f'bibcode:"{c.bibcode}"')
            if doc:
                docs.append(doc)
    return _pick_fuzzy_doc(docs, title=t, authors=authors)


def _resolve_by_author_year(
    authors: List[str],
    published: Optional[str],
    title: str,
) -> Optional[Dict[str, Any]]:
    if not (os.environ.get("ADS_API_TOKEN") or "").strip():
        return None
    year = _published_year(published)
    surname = _first_author_surname(authors[0]) if authors else ""
    if not year or not surname:
        return None
    doc = _ads_query_doc(f'author:"{surname}" AND year:{year}')
    if doc:
        picked = _pick_fuzzy_doc([doc], title=title, authors=authors)
        if picked:
            return picked
    try:
        result = ads_query(f'author:"{surname}" AND year:{year}', rows=8)
        docs = result.get("response", {}).get("docs", [])
    except Exception:
        return None
    return _pick_fuzzy_doc(docs, title=title, authors=authors)


def _resolve_ads_doc(
    *,
    bibcode: Optional[str],
    doi: Optional[str],
    arxiv_id: Optional[str],
    title: str,
    authors: List[str],
    published: Optional[str],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if bibcode and (doc := _resolve_by_bibcode(bibcode)):
        return doc, "bibcode"
    if doi and (doc := _resolve_by_doi(doi)):
        return doc, "doi"
    if arxiv_id and (doc := _resolve_by_arxiv(arxiv_id)):
        return doc, "arxiv"
    if title.strip() and (doc := _resolve_by_title(title, authors)):
        return doc, "title"
    if authors and (doc := _resolve_by_author_year(authors, published, title)):
        return doc, "author_year"
    return None, None


def _apply_fields(
    conn: sqlite3.Connection,
    paper_id: int,
    fields: Dict[str, Any],
    *,
    match_method: str,
) -> Dict[str, Any]:
    row = library_db.get_paper_row(conn, paper_id)
    if not row:
        raise ValueError("paper not found")

    strong = match_method in _STRONG_METHODS
    title = (fields.get("title") or "").strip() or row["title"]
    abstract = (fields.get("abstract") or "").strip()
    authors = fields.get("authors") or []
    if not strong:
        if not abstract:
            abstract = row.get("abstract") or ""
        if not authors:
            authors = row.get("authors") or []

    now = library_db._now_iso()
    conn.execute(
        """
        UPDATE papers SET
            arxiv_id = COALESCE(?, arxiv_id),
            bibcode = COALESCE(?, bibcode),
            doi = COALESCE(?, doi),
            title = ?,
            abstract = ?,
            authors_json = ?,
            published = COALESCE(?, published),
            updated_at = ?
        WHERE id = ?
        """,
        (
            fields.get("arxiv_id"),
            fields.get("bibcode"),
            fields.get("doi"),
            title,
            abstract,
            json.dumps(authors, ensure_ascii=False),
            fields.get("published"),
            now,
            paper_id,
        ),
    )
    conn.execute("DELETE FROM papers_fts WHERE paper_id = ?", (paper_id,))
    conn.execute(
        "INSERT INTO papers_fts(paper_id, title, abstract) VALUES (?, ?, ?)",
        (paper_id, title, abstract),
    )
    conn.commit()
    updated = library_db.get_paper_row(conn, paper_id)
    changed = {
        "title": title != (row.get("title") or ""),
        "abstract": abstract != (row.get("abstract") or ""),
        "authors": authors != (row.get("authors") or []),
        "bibcode": (fields.get("bibcode") or row.get("bibcode")) != row.get("bibcode"),
        "doi": (fields.get("doi") or row.get("doi")) != row.get("doi"),
        "arxiv_id": (fields.get("arxiv_id") or row.get("arxiv_id")) != row.get("arxiv_id"),
        "published": (fields.get("published") or row.get("published")) != row.get("published"),
    }
    return {
        "paper": updated,
        "changed": changed,
        "any_changed": any(changed.values()),
    }


def refresh_paper_metadata(conn: sqlite3.Connection, paper_id: int) -> Dict[str, Any]:
    """Re-fetch metadata for one library row. Identifiers take priority over fuzzy fields."""
    if not (os.environ.get("ADS_API_TOKEN") or "").strip():
        return {"ok": False, "error": "ADS_API_TOKEN is not configured"}

    row = library_db.get_paper_row(conn, paper_id)
    if not row:
        return {"ok": False, "error": "paper not found"}

    doc, method = _resolve_ads_doc(
        bibcode=row.get("bibcode"),
        doi=row.get("doi"),
        arxiv_id=row.get("arxiv_id"),
        title=row.get("title") or "",
        authors=row.get("authors") or [],
        published=row.get("published"),
    )
    if not doc or not method:
        return {
            "ok": False,
            "error": "No ADS match found (need bibcode, DOI, arXiv, or recognizable title/authors)",
            "match_method": None,
        }

    fields = _ads_doc_fields(doc)
    if not (fields.get("title") or "").strip():
        return {"ok": False, "error": "ADS record has no title", "match_method": method}

    applied = _apply_fields(conn, paper_id, fields, match_method=method)
    mark_metadata_synced(conn, paper_id, method)
    conn.commit()
    paper = applied["paper"]
    paper["tags"] = _fetch_tags(conn, paper_id)
    paper["metadata_synced"] = True
    return {
        "ok": True,
        "match_method": method,
        "updated": applied["any_changed"],
        "changed": applied["changed"],
        "tagged": True,
        "paper": paper,
    }
