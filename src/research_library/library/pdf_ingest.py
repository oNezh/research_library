"""Resolve PDF-extracted identifiers to ADS and ingest into library.db."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from research_library.config import get_pdfs_dir
from research_library.library import db as library_db
from research_library.library.bib_export import ingest_ads_doc
from research_library.library.pdf_identifiers import extract_pdf_identifiers
from research_library.library.reference_ingest import library_pdf_relpath
from research_library.lookup import ads_fetch_doc_by_bibcode, ads_query, choose_identifier


def _public_extracted(ext: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in ext.items() if not str(k).startswith("_")}


def _ads_url(bibcode: str) -> str:
    from urllib.parse import quote

    return f"https://ui.adsabs.harvard.edu/abs/{quote(bibcode)}/abstract"


def _summarize_doc(d: Dict[str, Any]) -> Dict[str, Any]:
    bc = (d.get("bibcode") or "").strip()
    tl = d.get("title") or []
    t = " ".join(tl[0].split()) if isinstance(tl, list) and tl else ""
    return {"bibcode": bc, "title": t, "ads_url": _ads_url(bc) if bc else ""}


def _normalize_doi(s: str) -> str:
    return (s or "").strip().lower()


def _doc_dois(doc: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    raw = doc.get("doi")
    if isinstance(raw, list):
        for x in raw:
            if x:
                out.append(_normalize_doi(str(x)))
    elif isinstance(raw, str) and raw.strip():
        out.append(_normalize_doi(raw))
    for ident in doc.get("identifier") or []:
        if isinstance(ident, str) and ident.lower().startswith("doi:"):
            out.append(_normalize_doi(ident[4:]))
    seen: set[str] = set()
    uniq: List[str] = []
    for d in out:
        if d and d not in seen:
            seen.add(d)
            uniq.append(d)
    return uniq


def _is_arxiv_eprint_bibcode(bc: str) -> bool:
    if not bc:
        return False
    return "ARXIV" in bc.upper()


def _prefer_ads_docs(docs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Rank ADS hits: prefer non-arXiv bibcodes, then newer year, then bibcode (stable)."""

    def sort_key(d: Dict[str, Any]) -> tuple[int, int, str]:
        bc = (d.get("bibcode") or "").strip()
        year = d.get("year")
        try:
            yi = int(year) if year is not None else 0
        except (TypeError, ValueError):
            yi = 0
        eprint = 1 if _is_arxiv_eprint_bibcode(bc) else 0
        return (eprint, -yi, bc)

    return sorted(docs, key=sort_key)


