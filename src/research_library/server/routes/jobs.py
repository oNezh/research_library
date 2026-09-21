"""Job endpoints: create, query, list, SSE progress stream, PDF upload."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from research_library.library import db as library_db
from research_library.server import jobs as jobs_mod
from research_library.server.deps import get_conn

router = APIRouter(tags=["jobs"])

_manager: Optional[jobs_mod.JobManager] = None


def get_manager() -> jobs_mod.JobManager:
    global _manager
    if _manager is None:
        _manager = jobs_mod.build_default_manager()
        conn = library_db.connect()
        try:
            jobs_mod.mark_stale_running_jobs(conn)
        finally:
            conn.close()
    return _manager


class JobCreate(BaseModel):
    kind: str
    params: Dict[str, Any] = {}


@router.get("/jobs/kinds")
def job_kinds() -> Dict[str, Any]:
    return {"kinds": get_manager().kinds}


@router.post("/jobs")
def create_job(body: JobCreate) -> Dict[str, Any]:
    mgr = get_manager()
    try:
        job_id = mgr.submit(body.kind, body.params)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"ok": True, "job_id": job_id}


@router.get("/jobs")
def list_jobs(
    status: Optional[str] = Query(None, enum=["queued", "running", "done", "error"]),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Dict[str, Any]:
    return jobs_mod.list_jobs(conn, status=status, limit=limit, offset=offset)


@router.get("/jobs/{job_id}")
def get_job(
    job_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Dict[str, Any]:
    job = jobs_mod.get_job(conn, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@router.get("/jobs/{job_id}/events")
async def job_events(job_id: int):
    """SSE stream polling job state until it reaches a terminal status."""

    def _fetch() -> Optional[Dict[str, Any]]:
        conn = library_db.connect()
        try:
            return jobs_mod.get_job(conn, job_id)
        finally:
            conn.close()

    first = await asyncio.to_thread(_fetch)
    if not first:
        raise HTTPException(status_code=404, detail="job not found")

    async def stream():
        last_payload = None
        while True:
            job = await asyncio.to_thread(_fetch)
            if not job:
                break
            payload = {
                "id": job["id"],
                "status": job["status"],
                "progress": job["progress"],
                "message": job["message"],
            }
            s = json.dumps(payload, ensure_ascii=False)
            if s != last_payload:
                last_payload = s
                yield {"event": "progress", "data": s}
            if job["status"] in ("done", "error"):
                yield {
                    "event": "end",
                    "data": json.dumps(
                        {"status": job["status"], "error": job.get("error")},
                        ensure_ascii=False,
                    ),
                }
                break
            await asyncio.sleep(0.8)

    return EventSourceResponse(stream())


@router.post("/upload/pdf")
async def upload_pdf(file: UploadFile) -> Dict[str, Any]:
    """Save an uploaded PDF to a temp location and start an ingest job."""
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(status_code=422, detail="only .pdf files are accepted")
    tmp_dir = Path(tempfile.gettempdir()) / "research_companion_uploads"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(file.filename).name
    dest = tmp_dir / safe_name
    content = await file.read()
    dest.write_bytes(content)
    job_id = get_manager().submit("ingest_pdf", {"pdf_path": str(dest)})
    return {"ok": True, "job_id": job_id, "saved_to": str(dest)}
