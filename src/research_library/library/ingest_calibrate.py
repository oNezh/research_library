"""Calibrated PDF ingest: confident ADS write vs pending-metadata row (no embed)."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, Optional

from research_library.library import db as library_db
from research_library.library.metadata_refresh import mark_metadata_synced
from research_library.library.pdf_identifiers import is_junk_title, is_usable_title_candidate
from research_library.lookup import choose_identifier, similarity

PENDING_METADATA_TAG = "pending_metadata"
PENDING_TITLE = "（待补全元数据）"
CONFIDENT_TITLE_SIM = 0.72

__all__ = [
    "PENDING_METADATA_TAG",
    "PENDING_TITLE",
    "CONFIDENT_TITLE_SIM",
    "assess_ingest_confidence",
    "mark_pending_metadata",
    "clear_pending_metadata",
    "has_pending_metadata",
    "count_pending_metadata",
    "create_pending_pdf_paper",
    "after_confident_ingest",
    "confirm_pending_metadata",
]


def _ads_title(doc: Dict[str, Any]) -> str:
    tl = doc.get("title")
    if isinstance(tl, list) and tl:
        return str(tl[0]).strip()
    return str(tl or "").strip()


def assess_ingest_confidence(
    extracted: Dict[str, Any],
    resolved: Dict[str, Any],
) -> Dict[str, Any]:
    """Decide whether ADS-resolved metadata is safe to commit + index."""
    if not resolved.get("ok") or not resolved.get("doc"):
        return {
            "confident": False,
            "reason": "no_ads_match",
            "title_similarity": None,
            "match_method": resolved.get("match_method"),
        }

    method = (resolved.get("match_method") or "").strip()
    doc = resolved["doc"]
    ads_title = _ads_title(doc)
    if is_junk_title(ads_title):
        return {
            "confident": False,
            "reason": "junk_ads_title",
            "title_similarity": None,
            "match_method": method,
        }

    pdf_title = (extracted.get("title_candidate") or "").strip() or None
    if pdf_title and (
        not is_usable_title_candidate(pdf_title) or is_junk_title(pdf_title)
    ):
        pdf_title = None
    sim = similarity(pdf_title or "", ads_title) if pdf_title else None

    if method in ("doi", "arxiv", "manual_bibcode"):
        return {
            "confident": True,
            "reason": f"strong_id:{method}",
            "title_similarity": sim,
            "match_method": method,
        }

    if method == "title":
        if sim is not None and sim >= CONFIDENT_TITLE_SIM:
            return {
                "confident": True,
                "reason": "title_high_sim",
                "title_similarity": sim,
                "match_method": method,
            }
        return {
            "confident": False,
            "reason": "title_weak",
            "title_similarity": sim,
            "match_method": method,
        }

    return {
        "confident": False,
        "reason": f"unknown_method:{method or 'none'}",
        "title_similarity": sim,
        "match_method": method or None,
    }


def mark_pending_metadata(
    conn: sqlite3.Connection,
    paper_id: int,
    *,
    reason: str,
    hints: Optional[Dict[str, Any]] = None,
) -> None:
    now = library_db._now_iso()
    payload = {"reason": reason, "at": now}
    if hints:
        payload["hints"] = hints
    value = json.dumps(payload, ensure_ascii=False)[:2000]
    existing = conn.execute(
        "SELECT id FROM paper_tags WHERE paper_id = ? AND tag_key = ?",
        (paper_id, PENDING_METADATA_TAG),
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE paper_tags SET tag_value = ?, source = 'ingest_calibrate' WHERE id = ?",
            (value, existing["id"]),
        )
    else:
        conn.execute(
            "INSERT INTO paper_tags(paper_id, tag_key, tag_value, source, created_at) "
            "VALUES (?, ?, ?, 'ingest_calibrate', ?)",
            (paper_id, PENDING_METADATA_TAG, value, now),
        )


def clear_pending_metadata(conn: sqlite3.Connection, paper_id: int) -> None:
    conn.execute(
        "DELETE FROM paper_tags WHERE paper_id = ? AND tag_key = ?",
        (paper_id, PENDING_METADATA_TAG),
    )


def has_pending_metadata(conn: sqlite3.Connection, paper_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM paper_tags WHERE paper_id = ? AND tag_key = ? LIMIT 1",
        (paper_id, PENDING_METADATA_TAG),
    ).fetchone()
    return row is not None


def count_pending_metadata(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        """
        SELECT COUNT(DISTINCT paper_id) FROM paper_tags
        WHERE tag_key = ?
        """,
        (PENDING_METADATA_TAG,),
    ).fetchone()
    return int(row[0]) if row else 0


def create_pending_pdf_paper(
    conn: sqlite3.Connection,
    *,
    pdf_relpath: str,
    extracted: Optional[Dict[str, Any]] = None,
    reason: str = "uncertain",
    source: str = "library_ingest_pdf_pending",
    resolved_summary: Optional[Dict[str, Any]] = None,
) -> int:
    """Insert PDF-only row with placeholder title; do not index."""
    extracted = extracted or {}
    # Keep extracted ids as clues only; title/authors stay empty-ish.
    doi = (extracted.get("doi") or "").strip() or None
    arxiv_id = (extracted.get("arxiv_id") or "").strip() or None
    pid = library_db.insert_paper_minimal(
        conn,
        title=PENDING_TITLE,
        abstract="",
        authors=[],
        published=None,
        doi=doi,
        arxiv_id=arxiv_id,
        bibcode=None,
        source=source,
        pdf_relpath=pdf_relpath,
        commit=False,
    )
    hints = {
        "doi": doi,
        "arxiv_id": arxiv_id,
        "title_candidate": extracted.get("title_candidate"),
        "resolved": resolved_summary,
    }
    mark_pending_metadata(conn, pid, reason=reason, hints=hints)
    conn.commit()
    return pid


def after_confident_ingest(
    conn: sqlite3.Connection,
    paper_id: int,
    *,
    match_method: str,
    fetch_ar5iv: bool = True,
    do_index: bool = True,
) -> Dict[str, Any]:
    """Mark synced, optionally fetch ar5iv, then embed (PDF only if no arxiv_id)."""
    from research_library.library.semantic import index_paper
    from research_library.library.tex_to_text import fetch_source_for_paper

    out: Dict[str, Any] = {"paper_id": paper_id}
    clear_pending_metadata(conn, paper_id)
    mark_metadata_synced(conn, paper_id, match_method or "ingest")
    conn.commit()

    paper = library_db.get_paper_row(conn, paper_id) or {}
    arxiv_id = (paper.get("arxiv_id") or "").strip()
    out["arxiv_id"] = arxiv_id or None

    if not do_index:
        out["semantic_index"] = {"ok": False, "skipped": True, "reason": "index_disabled"}
        return out

    if arxiv_id:
        if fetch_ar5iv:
            out["source_fetch"] = fetch_source_for_paper(conn, paper_id, force=True)
        else:
            out["source_fetch"] = {"ok": False, "skipped": True, "reason": "fetch_disabled"}

        paper = library_db.get_paper_row(conn, paper_id) or {}
        has_tex = (paper.get("source_kind") or "").strip() == "tex" and (
            paper.get("source_text_relpath") or ""
        ).strip()
        if not has_tex:
            out["semantic_index"] = {
                "ok": False,
                "skipped": True,
                "reason": "no_ar5iv_source",
                "detail": out.get("source_fetch"),
            }
            return out
        out["semantic_index"] = index_paper(
            conn, paper_id, force=True, allow_pdf_fallback=False, try_fetch_tex=False
        )
        return out

    # No arXiv: PDF embedding is allowed for confident journal papers.
    out["source_fetch"] = {"ok": False, "skipped": True, "reason": "no_arxiv_id"}
    out["semantic_index"] = index_paper(
        conn, paper_id, force=True, allow_pdf_fallback=True, try_fetch_tex=False
    )
    return out


def confirm_pending_metadata(
    conn: sqlite3.Connection,
    paper_id: int,
    *,
    refresh_from_ads: bool = True,
) -> Dict[str, Any]:
    """After user fills identifiers/title: resolve, clear pending, fetch ar5iv, re-embed."""
    paper = library_db.get_paper_row(conn, paper_id)
    if not paper:
        return {"ok": False, "error": "paper not found"}

    was_pending = has_pending_metadata(conn, paper_id)
    refresh_result: Optional[Dict[str, Any]] = None
    method = "manual_confirm"

    if refresh_from_ads:
        from research_library.library.metadata_refresh import refresh_paper_metadata

        has_id = bool(
            (paper.get("bibcode") or "").strip()
            or (paper.get("doi") or "").strip()
            or (paper.get("arxiv_id") or "").strip()
        )
        title = (paper.get("title") or "").strip()
        title_ok = (
            title
            and title != PENDING_TITLE
            and is_usable_title_candidate(title)
            and not is_junk_title(title)
        )
        if has_id or title_ok:
            refresh_result = refresh_paper_metadata(conn, paper_id)
            if refresh_result.get("ok"):
                method = str(refresh_result.get("match_method") or method)
            elif has_id:
                # Keep manual fields; still allow confirm if user filled enough.
                pass
            else:
                return {
                    "ok": False,
                    "error": refresh_result.get("error") or "refresh failed",
                    "refresh": refresh_result,
                }

    paper = library_db.get_paper_row(conn, paper_id) or {}
    title = (paper.get("title") or "").strip()
    if not title or title == PENDING_TITLE:
        return {
            "ok": False,
            "error": "title still empty; fill metadata first",
            "refresh": refresh_result,
        }
    if not (
        (paper.get("bibcode") or "").strip()
        or (paper.get("doi") or "").strip()
        or (paper.get("arxiv_id") or "").strip()
    ):
        return {
            "ok": False,
            "error": "need bibcode, DOI, or arXiv id before confirm",
            "refresh": refresh_result,
        }

    clear_pending_metadata(conn, paper_id)
    post = after_confident_ingest(conn, paper_id, match_method=method, fetch_ar5iv=True)
    return {
        "ok": True,
        "paper_id": paper_id,
        "was_pending": was_pending,
        "refresh": refresh_result,
        **post,
        "paper": library_db.get_paper_row(conn, paper_id),
    }
