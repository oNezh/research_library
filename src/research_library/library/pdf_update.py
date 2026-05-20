"""Refresh PDF file for a paper, preferring the publisher (journal) version.

``update-pdf`` overwrites the existing ``data/pdfs/<safe>.pdf`` in-place; no
historical copy is kept (intentional simplicity — see plan §2).
"""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from research_library.config import get_data_dir, get_pdfs_dir, load_env
from research_library.library import db as library_db
from research_library.library.reference_acquire import normalize_pub_version_label
from research_library.log import log_event
from research_library.lookup import download_pdf, fetch_pdf_links

_KNOWN_LINK_KEYS: Tuple[str, ...] = ("pub", "ads", "eprint", "arxiv")
_DEFAULT_AUTO_ORDER: Tuple[str, ...] = _KNOWN_LINK_KEYS


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _safe_name_for(paper: Dict[str, Any]) -> str:
    key = paper.get("bibcode") or paper.get("arxiv_id") or f"paper{paper.get('id')}"
    return re.sub(r"[^\w\-]", "_", str(key))


def _existing_pdf_path(paper: Dict[str, Any]) -> Optional[str]:
    rel = (paper.get("pdf_relpath") or "").strip()
    if not rel:
        return None
    cand = (get_data_dir() / rel).resolve()
    return str(cand)


def _resolve_dest_path(paper: Dict[str, Any]) -> str:
    existing = _existing_pdf_path(paper)
    if existing:
        return existing
    safe = _safe_name_for(paper)
    return str(get_pdfs_dir() / f"{safe}.pdf")


def update_paper_pdf(
    conn: sqlite3.Connection,
    paper_id: int,
    *,
    source: str = "auto",
    timeout: int = 120,
    reindex: bool = False,
) -> Dict[str, Any]:
    """Refetch PDF for one paper. Overwrites existing file. Returns structured result."""
    load_env()
    library_db.ensure_schema(conn)
    paper = library_db.get_paper_row(conn, paper_id)
    if not paper:
        return {"ok": False, "paper_id": paper_id, "error": "paper_not_found"}

    bibcode = (paper.get("bibcode") or "").strip()
    arxiv_id = (paper.get("arxiv_id") or "").strip()
    if not bibcode and not arxiv_id:
        return {
            "ok": False,
            "paper_id": paper_id,
            "error": "no_bibcode_or_arxiv_id",
        }

    src = (source or "auto").strip().lower()
    try:
        links = fetch_pdf_links(bibcode, arxiv_id_hint=arxiv_id or None) if bibcode else {}
    except Exception as e:
        links = {}
        log_event("pdf_update.fetch_links_failed", paper_id=paper_id, error=str(e))
    if not links.get("arxiv") and arxiv_id:
        links["arxiv"] = f"https://arxiv.org/pdf/{arxiv_id}.pdf"

    if src == "auto":
        order: Tuple[str, ...] = _DEFAULT_AUTO_ORDER
    elif src in _KNOWN_LINK_KEYS:
        order = (src,)
    else:
        return {
            "ok": False,
            "paper_id": paper_id,
            "error": "bad_source",
            "given": source,
            "allowed": list(_DEFAULT_AUTO_ORDER) + ["auto"],
        }

    dest_path = _resolve_dest_path(paper)
    os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)

    tried: List[Dict[str, Any]] = []
    used_label: Optional[str] = None
    for label in order:
        url = links.get(label)
        if not url:
            tried.append({"source": label, "ok": False, "reason": "no_url"})
            continue
        ok = download_pdf(dest_path, url, timeout=timeout)
        tried.append({"source": label, "ok": bool(ok), "url": url})
        if ok:
            used_label = label
            break

    if not used_label:
        return {
            "ok": False,
            "paper_id": paper_id,
            "error": "all_sources_failed",
            "tried": tried,
        }

    new_relpath = library_db.library_pdf_relpath_from_abs(dest_path)
    library_db.update_paper_pdf_metadata(
        conn,
        paper_id,
        pdf_relpath=new_relpath,
        pub_version=normalize_pub_version_label(used_label),
        pdf_fetched_at=_now_iso(),
        commit=True,
    )

    out: Dict[str, Any] = {
        "ok": True,
        "paper_id": paper_id,
        "pdf_path": dest_path,
        "pdf_relpath": new_relpath,
        "pub_version": normalize_pub_version_label(used_label),
        "tried": tried,
    }

    if reindex and (paper.get("source_kind") or "") == "pdf":
        from research_library.library.semantic import index_paper

        try:
            r = index_paper(conn, paper_id, force=True)
            out["reindex"] = r
        except Exception as e:
            out["reindex_error"] = str(e)

    return out


def update_pdfs(
    conn: sqlite3.Connection,
    paper_ids: Optional[List[int]],
    *,
    source: str = "auto",
    timeout: int = 120,
    reindex: bool = False,
) -> Dict[str, Any]:
    """Batch wrapper over :func:`update_paper_pdf`."""
    ids = list(paper_ids) if paper_ids else library_db.list_paper_ids_with_pdf(conn)
    items: List[Dict[str, Any]] = []
    errors = 0
    for pid in ids:
        try:
            r = update_paper_pdf(
                conn, int(pid), source=source, timeout=timeout, reindex=reindex
            )
        except Exception as e:
            r = {"ok": False, "paper_id": int(pid), "error": str(e)}
        items.append(r)
        if not r.get("ok"):
            errors += 1
    return {
        "requested": len(ids),
        "errors": errors,
        "source": source,
        "items": items,
    }
