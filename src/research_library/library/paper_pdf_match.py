"""Detect PDF/metadata mismatches and split into separate library rows."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from research_library.config import get_data_dir
from research_library.library import db as library_db
from research_library.library.metadata_refresh import METADATA_SYNCED_TAG
from research_library.library.pdf_identifiers import (
    extract_pdf_identifiers,
    is_junk_title,
    is_usable_title_candidate,
)
from research_library.library.pdf_ingest import resolve_extracted_to_ads_match
from research_library.library.reference_parse import strip_arxiv_version
from research_library.lookup import similarity

TITLE_MATCH_OK = 0.48
TITLE_MISMATCH = 0.36

__all__ = [
    "is_junk_title",
    "assess_paper_pdf_match",
    "split_paper_pdf",
    "audit_pdf_metadata_matches",
    "export_eligibility",
]


def _pdf_abs_from_row(row: Dict[str, Any]) -> Optional[Path]:
    rel = (row.get("pdf_relpath") or "").strip()
    if not rel:
        return None
    p = get_data_dir() / rel
    return p if p.is_file() else None


def _norm_arxiv(a: Optional[str]) -> Optional[str]:
    s = strip_arxiv_version((a or "").strip())
    return s or None


def _norm_doi(d: Optional[str]) -> Optional[str]:
    return library_db._norm_doi_value(d)


def _ads_title(doc: Dict[str, Any]) -> str:
    tl = doc.get("title")
    if isinstance(tl, list) and tl:
        return str(tl[0]).strip()
    return str(tl or "").strip()


def _strong_id_match(
    stored: Dict[str, Any],
    extracted: Dict[str, Any],
    resolved: Dict[str, Any],
) -> bool:
    s_doi = _norm_doi(stored.get("doi"))
    e_doi = _norm_doi(extracted.get("doi"))
    if s_doi and e_doi and s_doi == e_doi:
        return True

    s_ax = _norm_arxiv(stored.get("arxiv_id"))
    e_ax = _norm_arxiv(extracted.get("arxiv_id"))
    if s_ax and e_ax and s_ax == e_ax:
        return True

    s_bc = (stored.get("bibcode") or "").strip()
    r_bc = (resolved.get("bibcode") or "").strip()
    if s_bc and r_bc and s_bc.upper() == r_bc.upper():
        return True
    return False


def _title_from_source_text(
    text: str,
    sections: Optional[List[Any]],
) -> Optional[str]:
    """Pick a usable title from stored ar5iv/TeX prose (no network)."""
    candidates: List[str] = []
    if sections:
        level1: List[str] = []
        others: List[str] = []
        for sec in sections:
            if hasattr(sec, "title"):
                t = str(sec.title or "").strip()
                lvl = int(getattr(sec, "level", 1) or 1)
            elif isinstance(sec, dict):
                t = str(sec.get("title") or "").strip()
                lvl = int(sec.get("level") or 1)
            else:
                continue
            if not t:
                continue
            (level1 if lvl <= 1 else others).append(t)
        candidates.extend(level1)
        candidates.extend(others)
    if not candidates:
        for line in text.splitlines()[:80]:
            s = line.strip()
            m = re.match(r"^#\s+(.+)$", s)
            if m:
                candidates.append(m.group(1).strip())
                continue
            m = re.match(r"\\title\s*\{(.+?)\}", s)
            if m:
                candidates.append(m.group(1).strip())
    for t in candidates:
        t = re.sub(r"\s+", " ", t).strip()
        if is_usable_title_candidate(t) and not is_junk_title(t):
            return t
    return None


def _read_source_title(conn: sqlite3.Connection, paper_id: int) -> Optional[str]:
    """Use already-stored ar5iv/TeX text only — never fetch."""
    from research_library.library.semantic import _read_stored_tex_source

    text, kind, sections, _path = _read_stored_tex_source(conn, paper_id)
    if not text or kind != "tex":
        return None
    return _title_from_source_text(text, sections)


def _has_tag(conn: sqlite3.Connection, paper_id: int, tag_key: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM paper_tags WHERE paper_id = ? AND tag_key = ? LIMIT 1",
        (paper_id, tag_key),
    ).fetchone()
    return row is not None


def assess_paper_pdf_match(conn: sqlite3.Connection, paper_id: int) -> Dict[str, Any]:
    """Compare stored metadata with PDF identifiers and optional stored ar5iv title."""
    row = library_db.get_paper_row(conn, paper_id)
    if not row:
        return {"ok": False, "error": "paper not found"}

    pdf_abs = _pdf_abs_from_row(row)
    if pdf_abs is None:
        return {
            "ok": True,
            "paper_id": paper_id,
            "has_pdf": False,
            "matched": True,
            "mismatch": False,
            "junk_metadata": is_junk_title(row.get("title") or ""),
            "unreliable_pdf_title": False,
            "has_source_text": False,
            "source_title": None,
        }

    extracted = extract_pdf_identifiers(str(pdf_abs))
    public_extracted = {k: v for k, v in extracted.items() if not str(k).startswith("_")}
    raw_pdf_title = (public_extracted.get("title_candidate") or "").strip()
    pdf_title_ok = is_usable_title_candidate(raw_pdf_title)
    if not pdf_title_ok:
        public_extracted = {**public_extracted, "title_candidate": None}

    resolved = resolve_extracted_to_ads_match(
        public_extracted, title_rows=3, require_strong_id=False
    )

    stored_title = (row.get("title") or "").strip()
    pdf_title = raw_pdf_title if pdf_title_ok else ""
    ads_title = _ads_title(resolved.get("doc") or {}) if resolved.get("ok") else ""
    if ads_title and is_junk_title(ads_title) and (resolved.get("match_method") == "title"):
        resolved = {**resolved, "ok": False, "doc": None, "bibcode": None, "match_method": None}
        ads_title = ""

    source_title = _read_source_title(conn, paper_id)
    has_source = bool(source_title)

    sim_stored_pdf = similarity(stored_title, pdf_title) if pdf_title else 0.0
    sim_stored_ads = similarity(stored_title, ads_title) if ads_title else 0.0
    sim_stored_source = similarity(stored_title, source_title) if source_title else 0.0
    sim_source_pdf = (
        similarity(source_title, pdf_title) if source_title and pdf_title else 0.0
    )

    strong = _strong_id_match(row, public_extracted, resolved)
    best_sim = max(sim_stored_pdf, sim_stored_ads, sim_stored_source)

    junk_meta = is_junk_title(stored_title)
    unreliable = not pdf_title_ok and not (
        public_extracted.get("doi") or public_extracted.get("arxiv_id")
    )

    source_vs_meta = None
    source_vs_pdf = None
    if has_source:
        source_vs_meta = (
            "match"
            if sim_stored_source >= TITLE_MATCH_OK
            else ("mismatch" if sim_stored_source <= TITLE_MISMATCH else "uncertain")
        )
        if pdf_title:
            source_vs_pdf = (
                "match"
                if sim_source_pdf >= TITLE_MATCH_OK
                else ("mismatch" if sim_source_pdf <= TITLE_MISMATCH else "uncertain")
            )

    matched = strong or best_sim >= TITLE_MATCH_OK
    if has_source and source_vs_meta == "match" and source_vs_pdf == "mismatch":
        matched = False
    if has_source and source_vs_pdf == "match" and source_vs_meta == "mismatch":
        matched = False

    if unreliable and not strong and not junk_meta and not has_source:
        mismatch = False
        matched = True
    else:
        mismatch = not matched and (
            best_sim <= TITLE_MISMATCH
            or junk_meta
            or (has_source and source_vs_meta == "match" and source_vs_pdf == "mismatch")
            or (has_source and source_vs_pdf == "match" and source_vs_meta == "mismatch")
            or (
                resolved.get("ok")
                and (resolved.get("bibcode") or "").strip()
                and (row.get("bibcode") or "").strip()
                and (resolved.get("bibcode") or "").strip().upper()
                != (row.get("bibcode") or "").strip().upper()
            )
        )

    issue_hint = None
    if mismatch:
        if has_source and source_vs_meta == "match" and source_vs_pdf == "mismatch":
            issue_hint = "pdf_wrong"
        elif has_source and source_vs_pdf == "match" and source_vs_meta == "mismatch":
            issue_hint = "metadata_wrong"
        elif junk_meta:
            issue_hint = "junk_metadata"
        else:
            issue_hint = "mismatch"

    return {
        "ok": True,
        "paper_id": paper_id,
        "has_pdf": True,
        "matched": matched,
        "mismatch": mismatch,
        "junk_metadata": junk_meta,
        "unreliable_pdf_title": unreliable or (bool(raw_pdf_title) and not pdf_title_ok),
        "strong_id_match": strong,
        "title_similarity": round(best_sim, 3),
        "stored_title": stored_title,
        "pdf_title": (raw_pdf_title or None) if pdf_title_ok else None,
        "pdf_title_raw": raw_pdf_title or None,
        "pdf_ads_title": ads_title or None,
        "pdf_bibcode": resolved.get("bibcode"),
        "stored_bibcode": row.get("bibcode"),
        "match_method": resolved.get("match_method"),
        "has_source_text": has_source,
        "source_title": source_title,
        "sim_stored_source": round(sim_stored_source, 3) if has_source else None,
        "sim_source_pdf": round(sim_source_pdf, 3) if has_source and pdf_title else None,
        "source_vs_meta": source_vs_meta,
        "source_vs_pdf": source_vs_pdf,
        "issue_hint": issue_hint,
        "extracted": public_extracted,
    }


def split_paper_pdf(
    conn: sqlite3.Connection,
    paper_id: int,
    *,
    force: bool = False,
) -> Dict[str, Any]:
    """Detach PDF from metadata row and re-ingest it as a separate paper."""
    assess = assess_paper_pdf_match(conn, paper_id)
    if not assess.get("ok"):
        return assess
    if not assess.get("has_pdf"):
        return {"ok": False, "error": "paper has no PDF on disk"}

    if not force and not assess.get("mismatch") and not assess.get("junk_metadata"):
        return {
            "ok": False,
            "error": "PDF and metadata appear to match; pass force=true to split anyway",
            "assessment": assess,
        }

    row = library_db.get_paper_row(conn, paper_id)
    if not row:
        return {"ok": False, "error": "paper not found"}

    pdf_abs = _pdf_abs_from_row(row)
    if pdf_abs is None:
        return {"ok": False, "error": "PDF file missing on disk"}

    relp = (row.get("pdf_relpath") or "").strip()
    now = library_db._now_iso()

    conn.execute(
        "UPDATE papers SET pdf_relpath = NULL, updated_at = ? WHERE id = ?",
        (now, paper_id),
    )
    conn.execute("DELETE FROM papers_fts WHERE paper_id = ?", (paper_id,))
    conn.execute(
        "INSERT INTO papers_fts(paper_id, title, abstract) VALUES (?, ?, ?)",
        (paper_id, row.get("title") or "", row.get("abstract") or ""),
    )

    from research_library.library.pdf_ingest import ingest_pdf_file

    ingest = ingest_pdf_file(
        conn,
        str(pdf_abs),
        copy_to_pdfs=False,
        source="split_pdf_ingest",
    )

    pdf_paper_id: Optional[int] = ingest.get("paper_id")
    mode = "ads_ingest"

    if ingest.get("ok") and pdf_paper_id == paper_id:
        extracted = ingest.get("extracted") or {}
        title = (
            extracted.get("title_candidate")
            or assess.get("pdf_ads_title")
            or "Untitled (from PDF)"
        ).strip()
        pdf_paper_id = library_db.insert_paper_minimal(
            conn,
            title=title,
            doi=extracted.get("doi"),
            arxiv_id=extracted.get("arxiv_id"),
            pdf_relpath=relp,
            source="split_pdf_ingest",
            commit=False,
        )
        mode = "minimal_collision"

    elif not ingest.get("ok") or pdf_paper_id is None:
        extracted = ingest.get("extracted") or assess.get("extracted") or {}
        title = (extracted.get("title_candidate") or "Untitled (from PDF)").strip()
        pdf_paper_id = library_db.insert_paper_minimal(
            conn,
            title=title,
            doi=extracted.get("doi"),
            arxiv_id=extracted.get("arxiv_id"),
            pdf_relpath=relp,
            source="split_pdf_ingest",
            commit=False,
        )
        mode = "minimal_fallback"

    conn.execute(
        "INSERT OR IGNORE INTO paper_tags(paper_id, tag_key, tag_value, source, created_at) "
        "VALUES (?, 'pdf_detached', ?, 'split_pdf', ?)",
        (paper_id, relp, now),
    )
    if assess.get("junk_metadata"):
        conn.execute(
            "INSERT OR IGNORE INTO paper_tags(paper_id, tag_key, tag_value, source, created_at) "
            "VALUES (?, 'junk_metadata', '1', 'split_pdf', ?)",
            (paper_id, now),
        )

    conn.commit()

    metadata_row = library_db.get_paper_row(conn, paper_id)
    pdf_row = library_db.get_paper_row(conn, int(pdf_paper_id)) if pdf_paper_id else None

    return {
        "ok": True,
        "metadata_paper_id": paper_id,
        "pdf_paper_id": pdf_paper_id,
        "mode": mode,
        "assessment": assess,
        "ingest": {
            "ok": ingest.get("ok"),
            "match_method": ingest.get("match_method"),
            "bibcode": ingest.get("bibcode"),
            "error": ingest.get("error"),
        },
        "metadata_paper": metadata_row,
        "pdf_paper": pdf_row,
    }


def export_eligibility(
    conn: sqlite3.Connection,
    paper_ids: List[int],
    *,
    check_pdf_match: bool = True,
) -> Dict[str, Any]:
    """Decide which papers are safe to export as BibTeX.

    Rules:
    - must have ``metadata_synced`` tag
    - must have bibcode (for ADS BibTeX)
    - if has PDF and ``check_pdf_match``: must not be a confirmed PDF/metadata mismatch
    """
    eligible: List[int] = []
    skipped: List[Dict[str, Any]] = []

    for pid in paper_ids:
        row = library_db.get_paper_row(conn, pid)
        if not row:
            skipped.append({"id": pid, "reason": "not_found"})
            continue
        if not _has_tag(conn, pid, METADATA_SYNCED_TAG):
            skipped.append(
                {"id": pid, "reason": "missing_metadata_synced", "title": row.get("title")}
            )
            continue
        if not (row.get("bibcode") or "").strip():
            skipped.append({"id": pid, "reason": "no_bibcode", "title": row.get("title")})
            continue
        if is_junk_title(row.get("title") or ""):
            skipped.append({"id": pid, "reason": "junk_metadata", "title": row.get("title")})
            continue
        has_pdf = bool((row.get("pdf_relpath") or "").strip())
        if check_pdf_match and has_pdf:
            assess = assess_paper_pdf_match(conn, pid)
            if assess.get("mismatch") or assess.get("junk_metadata"):
                skipped.append(
                    {
                        "id": pid,
                        "reason": assess.get("issue_hint") or "pdf_mismatch",
                        "title": row.get("title"),
                    }
                )
                continue
        eligible.append(pid)

    return {
        "ok": True,
        "eligible": eligible,
        "skipped": skipped,
        "eligible_count": len(eligible),
        "skipped_count": len(skipped),
    }


def audit_pdf_metadata_matches(
    conn: sqlite3.Connection,
    *,
    limit: Optional[int] = None,
    report: Optional[Any] = None,
) -> Dict[str, Any]:
    """Scan papers with PDFs and list likely metadata/PDF mismatches."""
    sql = """
        SELECT id FROM papers
        WHERE pdf_relpath IS NOT NULL AND TRIM(pdf_relpath) != ''
        ORDER BY id
    """
    params: List[Any] = []
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))

    ids = [int(r[0]) for r in conn.execute(sql, params).fetchall()]
    mismatches: List[Dict[str, Any]] = []
    junk: List[Dict[str, Any]] = []

    for i, pid in enumerate(ids):
        if report:
            report(100.0 * i / max(len(ids), 1), f"checking {i + 1}/{len(ids)}")
        item = assess_paper_pdf_match(conn, pid)
        if not item.get("ok") or not item.get("has_pdf"):
            continue
        if item.get("junk_metadata"):
            junk.append(
                {
                    "paper_id": pid,
                    "title": item.get("stored_title"),
                    "title_similarity": item.get("title_similarity"),
                }
            )
        if item.get("mismatch"):
            mismatches.append(
                {
                    "paper_id": pid,
                    "stored_title": item.get("stored_title"),
                    "pdf_title": item.get("pdf_title"),
                    "pdf_ads_title": item.get("pdf_ads_title"),
                    "title_similarity": item.get("title_similarity"),
                    "junk_metadata": item.get("junk_metadata"),
                    "stored_bibcode": item.get("stored_bibcode"),
                    "pdf_bibcode": item.get("pdf_bibcode"),
                    "has_source_text": item.get("has_source_text"),
                    "source_title": item.get("source_title"),
                    "source_vs_meta": item.get("source_vs_meta"),
                    "source_vs_pdf": item.get("source_vs_pdf"),
                    "issue_hint": item.get("issue_hint"),
                }
            )

    if report:
        report(100.0, f"audit done: {len(mismatches)} mismatches, {len(junk)} junk")

    seen: set[int] = set()
    issues: List[Dict[str, Any]] = []
    for m in mismatches:
        pid = int(m["paper_id"])
        seen.add(pid)
        issues.append({**m, "issue_kind": "mismatch"})
    for j in junk:
        pid = int(j["paper_id"])
        if pid in seen:
            continue
        issues.append(
            {
                "paper_id": pid,
                "stored_title": j.get("title"),
                "pdf_title": None,
                "pdf_ads_title": None,
                "title_similarity": j.get("title_similarity"),
                "junk_metadata": True,
                "stored_bibcode": None,
                "pdf_bibcode": None,
                "issue_kind": "junk",
                "issue_hint": "junk_metadata",
            }
        )

    return {
        "ok": True,
        "scanned": len(ids),
        "mismatch_count": len(mismatches),
        "junk_count": len(junk),
        "issue_count": len(issues),
        "mismatches": mismatches[:500],
        "junk": junk[:200],
        "issues": issues[:500],
    }
