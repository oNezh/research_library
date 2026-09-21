"""PDF and full-text source file serving."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from research_library.config import get_data_dir
from research_library.library import db as library_db
from research_library.server.deps import get_conn

router = APIRouter(tags=["files"])


def _resolve_under_data_dir(relpath: str) -> Path:
    data_root = get_data_dir().resolve()
    p = (data_root / relpath).resolve()
    if not str(p).startswith(str(data_root)):
        raise HTTPException(status_code=400, detail="invalid path")
    return p


@router.get("/papers/{paper_id}/pdf")
def get_pdf(
    paper_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
):
    row = library_db.get_paper_row(conn, paper_id)
    if not row:
        raise HTTPException(status_code=404, detail="paper not found")
    rel = (row.get("pdf_relpath") or "").strip()
    if not rel:
        raise HTTPException(status_code=404, detail="paper has no PDF")
    p = _resolve_under_data_dir(rel)
    if not p.is_file():
        raise HTTPException(status_code=404, detail="PDF file missing on disk")
    return FileResponse(str(p), media_type="application/pdf", filename=p.name)


@router.get("/papers/{paper_id}/source")
def get_source(
    paper_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Dict[str, Any]:
    row = library_db.get_paper_row(conn, paper_id)
    if not row:
        raise HTTPException(status_code=404, detail="paper not found")
    src_dir = get_data_dir() / "sources" / str(paper_id)
    main_txt = src_dir / "main.txt"
    if not main_txt.is_file():
        raise HTTPException(status_code=404, detail="no full-text source for this paper")
    sections = src_dir / "sections.json"
    out: Dict[str, Any] = {
        "paper_id": paper_id,
        "text": main_txt.read_text(encoding="utf-8", errors="replace"),
    }
    if sections.is_file():
        import json

        try:
            out["sections"] = json.loads(sections.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            out["sections"] = None
    return out
