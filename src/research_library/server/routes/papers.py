"""Paper listing, detail, edit and delete endpoints."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from research_library.library import db as library_db
from research_library.library.ingest_calibrate import PENDING_METADATA_TAG
from research_library.library.metadata_refresh import METADATA_SYNCED_TAG
from research_library.server.deps import get_conn

router = APIRouter(tags=["papers"])

_ADS_REF_CACHE: Dict[str, Dict[str, Any]] = {}
_MAX_ADS_REF_CACHE = 8000

_SORTABLE = {
    "id": "p.id",
    "title": "p.title",
    "published": "p.published",
    "created_at": "p.created_at",
    "updated_at": "p.updated_at",
    "bibcode": "p.bibcode",
}


def _bibcode_year(bibcode: str) -> Optional[str]:
    m = re.match(r"^(\d{4})", bibcode or "")
    return m.group(1) if m else None


def _bibcode_journal(bibcode: str) -> Optional[str]:
    m = re.match(r"^\d{4}([A-Za-z&+]+)", bibcode or "")
    return m.group(1) if m else None


def _arxiv_from_bibcode(bibcode: str) -> Optional[str]:
    m = re.search(r"arXiv(\d{4}\.\d{4,5})", bibcode or "", re.I)
    return m.group(1) if m else None


def _fetch_ads_ref_metadata(bibcodes: List[str]) -> Dict[str, Dict[str, Any]]:
    if not bibcodes or not (os.environ.get("ADS_API_TOKEN") or "").strip():
        return {}
    from research_library.lookup import ads_query

    out: Dict[str, Dict[str, Any]] = {
        bc: _ADS_REF_CACHE[bc] for bc in bibcodes if bc in _ADS_REF_CACHE
    }
    pending = [bc for bc in bibcodes if bc not in out]
    if not pending:
        return out
    for i in range(0, len(pending), 15):
        batch = pending[i : i + 15]
        q = " OR ".join(f'bibcode:"{b}"' for b in batch)
        try:
            result = ads_query(
                q, rows=len(batch), fl=["bibcode", "title", "author", "year", "pub"]
            )
            for d in result.get("response", {}).get("docs", []):
                bc = str(d.get("bibcode") or "").strip()
                if not bc:
                    continue
                titles = d.get("title") or []
                title = " ".join(str(titles[0]).split()) if titles else ""
                meta = {
                    "title": title,
                    "authors": list(d.get("author") or []),
                    "year": str(d.get("year") or ""),
                    "journal": str(d.get("pub") or ""),
                }
                out[bc] = meta
                if len(_ADS_REF_CACHE) < _MAX_ADS_REF_CACHE:
                    _ADS_REF_CACHE[bc] = meta
        except Exception:
            continue
    return out


def _enrich_references(
    refs_rows: List[sqlite3.Row], *, fetch_ads: bool = False
) -> List[Dict[str, Any]]:
    raw = [dict(r) for r in refs_rows]
    missing_ads = [
        r["ref_bibcode"] for r in raw if not (r.get("local_title") or "").strip()
    ]
    ads_map = _fetch_ads_ref_metadata(missing_ads) if fetch_ads else {}
    enriched: List[Dict[str, Any]] = []
    for r in raw:
        bc = r["ref_bibcode"]
        local_title = (r.get("local_title") or "").strip()
        if local_title:
            try:
                authors = json.loads(r.get("local_authors_json") or "[]")
            except json.JSONDecodeError:
                authors = []
            published = (r.get("local_published") or "").strip()
            year = (
                published[:4]
                if len(published) >= 4 and published[:4].isdigit()
                else _bibcode_year(bc)
            )
            journal = _bibcode_journal(r.get("local_bibcode") or bc) or ""
            title = local_title
        else:
            meta = ads_map.get(bc, {})
            authors = meta.get("authors") or []
            year = str(meta.get("year") or "") or _bibcode_year(bc) or ""
            journal = str(meta.get("journal") or "") or _bibcode_journal(bc) or ""
            title = str(meta.get("title") or "").strip() or bc
        arxiv = (r.get("local_arxiv_id") or "").strip() or _arxiv_from_bibcode(bc) or ""
        enriched.append(
            {
                "ref_bibcode": bc,
                "authors": authors,
                "year": year or None,
                "journal": journal or None,
                "title": title,
                "local_paper_id": r.get("local_paper_id"),
                "local_has_pdf": int(r.get("local_has_pdf") or 0),
                "arxiv_id": arxiv or None,
            }
        )
    return enriched


def _paper_summary(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    try:
        d["authors"] = json.loads(d.pop("authors_json") or "[]")
    except (json.JSONDecodeError, KeyError):
        d["authors"] = []
    d.pop("categories_json", None)
    d.pop("matched_keywords_json", None)
    d["has_pdf"] = bool((d.get("pdf_relpath") or "").strip())
    if "metadata_synced" in d:
        d["metadata_synced"] = bool(d.pop("metadata_synced"))
    if "pending_metadata" in d:
        d["pending_metadata"] = bool(d.pop("pending_metadata"))
    return d


@router.get("/papers")
def list_papers(
    q: Optional[str] = Query(None, description="FTS query on title+abstract"),
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
    journal: Optional[str] = Query(None, description="Bibcode journal fragment, e.g. ApJ"),
    tag: Optional[str] = None,
    missing_tag: Optional[str] = Query(
        None, description="Only papers that do not have this tag (e.g. metadata_synced)"
    ),
    has_pdf: Optional[bool] = None,
    source: Optional[str] = None,
    sort: str = Query("updated_at", enum=list(_SORTABLE)),
    order: str = Query("desc", enum=["asc", "desc"]),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Dict[str, Any]:
    where: List[str] = []
    params: List[Any] = []
    joins: List[str] = []

    if q and q.strip():
        from research_library.library.db import _fts_match_terms

        fts_q = _fts_match_terms(q.strip())
        if not fts_q:
            return {"total": 0, "limit": limit, "offset": offset, "items": []}
        joins.append("INNER JOIN papers_fts ON p.id = papers_fts.paper_id")
        where.append("papers_fts MATCH ?")
        params.append(fts_q)
    if year_from is not None:
        where.append("CAST(substr(p.published, 1, 4) AS INTEGER) >= ?")
        params.append(year_from)
    if year_to is not None:
        where.append("CAST(substr(p.published, 1, 4) AS INTEGER) <= ?")
        params.append(year_to)
    if journal and journal.strip():
        where.append("p.bibcode LIKE ?")
        params.append(f"____{journal.strip()}%")
    if tag and tag.strip():
        joins.append("INNER JOIN paper_tags t ON t.paper_id = p.id")
        where.append("t.tag_key = ?")
        params.append(tag.strip())
    if missing_tag and missing_tag.strip():
        where.append(
            """
            NOT EXISTS (
                SELECT 1 FROM paper_tags t_missing
                WHERE t_missing.paper_id = p.id AND t_missing.tag_key = ?
            )
            """
        )
        params.append(missing_tag.strip())
    if has_pdf is True:
        where.append("p.pdf_relpath IS NOT NULL AND TRIM(p.pdf_relpath) != ''")
    elif has_pdf is False:
        where.append("(p.pdf_relpath IS NULL OR TRIM(p.pdf_relpath) = '')")
    if source and source.strip():
        where.append("p.source = ?")
        params.append(source.strip())

    join_sql = " ".join(joins)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    order_sql = f"ORDER BY {_SORTABLE[sort]} {'ASC' if order == 'asc' else 'DESC'}"

    total = conn.execute(
        f"SELECT COUNT(DISTINCT p.id) FROM papers p {join_sql} {where_sql}",
        params,
    ).fetchone()[0]
    try:
        rows = conn.execute(
            f"""
            SELECT DISTINCT p.*,
                EXISTS (
                    SELECT 1 FROM paper_tags ts
                    WHERE ts.paper_id = p.id AND ts.tag_key = ?
                ) AS metadata_synced,
                EXISTS (
                    SELECT 1 FROM paper_tags tp
                    WHERE tp.paper_id = p.id AND tp.tag_key = ?
                ) AS pending_metadata
            FROM papers p {join_sql} {where_sql} {order_sql} LIMIT ? OFFSET ?
            """,
            [METADATA_SYNCED_TAG, PENDING_METADATA_TAG] + params + [limit, offset],
        ).fetchall()
    except sqlite3.OperationalError as e:
        if q and q.strip():
            raise HTTPException(status_code=400, detail=f"检索语法无效：{e}") from e
        raise

    return {
        "total": int(total),
        "limit": limit,
        "offset": offset,
        "items": [_paper_summary(r) for r in rows],
    }


@router.get("/papers/facets")
def paper_facets(conn: sqlite3.Connection = Depends(get_conn)) -> Dict[str, Any]:
    years = conn.execute(
        """
        SELECT substr(published, 1, 4) y, COUNT(*) n FROM papers
        WHERE published GLOB '[0-9][0-9][0-9][0-9]*'
        GROUP BY y ORDER BY y DESC
        """
    ).fetchall()
    sources = conn.execute(
        "SELECT source, COUNT(*) n FROM papers GROUP BY source ORDER BY n DESC"
    ).fetchall()
    tags = conn.execute(
        "SELECT tag_key, COUNT(DISTINCT paper_id) n FROM paper_tags GROUP BY tag_key ORDER BY n DESC"
    ).fetchall()
    from research_library.library.metadata_refresh import count_pending_metadata_sync
    from research_library.library.ingest_calibrate import count_pending_metadata

    return {
        "years": [{"year": r["y"], "count": r["n"]} for r in years],
        "sources": [{"source": r["source"], "count": r["n"]} for r in sources],
        "tags": [{"tag": r["tag_key"], "count": r["n"]} for r in tags],
        "pending_metadata_sync": count_pending_metadata_sync(conn),
        "metadata_synced_tag": METADATA_SYNCED_TAG,
        "pending_metadata": count_pending_metadata(conn),
        "pending_metadata_tag": PENDING_METADATA_TAG,
    }


@router.get("/papers/{paper_id}")
def get_paper(
    paper_id: int,
    enrich_ads: bool = Query(
        False,
        description="Fetch missing reference metadata from ADS (slow; off by default)",
    ),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Dict[str, Any]:
    row = library_db.get_paper_row(conn, paper_id)
    if not row:
        raise HTTPException(status_code=404, detail="paper not found")

    refs = conn.execute(
        """
        SELECT r.ref_bibcode, p2.id AS local_paper_id, p2.title AS local_title,
               p2.authors_json AS local_authors_json, p2.published AS local_published,
               p2.bibcode AS local_bibcode,
               p2.arxiv_id AS local_arxiv_id, p2.doi AS local_doi,
               CASE WHEN p2.pdf_relpath IS NOT NULL AND TRIM(p2.pdf_relpath) != '' THEN 1 ELSE 0 END AS local_has_pdf
        FROM paper_references r
        LEFT JOIN papers p2 ON p2.bibcode = r.ref_bibcode
        WHERE r.from_paper_id = ?
        ORDER BY r.ref_bibcode
        """,
        (paper_id,),
    ).fetchall()
    cited_by = conn.execute(
        """
        SELECT r.from_paper_id AS paper_id, p2.title
        FROM paper_references r
        INNER JOIN papers p ON p.id = ?
        INNER JOIN papers p2 ON p2.id = r.from_paper_id
        WHERE r.ref_bibcode = p.bibcode AND p.bibcode IS NOT NULL
        """,
        (paper_id,),
    ).fetchall()
    n_chunks = conn.execute(
        "SELECT COUNT(*) FROM paper_chunks WHERE paper_id = ?", (paper_id,)
    ).fetchone()[0]
    tags = conn.execute(
        "SELECT tag_key, tag_value FROM paper_tags WHERE paper_id = ? ORDER BY tag_key",
        (paper_id,),
    ).fetchall()
    zlink = conn.execute(
        "SELECT zotero_key, zotero_version, last_push_at FROM zotero_links WHERE paper_id = ?",
        (paper_id,),
    ).fetchone()

    tag_list = [dict(t) for t in tags]
    row["references"] = _enrich_references(refs, fetch_ads=enrich_ads)
    row["cited_by"] = [dict(r) for r in cited_by]
    row["chunks"] = int(n_chunks)
    row["tags"] = tag_list
    row["zotero"] = dict(zlink) if zlink else None
    row["pending_metadata"] = any(t["tag_key"] == PENDING_METADATA_TAG for t in tag_list)
    row["metadata_synced"] = any(t["tag_key"] == METADATA_SYNCED_TAG for t in tag_list)
    return row


class PaperPatch(BaseModel):
    title: Optional[str] = None
    abstract: Optional[str] = None
    authors: Optional[List[str]] = None
    published: Optional[str] = None
    doi: Optional[str] = None
    arxiv_id: Optional[str] = None
    bibcode: Optional[str] = None
    notes: Optional[str] = None


@router.patch("/papers/{paper_id}")
def patch_paper(
    paper_id: int,
    patch: PaperPatch,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Dict[str, Any]:
    row = library_db.get_paper_row(conn, paper_id)
    if not row:
        raise HTTPException(status_code=404, detail="paper not found")

    sets: List[str] = []
    params: List[Any] = []
    data = patch.model_dump(exclude_unset=True)
    if "authors" in data:
        sets.append("authors_json = ?")
        params.append(json.dumps(data.pop("authors") or [], ensure_ascii=False))
    for field in ("title", "abstract", "published", "doi", "arxiv_id", "bibcode", "notes"):
        if field in data:
            sets.append(f"{field} = ?")
            params.append(data[field])
    if not sets:
        return {"ok": True, "updated": False}

    sets.append("updated_at = ?")
    params.append(library_db._now_iso())
    params.append(paper_id)
    conn.execute(f"UPDATE papers SET {', '.join(sets)} WHERE id = ?", params)

    updated = library_db.get_paper_row(conn, paper_id)
    conn.execute("DELETE FROM papers_fts WHERE paper_id = ?", (paper_id,))
    conn.execute(
        "INSERT INTO papers_fts(paper_id, title, abstract) VALUES (?, ?, ?)",
        (paper_id, updated["title"] or "", updated["abstract"] or ""),
    )
    conn.commit()
    return {"ok": True, "updated": True, "paper": updated}


@router.get("/papers/{paper_id}/pdf-match")
def assess_pdf_match(
    paper_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Dict[str, Any]:
    from research_library.library.paper_pdf_match import assess_paper_pdf_match

    result = assess_paper_pdf_match(conn, paper_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error") or "assessment failed")
    return result


class SplitPdfBody(BaseModel):
    force: bool = False


@router.post("/papers/{paper_id}/split-pdf")
def split_pdf_route(
    paper_id: int,
    body: SplitPdfBody = SplitPdfBody(),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Dict[str, Any]:
    from research_library.library.paper_pdf_match import split_paper_pdf

    result = split_paper_pdf(conn, paper_id, force=body.force)
    if not result.get("ok"):
        err = str(result.get("error") or "split failed")
        code = 404 if "not found" in err else 422
        raise HTTPException(status_code=code, detail=err)
    return result


@router.post("/papers/{paper_id}/refresh-metadata")
def refresh_paper_metadata_route(
    paper_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Dict[str, Any]:
    from research_library.library.metadata_refresh import refresh_paper_metadata

    result = refresh_paper_metadata(conn, paper_id)
    if not result.get("ok"):
        err = str(result.get("error") or "refresh failed")
        code = 503 if "ADS_API_TOKEN" in err else 404 if "not found" in err else 422
        raise HTTPException(status_code=code, detail=err)
    return result


@router.post("/papers/{paper_id}/confirm-metadata")
def confirm_paper_metadata_route(
    paper_id: int,
    refresh_from_ads: bool = Query(True),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Dict[str, Any]:
    """Clear pending_metadata, refresh from ADS if possible, fetch ar5iv, re-embed."""
    from research_library.library.ingest_calibrate import confirm_pending_metadata

    result = confirm_pending_metadata(
        conn, paper_id, refresh_from_ads=refresh_from_ads
    )
    if not result.get("ok"):
        err = str(result.get("error") or "confirm failed")
        code = 503 if "ADS_API_TOKEN" in err else 404 if "not found" in err else 422
        raise HTTPException(status_code=code, detail=err)
    return result


@router.delete("/papers/{paper_id}")
def delete_paper(
    paper_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Dict[str, Any]:
    row = library_db.get_paper_row(conn, paper_id)
    if not row:
        raise HTTPException(status_code=404, detail="paper not found")
    conn.execute("DELETE FROM papers_fts WHERE paper_id = ?", (paper_id,))
    conn.execute("DELETE FROM paper_chunks_fts WHERE paper_id = ?", (paper_id,))
    conn.execute("DELETE FROM papers WHERE id = ?", (paper_id,))
    conn.commit()
    return {"ok": True, "deleted": paper_id}


class TagBody(BaseModel):
    tag_key: str
    tag_value: str = ""


@router.post("/papers/{paper_id}/tags")
def add_tag(
    paper_id: int,
    body: TagBody,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Dict[str, Any]:
    if not library_db.get_paper_row(conn, paper_id):
        raise HTTPException(status_code=404, detail="paper not found")
    key = body.tag_key.strip()
    if not key:
        raise HTTPException(status_code=422, detail="tag_key must not be empty")
    existing = conn.execute(
        "SELECT id FROM paper_tags WHERE paper_id = ? AND tag_key = ?",
        (paper_id, key),
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE paper_tags SET tag_value = ? WHERE id = ?",
            (body.tag_value, existing["id"]),
        )
    else:
        conn.execute(
            "INSERT INTO paper_tags(paper_id, tag_key, tag_value, source, created_at) "
            "VALUES (?, ?, ?, 'manual', ?)",
            (paper_id, key, body.tag_value, library_db._now_iso()),
        )
    conn.commit()
    return {"ok": True}


@router.delete("/papers/{paper_id}/tags/{tag_key}")
def remove_tag(
    paper_id: int,
    tag_key: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Dict[str, Any]:
    conn.execute(
        "DELETE FROM paper_tags WHERE paper_id = ? AND tag_key = ?",
        (paper_id, tag_key),
    )
    conn.commit()
    return {"ok": True}