def resolve_extracted_to_ads_match(
    extracted: Dict[str, Any],
    *,
    title_rows: int = 3,
    require_strong_id: bool = False,
) -> Dict[str, Any]:
    """Match order: DOI, arXiv, then optional title search."""
    if not (os.environ.get("ADS_API_TOKEN") or "").strip():
        return {
            "ok": False,
            "error": "ADS_API_TOKEN is not set",
            "match_method": None,
            "bibcode": None,
            "doc": None,
            "candidates": [],
            "extracted": _public_extracted(extracted),
        }

    doi = (extracted.get("doi") or "").strip() or None
    arxiv_id = (extracted.get("arxiv_id") or "").strip() or None
    title = (extracted.get("title_candidate") or "").strip() or None

    if require_strong_id and not doi and not arxiv_id:
        return {
            "ok": False,
            "error": "require_strong_id: no DOI or arXiv in PDF",
            "match_method": None,
            "bibcode": None,
            "doc": None,
            "candidates": [],
            "extracted": _public_extracted(extracted),
        }

    candidates: List[Dict[str, Any]] = []
    match_method: Optional[str] = None
    bibcode: Optional[str] = None
    thin_doc: Optional[Dict[str, Any]] = None

    try:
        if doi:
            result = ads_query(f'doi:"{doi}"', rows=1)
            docs = result.get("response", {}).get("docs", [])
            if docs:
                thin_doc = docs[0]
                match_method = "doi"
        if thin_doc is None and arxiv_id:
            result = ads_query(f"arxiv:{arxiv_id}", rows=15)
            docs = result.get("response", {}).get("docs", [])
            if docs:
                docs = _prefer_ads_docs(docs)
                thin_doc = docs[0]
                match_method = "arxiv"
                candidates = [_summarize_doc(d) for d in docs[:10]]
        if thin_doc is None and title:
            result = ads_query(f'"{title}"', rows=max(1, int(title_rows)))
            docs = result.get("response", {}).get("docs", [])
            if docs:
                docs = _prefer_ads_docs(docs)
                thin_doc = docs[0]
                match_method = "title"
                candidates = [_summarize_doc(d) for d in docs]

        if (
            thin_doc is not None
            and doi
            and match_method in ("arxiv", "title")
        ):
            ext = _normalize_doi(doi)
            got = _doc_dois(thin_doc)
            if got and ext not in got:
                r2 = ads_query(f'doi:"{doi}"', rows=2)
                alts = r2.get("response", {}).get("docs", [])
                if len(alts) == 1:
                    thin_doc = alts[0]
                    match_method = "doi_disambiguates_metadata"
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "match_method": match_method,
            "bibcode": None,
            "doc": None,
            "candidates": candidates,
            "extracted": _public_extracted(extracted),
        }

    if thin_doc is None:
        err = "No ADS match (check DOI/arXiv/title extraction)"
        if not doi and not arxiv_id and not title:
            err = "Could not extract DOI, arXiv, or title from PDF"
        return {
            "ok": False,
            "error": err,
            "match_method": None,
            "bibcode": None,
            "doc": None,
            "candidates": [],
            "extracted": _public_extracted(extracted),
        }

    bibcode = (thin_doc.get("bibcode") or "").strip() or None
    full_doc = ads_fetch_doc_by_bibcode(bibcode) if bibcode else None
    doc_out = full_doc if full_doc is not None else thin_doc
    bc_final = (doc_out.get("bibcode") or "").strip() or bibcode
    if not candidates:
        candidates = [_summarize_doc(doc_out)]

    return {
        "ok": True,
        "error": None,
        "match_method": match_method,
        "bibcode": bc_final,
        "doc": doc_out,
        "candidates": candidates,
        "extracted": _public_extracted(extracted),
    }


def _default_require_strong_id() -> bool:
    v = (os.environ.get("RESEARCH_PDF_INGEST_REQUIRE_STRONG_ID") or "").strip().lower()
    return v in ("1", "true", "yes")


def _default_sync_references_on_ingest() -> bool:
    v = (os.environ.get("RESEARCH_PDF_INGEST_SYNC_REFERENCES") or "").strip().lower()
    if v in ("0", "false", "no", "off"):
        return False
    return True


