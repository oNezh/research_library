"""Traditional library management: BibTeX/CSV export and Zotero status."""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from research_library.server.deps import get_conn

router = APIRouter(tags=["manage"])


class ArxivKeywordsBody(BaseModel):
    text: str


@router.get("/arxiv/keywords")
def get_arxiv_keywords() -> Dict[str, Any]:
    from research_library.arxiv_keyword_settings import load_phrases, phrases_to_text

    phrases = load_phrases()
    return {"phrases": phrases, "text": phrases_to_text(phrases)}


@router.patch("/arxiv/keywords")
def patch_arxiv_keywords(body: ArxivKeywordsBody) -> Dict[str, Any]:
    from research_library.arxiv_keyword_settings import save_phrases, text_to_phrases, phrases_to_text

    phrases = save_phrases(text_to_phrases(body.text))
    return {"phrases": phrases, "text": phrases_to_text(phrases)}


def _parse_ids(ids: str) -> List[int]:
    try:
        out = [int(x) for x in ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(status_code=422, detail="ids must be comma-separated integers")
    if not out:
        raise HTTPException(status_code=422, detail="ids must not be empty")
    if len(out) > 500:
        raise HTTPException(status_code=422, detail="too many ids (max 500)")
    return out


@router.get("/export/eligibility")
def export_eligibility_endpoint(
    ids: str = Query(..., description="Comma-separated paper ids"),
    check_pdf_match: bool = Query(True),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Dict[str, Any]:
    from research_library.library.paper_pdf_match import export_eligibility

    return export_eligibility(conn, _parse_ids(ids), check_pdf_match=check_pdf_match)


@router.get("/export/bibtex", response_class=PlainTextResponse)
def export_bibtex(
    ids: str = Query(..., description="Comma-separated paper ids"),
    verified_only: bool = Query(
        True,
        description="Only export papers with metadata_synced (+ bibcode; PDF must not mismatch)",
    ),
    check_pdf_match: bool = Query(True),
    conn: sqlite3.Connection = Depends(get_conn),
) -> str:
    from research_library.lookup import fetch_bibtex_bulk

    id_list = _parse_ids(ids)
    if verified_only:
        from research_library.library.paper_pdf_match import export_eligibility

        elig = export_eligibility(conn, id_list, check_pdf_match=check_pdf_match)
        id_list = list(elig["eligible"])
        if not id_list:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "no eligible papers for verified BibTeX export",
                    "skipped": elig["skipped"],
                },
            )

    qmarks = ",".join("?" * len(id_list))
    rows = conn.execute(
        f"SELECT id, bibcode, arxiv_id, doi, title, authors_json, published "
        f"FROM papers WHERE id IN ({qmarks})",
        id_list,
    ).fetchall()

    import os

    bibcodes = [r["bibcode"] for r in rows if (r["bibcode"] or "").strip()]
    parts: List[str] = []
    ads_failed_bibcodes: List[str] = []
    if bibcodes:
        if not (os.environ.get("ADS_API_TOKEN") or "").strip():
            ads_failed_bibcodes = bibcodes
        else:
            bib = fetch_bibtex_bulk(bibcodes)
            if bib:
                parts.append(bib.strip())
            else:
                # ADS unavailable (rate limit etc.) -- fall back to minimal entries.
                ads_failed_bibcodes = bibcodes

    # Fallback minimal entries for papers without bibcode or when ADS failed.
    for r in rows:
        bc = (r["bibcode"] or "").strip()
        if bc and bc not in ads_failed_bibcodes:
            continue
        try:
            authors = json.loads(r["authors_json"] or "[]")
        except json.JSONDecodeError:
            authors = []
        year = (r["published"] or "")[:4]
        key = bc.replace(" ", "") if bc else f"paper{r['id']}"
        fields = [f"  title = {{{r['title'] or ''}}}"]
        if authors:
            fields.append(f"  author = {{{' and '.join(authors)}}}")
        if year.isdigit():
            fields.append(f"  year = {{{year}}}")
        if r["doi"]:
            fields.append(f"  doi = {{{r['doi']}}}")
        if r["arxiv_id"]:
            fields.append(f"  eprint = {{{r['arxiv_id']}}}")
        parts.append("@article{" + key + ",\n" + ",\n".join(fields) + "\n}")

    return "\n\n".join(parts) + "\n"


