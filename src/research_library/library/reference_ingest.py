"""Upsert library.db rows (+ pdf_relpath) after resolve / PDF acquire."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

from research_library.config import get_data_dir
from . import db as library_db
from .bib_export import ingest_ads_doc
from .reference_parse import StandardRef, strip_arxiv_version
from research_library.lookup import ads_fetch_doc_by_bibcode, ads_query


def _sync_refs_on_downloaded_ingest_enabled() -> bool:
    v = (os.environ.get("RESEARCH_PDF_INGEST_SYNC_REFERENCES") or "").strip().lower()
    if v in ("0", "false", "no", "off"):
        return False
    return True


def library_pdf_relpath(pdf_abs: str) -> str:
    data_root = get_data_dir().resolve()
    p = Path(pdf_abs).resolve()
    try:
        rel = p.relative_to(data_root)
        return str(rel).replace("\\", "/")
    except ValueError:
        return str(p).replace("\\", "/")


def short_label(text: str, max_len: int = 500) -> str:
    one = re.sub(r"\s+", " ", text.strip())
    if len(one) <= max_len:
        return one
    return one[: max_len - 1] + "…"


def fetch_ads_doc_for_standard_ref(ref: StandardRef) -> Optional[Dict[str, Any]]:
    import os

    if not (os.environ.get("ADS_API_TOKEN") or "").strip():
        return None
    if ref.bibcode:
        return ads_fetch_doc_by_bibcode(str(ref.bibcode))
    arx = ref.arxiv_id
    if arx:
        try:
            r = ads_query(f"arxiv:{arx}", rows=1)
            docs = r.get("response", {}).get("docs", [])
            if docs:
                d = docs[0]
                bc = d.get("bibcode")
                if bc:
                    full = ads_fetch_doc_by_bibcode(str(bc))
                    if full:
                        return full
                return d
        except Exception:
            return None
    if ref.doi:
        try:
            r = ads_query(f'doi:"{ref.doi}"', rows=1)
            docs = r.get("response", {}).get("docs", [])
            if docs:
                d = docs[0]
                bc = d.get("bibcode")
                if bc:
                    full = ads_fetch_doc_by_bibcode(str(bc))
                    if full:
                        return full
                return d
        except Exception:
            return None
    return None


def ingest_downloaded_reference(
    conn: Any,
    ref: StandardRef,
    pdf_abs: str,
    *,
    source: str = "pdf_reference_chain",
    acquire_reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Full ADS metadata + pdf_relpath when possible; else minimal arxiv row."""
    from datetime import datetime, timezone

    from research_library.library.reference_acquire import normalize_pub_version_label

    library_db.ensure_schema(conn)
    relp = library_pdf_relpath(pdf_abs)
    arx = ref.arxiv_id
    if not arx:
        m = re.search(r"\b(\d{4}\.\d{4,5}(?:v\d+)?)\b", ref.raw_line)
        if m:
            arx = strip_arxiv_version(m.group(1))

    pub_version = normalize_pub_version_label(acquire_reason or "")
    fetched_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    ads_doc = fetch_ads_doc_for_standard_ref(ref)
    if ads_doc and ads_doc.get("bibcode"):
        ingest_ads_doc(conn, ads_doc, source=source, pdf_relpath=relp)
        bc = str(ads_doc["bibcode"]).strip()
        if bc:
            row = conn.execute(
                "SELECT id FROM papers WHERE bibcode = ?", (bc,)
            ).fetchone()
            if row:
                library_db.update_paper_pdf_metadata(
                    conn,
                    int(row[0]),
                    pub_version=pub_version,
                    pdf_fetched_at=fetched_at,
                    commit=False,
                )
        if _sync_refs_on_downloaded_ingest_enabled() and bc:
            row = conn.execute(
                "SELECT id FROM papers WHERE bibcode = ?", (bc,)
            ).fetchone()
            if row and (os.environ.get("ADS_API_TOKEN") or "").strip():
                from research_library.library.citations import fetch_ads_reference_bibcodes
                from research_library.log import log_event

                try:
                    library_db.replace_paper_references(
                        conn, int(row[0]), fetch_ads_reference_bibcodes(bc)
                    )
                except Exception as _err:
                    # Surface in structured logs instead of silently swallowing — the
                    # caller may want to retry, or the user may want to know we
                    # could not populate the citation graph for this paper.
                    log_event(
                        "reference_ingest.sync_refs_failed",
                        bibcode=bc,
                        paper_id=int(row[0]),
                        error=str(_err),
                    )
        return {"ok": True, "mode": "ads", "pdf_relpath": relp}

    if arx:
        pid = library_db.upsert_paper(
            conn,
            arxiv_id=arx,
            title=short_label(ref.raw_line, 500),
            abstract="",
            authors=[],
            categories=[],
            matched_keywords=[],
            published=None,
            bibcode=None,
            source=source,
            pdf_relpath=relp,
        )
        library_db.update_paper_pdf_metadata(
            conn,
            int(pid),
            pub_version=pub_version,
            pdf_fetched_at=fetched_at,
            commit=False,
        )
        return {"ok": True, "mode": "minimal_arxiv", "pdf_relpath": relp}

    return {"ok": False, "reason": "no_ads_match_and_no_arxiv_id"}


def ingest_from_ads_doc(
    conn: Any,
    doc: Dict[str, Any],
    *,
    source: str = "parse_pipeline",
    pdf_relpath: Optional[str] = None,
) -> None:
    """Thin wrapper for callers that already have an ADS document dict."""
    ingest_ads_doc(conn, doc, source=source, pdf_relpath=pdf_relpath)
