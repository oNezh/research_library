"""Background job framework: SQLite-backed job table + thread pool workers.

All heavy library functions are synchronous, so jobs run in a small
ThreadPoolExecutor. Each worker opens its own SQLite connection. Progress is
written to the ``jobs`` table so SSE streaming works by polling the table --
robust across threads and process restarts (interrupted jobs surface as
``running`` rows that never finish; they are marked stale on startup).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Optional

from research_library.library import db as library_db

JobHandler = Callable[[sqlite3.Connection, Dict[str, Any], "ProgressReporter"], Dict[str, Any]]

_JOBS_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'queued',
    progress REAL NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    result_json TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
"""


def ensure_jobs_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_JOBS_SCHEMA)
    conn.commit()


def mark_stale_running_jobs(conn: sqlite3.Connection) -> int:
    """Jobs left 'running' from a previous process are unrecoverable."""
    ensure_jobs_schema(conn)
    cur = conn.execute(
        "UPDATE jobs SET status = 'error', error = 'interrupted (server restart)', "
        "updated_at = ? WHERE status IN ('queued', 'running')",
        (library_db._now_iso(),),
    )
    conn.commit()
    return cur.rowcount


class ProgressReporter:
    """Thread-safe progress writer bound to one job id."""

    def __init__(self, job_id: int):
        self.job_id = job_id

    def __call__(self, progress: float, message: str = "") -> None:
        conn = library_db.connect()
        try:
            conn.execute(
                "UPDATE jobs SET progress = ?, message = ?, updated_at = ? WHERE id = ?",
                (
                    max(0.0, min(100.0, float(progress))),
                    message[:500],
                    library_db._now_iso(),
                    self.job_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()


class JobManager:
    def __init__(self, max_workers: int = 2):
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="job")
        self._handlers: Dict[str, JobHandler] = {}
        self._lock = threading.Lock()

    def register(self, kind: str, handler: JobHandler) -> None:
        with self._lock:
            self._handlers[kind] = handler

    @property
    def kinds(self) -> list:
        return sorted(self._handlers)

    def submit(self, kind: str, params: Dict[str, Any]) -> int:
        if kind not in self._handlers:
            raise ValueError(f"unknown job kind: {kind}")
        conn = library_db.connect()
        try:
            ensure_jobs_schema(conn)
            now = library_db._now_iso()
            cur = conn.execute(
                "INSERT INTO jobs(kind, params_json, status, created_at, updated_at) "
                "VALUES (?, ?, 'queued', ?, ?)",
                (kind, json.dumps(params, ensure_ascii=False), now, now),
            )
            job_id = int(cur.lastrowid)
            conn.commit()
        finally:
            conn.close()
        self._executor.submit(self._run, job_id, kind, params)
        return job_id

    def _run(self, job_id: int, kind: str, params: Dict[str, Any]) -> None:
        handler = self._handlers[kind]
        conn = library_db.connect()
        try:
            library_db.ensure_schema(conn)
            conn.execute(
                "UPDATE jobs SET status = 'running', updated_at = ? WHERE id = ?",
                (library_db._now_iso(), job_id),
            )
            conn.commit()
            reporter = ProgressReporter(job_id)
            result = handler(conn, params, reporter)
            conn.execute(
                "UPDATE jobs SET status = 'done', progress = 100, result_json = ?, "
                "updated_at = ? WHERE id = ?",
                (json.dumps(result, ensure_ascii=False, default=str), library_db._now_iso(), job_id),
            )
            conn.commit()
        except Exception as e:
            err = f"{type(e).__name__}: {e}\n{traceback.format_exc()[-2000:]}"
            try:
                conn.execute(
                    "UPDATE jobs SET status = 'error', error = ?, updated_at = ? WHERE id = ?",
                    (err, library_db._now_iso(), job_id),
                )
                conn.commit()
            except Exception:
                pass
        finally:
            conn.close()


def get_job(conn: sqlite3.Connection, job_id: int) -> Optional[Dict[str, Any]]:
    ensure_jobs_schema(conn)
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["params"] = json.loads(d.pop("params_json") or "{}")
    except json.JSONDecodeError:
        d["params"] = {}
    rj = d.pop("result_json", None)
    if rj:
        try:
            d["result"] = json.loads(rj)
        except json.JSONDecodeError:
            d["result"] = None
    else:
        d["result"] = None
    return d


def list_jobs(
    conn: sqlite3.Connection,
    *,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> Dict[str, Any]:
    ensure_jobs_schema(conn)
    where = ""
    params: list = []
    if status:
        where = "WHERE status = ?"
        params.append(status)
    total = conn.execute(f"SELECT COUNT(*) FROM jobs {where}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT id, kind, status, progress, message, error, params_json, created_at, updated_at "
        f"FROM jobs {where} ORDER BY id DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        try:
            d["params"] = json.loads(d.pop("params_json") or "{}")
        except json.JSONDecodeError:
            d["params"] = {}
        items.append(d)
    return {"total": int(total), "items": items}


# ---------------------------------------------------------------------------
# Handlers (thin wrappers around existing library functions)
# ---------------------------------------------------------------------------


def _handle_semantic_index(conn, params, report: ProgressReporter) -> Dict[str, Any]:
    from research_library.library.semantic import index_papers

    paper_ids = params.get("paper_ids")
    report(5, "indexing chunks")
    out = index_papers(conn, paper_ids, force=bool(params.get("force")))
    conn.commit()
    return out


def _handle_topic_dossier(conn, params, report: ProgressReporter) -> Dict[str, Any]:
    from research_library.library.topic_dossier import build_topic_dossier

    topic = str(params.get("topic") or "").strip()
    if not topic:
        raise ValueError("topic is required")
    report(10, "gathering chunks")
    out = build_topic_dossier(
        conn,
        topic,
        extra_queries=params.get("extra_queries"),
        per_query_limit=int(params.get("per_query_limit") or 12),
        synthesize=bool(params.get("synthesize", True)),
    )
    return out


def _handle_semantic_report(conn, params, report: ProgressReporter) -> Dict[str, Any]:
    from research_library.library.report import build_semantic_report

    query = str(params.get("query") or "").strip()
    if not query:
        raise ValueError("query is required")
    report(10, "gathering chunks and references")
    out = build_semantic_report(
        conn,
        query,
        extra_queries=params.get("extra_queries"),
        expand_queries=bool(params.get("expand_queries")),
        per_query_limit=int(params.get("per_query_limit") or 12),
        synthesize=bool(params.get("synthesize", True)),
    )
    return out


def _handle_reference_chain(conn, params, report: ProgressReporter) -> Dict[str, Any]:
    from research_library.analysis.pdf import analyze_pdf_reference_chain
    from research_library.config import get_data_dir

    paper_id = params.get("paper_id")
    pdf_path = params.get("pdf_path")
    if paper_id and not pdf_path:
        row = library_db.get_paper_row(conn, int(paper_id))
        if not row:
            raise ValueError(f"paper {paper_id} not found")
        rel = (row.get("pdf_relpath") or "").strip()
        if not rel:
            raise ValueError(f"paper {paper_id} has no PDF")
        pdf_path = str(get_data_dir() / rel)
    if not pdf_path:
        raise ValueError("paper_id or pdf_path is required")
    question = str(params.get("question") or "").strip()
    if not question:
        raise ValueError("question is required")
    report(5, "starting reference chain")
    out = analyze_pdf_reference_chain(
        pdf_path,
        question,
        max_hops=int(params.get("max_hops") or 2),
    )
    return out


def _handle_citation_sync(conn, params, report: ProgressReporter) -> Dict[str, Any]:
    from research_library.library.citations import sync_references_from_ads

    paper_ids = params.get("paper_ids")
    report(5, "syncing ADS reference lists")
    out = sync_references_from_ads(
        conn,
        paper_ids=set(paper_ids) if paper_ids else None,
        missing_only=bool(params.get("missing_only", True)),
    )
    conn.commit()
    return out


def _handle_zotero_sync(conn, params, report: ProgressReporter) -> Dict[str, Any]:
    from research_library.library import zotero_sync as zs

    report(5, "zotero sync (link -> pull -> push)")
    out = zs.sync_all(
        conn,
        full=bool(params.get("full")),
        with_pdf=bool(params.get("with_pdf", False)),
        do_index=bool(params.get("do_index", False)),
    )
    return out


def _handle_ingest_pdf(conn, params, report: ProgressReporter) -> Dict[str, Any]:
    from research_library.library.pdf_ingest import ingest_pdf_file

    pdf_path = str(params.get("pdf_path") or "").strip()
    if not pdf_path:
        raise ValueError("pdf_path is required")
    report(10, "extracting identifiers and matching ADS")
    out = ingest_pdf_file(conn, pdf_path)
    conn.commit()
    return out


def _handle_ingest_hubs(conn, params, report: ProgressReporter) -> Dict[str, Any]:
    from research_library.library.citations import ingest_hub_bibcodes

    bibcodes = params.get("bibcodes") or []
    if not bibcodes:
        raise ValueError("bibcodes is required")
    report(10, f"ingesting {len(bibcodes)} hub records from ADS")
    out = ingest_hub_bibcodes(conn, bibcodes)
    conn.commit()
    return out


def _handle_ingest_ref(conn, params, report: ProgressReporter) -> Dict[str, Any]:
    """Resolve a reference line / DOI / arXiv / bibcode, download PDF, upsert DB."""
    import os
    import re

    from research_library.config import get_pdfs_dir
    from research_library.library.reference_acquire import acquire_pdf
    from research_library.library.reference_ingest import ingest_downloaded_reference
    from research_library.library.reference_parse import parse_catalog_line

    line = str(params.get("text") or "").strip()
    if not line:
        raise ValueError("text is required")
    report(10, "resolving reference in ADS")
    ref = parse_catalog_line(line, conn, use_ads=True)
    if not ref.bibcode and not ref.arxiv_id:
        return {
            "ok": False,
            "error": "could not resolve bibcode or arXiv id",
            "resolution_note": ref.resolution_note,
        }
    key = ref.bibcode or ref.arxiv_id or "paper"
    safe_name = re.sub(r"[^\w\-]", "_", str(key))
    dest = os.path.join(str(get_pdfs_dir()), f"{safe_name}.pdf")
    report(40, "downloading PDF")
    path, acquire_reason = acquire_pdf(ref, conn, dest, timeout=int(params.get("timeout") or 120))
    if not path:
        return {
            "ok": False,
            "error": "acquire_pdf failed",
            "reason": acquire_reason,
            "bibcode": ref.bibcode,
        }
    report(80, "ingesting into library")
    meta = ingest_downloaded_reference(
        conn, ref, path, source="app_ingest_ref", acquire_reason=acquire_reason
    )
    conn.commit()
    return {
        "ok": bool(meta.get("ok")),
        "pdf_path": path,
        "acquire_reason": acquire_reason,
        "bibcode": ref.bibcode,
        "ingest": meta,
    }


def _handle_zotero_backfill(conn, params, report: ProgressReporter) -> Dict[str, Any]:
    from research_library.library import zotero_sync as zs

    report(5, "backfilling Unknown-author papers from ADS")
    out = zs.backfill_unknown(
        conn,
        delay=float(params.get("delay") or 1.0),
        push_zotero=bool(params.get("push_zotero", True)),
    )
    return out


def _handle_arxiv_scan(conn, params, report: ProgressReporter) -> Dict[str, Any]:
    from research_library.arxiv_keywords import run as arxiv_run

    before = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    report(10, "scanning arXiv for keyword matches")
    arxiv_run(
        category=str(params.get("category") or "all"),
        days_back=int(params.get("days_back") or 7),
        persist_db=True,
    )
    after_conn = library_db.connect()
    try:
        after = after_conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    finally:
        after_conn.close()
    return {"ok": True, "papers_before": int(before), "papers_after": int(after), "added": int(after - before)}


def _handle_pdf_metadata_audit(conn, params, report: ProgressReporter) -> Dict[str, Any]:
    from research_library.library.paper_pdf_match import audit_pdf_metadata_matches

    report(2, "auditing PDF/metadata matches")
    return audit_pdf_metadata_matches(
        conn,
        limit=int(params["limit"]) if params.get("limit") else None,
        report=report,
    )


def _handle_metadata_sync(conn, params, report: ProgressReporter) -> Dict[str, Any]:
    from research_library.library.metadata_refresh import refresh_pending_metadata

    report(2, "listing papers missing metadata_synced tag")
    out = refresh_pending_metadata(
        conn,
        delay=float(params.get("delay") or 1.0),
        limit=int(params["limit"]) if params.get("limit") else None,
        report=report,
    )
    return out


def build_default_manager(max_workers: int = 2) -> JobManager:
    mgr = JobManager(max_workers=max_workers)
    mgr.register("semantic_index", _handle_semantic_index)
    mgr.register("topic_dossier", _handle_topic_dossier)
    mgr.register("semantic_report", _handle_semantic_report)
    mgr.register("reference_chain", _handle_reference_chain)
    mgr.register("citation_sync", _handle_citation_sync)
    mgr.register("zotero_sync", _handle_zotero_sync)
    mgr.register("zotero_backfill", _handle_zotero_backfill)
    mgr.register("metadata_sync", _handle_metadata_sync)
    mgr.register("pdf_metadata_audit", _handle_pdf_metadata_audit)
    mgr.register("ingest_pdf", _handle_ingest_pdf)
    mgr.register("ingest_hubs", _handle_ingest_hubs)
    mgr.register("ingest_ref", _handle_ingest_ref)
    mgr.register("arxiv_scan", _handle_arxiv_scan)
    return mgr