def _default_auto_semantic_index_on_ingest() -> bool:
    v = (os.environ.get("RESEARCH_INGEST_AUTO_SEMANTIC_INDEX") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _sync_paper_references_from_ads(
    conn: Any,
    paper_id: int,
    bibcode: str,
) -> Dict[str, Any]:
    """Fill ``paper_references`` from ADS ``reference`` field (same source as ``citation sync``)."""
    if not (os.environ.get("ADS_API_TOKEN") or "").strip():
        return {"ok": False, "skipped": True, "reason": "no_ADS_API_TOKEN"}
    bc = (bibcode or "").strip()
    if not bc:
        return {"ok": False, "skipped": True, "reason": "no_bibcode"}
    try:
        from research_library.library.citations import fetch_ads_reference_bibcodes

        refs = fetch_ads_reference_bibcodes(bc)
        n = library_db.replace_paper_references(conn, paper_id, refs)
        return {
            "ok": True,
            "edges_written": n,
            "ads_reference_field_len": len(refs),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _safe_stem(s: str) -> str:
    stem = re.sub(r"[^\w\-.]+", "_", s.strip())[:120]
    return stem or "paper"


def _place_pdf_in_library(
    pdf_abs: str,
    *,
    bibcode: Optional[str],
    arxiv_id: Optional[str],
    copy_to_pdfs: bool,
    symlink_to_pdfs: bool,
) -> tuple[str, Dict[str, Any]]:
    """Return (absolute path to PDF for recording, meta about operation)."""
    src = str(Path(pdf_abs).resolve())
    meta: Dict[str, Any] = {"mode": "in_place", "path": src}

    if not copy_to_pdfs and not symlink_to_pdfs:
        return src, meta

    key = bibcode or arxiv_id or Path(src).stem
    name = f"{_safe_stem(key)}.pdf"
    dest_path = get_pdfs_dir() / name

    if dest_path.resolve() == Path(src).resolve():
        return src, meta

    if copy_to_pdfs:
        shutil.copy2(src, dest_path)
        meta["mode"] = "copied"
        meta["path"] = str(dest_path.resolve())
        meta["relative_to_data"] = f"pdfs/{name}"
        return meta["path"], meta

    if symlink_to_pdfs:
        if dest_path.exists() or dest_path.is_symlink():
            dest_path.unlink()
        os.symlink(src, dest_path)
        meta["mode"] = "symlinked"
        meta["path"] = str(dest_path.resolve())
        meta["relative_to_data"] = f"pdfs/{name}"
        return meta["path"], meta

    return src, meta


def preresolved_from_manual_bibcode(bibcode: str) -> Dict[str, Any]:
    """Build a :func:`resolve_extracted_to_ads_match`-shaped dict from a known ADS bibcode."""
    bc = (bibcode or "").strip()
    if not bc:
        return {
            "ok": False,
            "error": "empty bibcode",
            "match_method": None,
            "bibcode": None,
            "doc": None,
            "candidates": [],
            "extracted": {},
        }
    if not (os.environ.get("ADS_API_TOKEN") or "").strip():
        return {
            "ok": False,
            "error": "ADS_API_TOKEN is not set",
            "match_method": None,
            "bibcode": None,
            "doc": None,
            "candidates": [],
            "extracted": {},
        }
    doc = ads_fetch_doc_by_bibcode(bc)
    if not doc:
        return {
            "ok": False,
            "error": f"No ADS document for bibcode {bc!r}",
            "match_method": None,
            "bibcode": None,
            "doc": None,
            "candidates": [],
            "extracted": {},
        }
    bc_final = (doc.get("bibcode") or "").strip() or bc
    return {
        "ok": True,
        "error": None,
        "match_method": "manual_bibcode",
        "bibcode": bc_final,
        "doc": doc,
        "candidates": [_summarize_doc(doc)],
        "extracted": {},
    }


def ingest_pdf_file(
    conn: Any,
    pdf_abs: str,
    *,
    dry_run: bool = False,
    require_strong_id: Optional[bool] = None,
    title_rows: int = 3,
    copy_to_pdfs: bool = True,
    symlink_to_pdfs: bool = False,
    source: str = "library_ingest_pdf",
    preresolved: Optional[Dict[str, Any]] = None,
    extracted_override: Optional[Dict[str, Any]] = None,
    sync_references: Optional[bool] = None,
) -> Dict[str, Any]:
    """Extract identifiers from PDF, resolve in ADS, upsert ``papers`` + ``pdf_relpath``.

    If ``preresolved`` is set (from :func:`resolve_extracted_to_ads_match`), skip ADS queries.

    After a successful DB ingest, by default loads ADS reference bibcodes into ``paper_references``
    (disable with ``sync_references=False`` or env ``RESEARCH_PDF_INGEST_SYNC_REFERENCES=0``).
    """
    path = str(Path(pdf_abs).expanduser().resolve())
    if not Path(path).is_file():
        return {"ok": False, "error": f"file not found: {path}"}

    if require_strong_id is None:
        require_strong_id = _default_require_strong_id()

    if preresolved is not None:
        resolved = preresolved
        if extracted_override is not None:
            extracted = {
                k: v
                for k, v in extracted_override.items()
                if not str(k).startswith("_")
            }
        else:
            extracted = {
                k: v
                for k, v in (resolved.get("extracted") or {}).items()
                if not str(k).startswith("_")
            }
    else:
        merged_ext = extract_pdf_identifiers(path)
        if extracted_override:
            merged_ext = {**merged_ext, **extracted_override}
        extracted = {k: v for k, v in merged_ext.items() if not str(k).startswith("_")}
        resolved = resolve_extracted_to_ads_match(
            merged_ext,
            title_rows=title_rows,
            require_strong_id=require_strong_id,
        )

    out: Dict[str, Any] = {
        "ok": resolved["ok"],
        "error": resolved.get("error"),
        "dry_run": dry_run,
        "match_method": resolved.get("match_method"),
        "bibcode": resolved.get("bibcode"),
        "candidates": resolved.get("candidates") or [],
        "extracted": extracted,
        "pdf_path_input": path,
        "pdf_path_stored": path,
        "placement": {"mode": "in_place", "path": path},
        "paper_id": None,
    }

    if not resolved["ok"] or not resolved.get("doc"):
        return out

    doc = resolved["doc"]
    bc = (doc.get("bibcode") or "").strip() or None
    _, arx = choose_identifier(doc.get("identifier") or [])

    final_path, placement = _place_pdf_in_library(
        path,
        bibcode=bc,
        arxiv_id=arx or extracted.get("arxiv_id"),
        copy_to_pdfs=copy_to_pdfs,
        symlink_to_pdfs=symlink_to_pdfs and not copy_to_pdfs,
    )
    out["pdf_path_stored"] = final_path
    out["placement"] = placement
    relp = library_pdf_relpath(final_path)

    if dry_run:
        out["pdf_relpath_would_be"] = relp
        return out

    library_db.ensure_schema(conn)
    ingest_ads_doc(conn, doc, source=source, pdf_relpath=relp)
    row = None
    if bc:
        row = conn.execute("SELECT id FROM papers WHERE bibcode = ?", (bc,)).fetchone()
    if row is None:
        ax = (arx or extracted.get("arxiv_id") or "").strip()
        if ax:
            row = conn.execute("SELECT id FROM papers WHERE arxiv_id = ?", (ax,)).fetchone()
    if row:
        out["paper_id"] = int(row[0])
    out["pdf_relpath"] = relp

    do_sync = (
        _default_sync_references_on_ingest()
        if sync_references is None
        else bool(sync_references)
    )
    if out.get("paper_id") is not None and bc and do_sync:
        out["references_sync"] = _sync_paper_references_from_ads(
            conn, int(out["paper_id"]), bc
        )
    elif do_sync and not bc:
        out["references_sync"] = {
            "ok": False,
            "skipped": True,
            "reason": "no_bibcode",
        }

    if not dry_run and out.get("paper_id") is not None and _default_auto_semantic_index_on_ingest():
        try:
            from research_library.library.semantic import index_paper

            out["semantic_index"] = index_paper(conn, int(out["paper_id"]), force=False)
        except Exception as e:
            out["semantic_index"] = {"ok": False, "error": str(e)}

    return out


def resolve_pdf_to_ads(pdf_path: str, **kwargs: Any) -> Dict[str, Any]:
    """Convenience: extract + ADS match (no DB write)."""
    p = str(Path(pdf_path).expanduser().resolve())
    ext = extract_pdf_identifiers(p)
    r = resolve_extracted_to_ads_match(ext, **kwargs)
    r["pdf_path"] = p
    return r
