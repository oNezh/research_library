"""FastAPI application factory for the research companion desktop app."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Dict

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from research_library.app_settings import apply_to_environ
from research_library.config import (
    get_chroma_semantic_dir,
    get_data_dir,
    get_pdfs_dir,
    get_semantic_backend,
    load_env,
)
from research_library.settings import get_settings, reload_settings
from research_library.library import db as library_db
from research_library.server.deps import get_conn


def _embedding_token_status() -> bool:
    s = get_settings()
    provider = (s.embedding_provider or "").strip().lower()
    if not provider:
        qf = (s.llm_provider or "").strip().lower()
        if qf in ("openai", "openai_compat", "openai-compatible"):
            provider = "openai_compat"
        else:
            provider = "minimax"
    if provider == "local_sentence_transformer":
        return bool((s.local_embedding_model or "").strip())
    if provider in ("openai_compat", "openai", "openai-compatible"):
        return bool(
            (s.openai_api_key or "").strip() or (s.qf_llm_api_key or "").strip()
        )
    return bool((s.minimax_api_key or "").strip() or (s.qf_llm_api_key or "").strip())


def _token_status() -> Dict[str, bool]:
    s = get_settings()
    return {
        "ads": bool((s.ads_api_token or os.environ.get("ADS_API_TOKEN") or "").strip()),
        "zotero": bool(
            (s.zotero_library_id or os.environ.get("ZOTERO_LIBRARY_ID") or "").strip()
            and (s.zotero_api_key or os.environ.get("ZOTERO_API_KEY") or "").strip()
        ),
        "llm": bool(
            (s.qf_llm_api_key or os.environ.get("QF_LLM_API_KEY") or "").strip()
            or (s.minimax_api_key or os.environ.get("MINIMAX_API_KEY") or "").strip()
        ),
        "embedding": _embedding_token_status(),
    }


def _chroma_status() -> Dict[str, Any]:
    chroma_dir = get_chroma_semantic_dir()
    populated = any(chroma_dir.iterdir()) if chroma_dir.is_dir() else False
    return {
        "backend": get_semantic_backend(),
        "chroma_dir": str(chroma_dir),
        "populated": populated,
    }


def create_app() -> FastAPI:
    load_env()
    apply_to_environ()
    reload_settings()
    app = FastAPI(title="Research Companion", version="0.1.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def frontend_cache_control(request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path in ("", "/") or path.endswith("/index.html"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response

    @app.get("/api/health")
    def health(conn: sqlite3.Connection = Depends(get_conn)) -> Dict[str, Any]:
        st = library_db.stats(conn)
        chunks = conn.execute("SELECT COUNT(*) FROM paper_chunks").fetchone()[0]
        with_pdf = conn.execute(
            "SELECT COUNT(*) FROM papers WHERE pdf_relpath IS NOT NULL AND TRIM(pdf_relpath) != ''"
        ).fetchone()[0]
        return {
            "ok": True,
            "data_dir": str(get_data_dir()),
            "db_path": st["db_path"],
            "papers": st["papers"],
            "papers_with_pdf": int(with_pdf),
            "chunks": int(chunks),
            "last_updated": st["last_updated"],
            "pdfs_dir": str(get_pdfs_dir()),
            "semantic": _chroma_status(),
            "tokens": _token_status(),
            "llm_provider": get_settings().llm_provider or "minimax",
            "embedding_provider": get_settings().embedding_provider or "minimax",
        }

    from research_library.server.routes import files, jobs, manage, papers, research, search, settings

    app.include_router(papers.router, prefix="/api")
    app.include_router(search.router, prefix="/api")
    app.include_router(files.router, prefix="/api")
    app.include_router(jobs.router, prefix="/api")
    app.include_router(research.router, prefix="/api")
    app.include_router(manage.router, prefix="/api")
    app.include_router(settings.router, prefix="/api")

    _mount_frontend(app)
    return app


def _mount_frontend(app: FastAPI) -> None:
    """Serve the built frontend (frontend/dist) if present."""
    from research_library.config import find_repo_root

    root = find_repo_root()
    if root is None:
        return
    dist = root / "frontend" / "dist"
    if not (dist / "index.html").is_file():
        return
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=str(dist), html=True), name="frontend")


def serve(host: str = "127.0.0.1", port: int = 8230, reload: bool = False) -> None:
    import uvicorn

    if reload:
        uvicorn.run(
            "research_library.server.app:create_app",
            factory=True,
            host=host,
            port=port,
            reload=True,
            reload_dirs=[str(Path(__file__).resolve().parents[1])],
        )
    else:
        uvicorn.run(create_app(), host=host, port=port)
