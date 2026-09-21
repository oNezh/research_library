"""Unified search: FTS, semantic (chunks) and related papers."""

from __future__ import annotations

import sqlite3
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Query

from research_library.library import db as library_db
from research_library.server.deps import get_conn

router = APIRouter(tags=["search"])


@router.get("/search")
def search(
    q: str = Query(..., min_length=1),
    mode: str = Query("fts", enum=["fts", "semantic"]),
    limit: int = Query(20, ge=1, le=100),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Dict[str, Any]:
    if mode == "fts":
        rows = library_db.search_fts(conn, q, limit=limit)
        return {"mode": "fts", "query": q, "results": rows}

    from research_library.library.semantic import semantic_search

    try:
        hits = semantic_search(conn, q, limit=limit)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"semantic search unavailable: {e}")
    return {"mode": "semantic", "query": q, "results": hits}


@router.get("/papers/{paper_id}/related")
def related(
    paper_id: int,
    limit: int = Query(8, ge=1, le=50),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Dict[str, Any]:
    if not library_db.get_paper_row(conn, paper_id):
        raise HTTPException(status_code=404, detail="paper not found")
    from research_library.library.semantic import get_related_papers

    try:
        items = get_related_papers(conn, paper_id, limit=limit)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"related papers unavailable: {e}")
    return {"paper_id": paper_id, "results": items}


@router.get("/search/remote")
def search_remote(
    q: str = Query(..., min_length=1),
    mode: str = Query("query", enum=["title", "query", "ref"]),
) -> Dict[str, Any]:
    from research_library import services

    if mode == "title":
        return services.lookup_title_search(q)
    if mode == "ref":
        return services.lookup_ref_search(q)
    return services.lookup_query_search(q)