@router.get("/export/csv", response_class=PlainTextResponse)
def export_csv(
    ids: str = Query(..., description="Comma-separated paper ids"),
    conn: sqlite3.Connection = Depends(get_conn),
) -> str:
    id_list = _parse_ids(ids)
    qmarks = ",".join("?" * len(id_list))
    rows = conn.execute(
        f"SELECT id, title, authors_json, published, bibcode, arxiv_id, doi "
        f"FROM papers WHERE id IN ({qmarks}) ORDER BY id",
        id_list,
    ).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "title", "authors", "published", "bibcode", "arxiv_id", "doi"])
    for r in rows:
        try:
            authors = "; ".join(json.loads(r["authors_json"] or "[]"))
        except json.JSONDecodeError:
            authors = ""
        w.writerow([r["id"], r["title"], authors, r["published"], r["bibcode"], r["arxiv_id"], r["doi"]])
    return buf.getvalue()


@router.get("/zotero/status")
def zotero_status(conn: sqlite3.Connection = Depends(get_conn)) -> Dict[str, Any]:
    from research_library.library import zotero_sync as zs

    library_id, api_key, library_type = zs._zotero_config()
    from research_library.library import db as library_db

    local = library_db.zotero_sync_stats(conn)
    unknown_authors = conn.execute(
        "SELECT COUNT(*) FROM papers WHERE authors_json LIKE '%Unknown%'"
    ).fetchone()[0]
    return {
        "configured": bool(library_id and api_key),
        "library_id": library_id,
        "library_type": library_type,
        "local": local,
        "unknown_authors": int(unknown_authors),
        "last_pull_version": library_db.get_zotero_sync_version(conn, library_id or ""),
    }


@router.get("/dashboard")
def dashboard(conn: sqlite3.Connection = Depends(get_conn)) -> Dict[str, Any]:
    per_year = conn.execute(
        """
        SELECT substr(published, 1, 4) y, COUNT(*) n FROM papers
        WHERE published GLOB '[0-9][0-9][0-9][0-9]*'
        GROUP BY y ORDER BY y
        """
    ).fetchall()
    per_source = conn.execute(
        "SELECT source, COUNT(*) n FROM papers GROUP BY source ORDER BY n DESC LIMIT 10"
    ).fetchall()
    added_per_month = conn.execute(
        """
        SELECT substr(created_at, 1, 7) m, COUNT(*) n FROM papers
        GROUP BY m ORDER BY m DESC LIMIT 12
        """
    ).fetchall()
    recent = conn.execute(
        """
        SELECT id, title, bibcode, published, created_at FROM papers
        ORDER BY created_at DESC LIMIT 10
        """
    ).fetchall()
    counts = conn.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM papers) AS papers,
          (SELECT COUNT(*) FROM papers WHERE pdf_relpath IS NOT NULL AND TRIM(pdf_relpath) != '') AS with_pdf,
          (SELECT COUNT(DISTINCT paper_id) FROM paper_chunks) AS indexed_papers,
          (SELECT COUNT(*) FROM paper_references) AS reference_edges,
          (SELECT COUNT(*) FROM zotero_links) AS zotero_linked
        """
    ).fetchone()
    return {
        "counts": dict(counts),
        "per_year": [{"year": r["y"], "count": r["n"]} for r in per_year],
        "per_source": [{"source": r["source"], "count": r["n"]} for r in per_source],
        "added_per_month": [{"month": r["m"], "count": r["n"]} for r in reversed(added_per_month)],
        "recent": [dict(r) for r in recent],
    }
