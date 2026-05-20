"""
Live semantic tests (MiniMax embeddings via QF_LLM_* / MINIMAX_* — same as chat client).

Skip when no API key or optional deps missing. Set RESEARCH_EMBEDDING_MODEL to a model
your account exposes on the OpenAI-compatible /v1/embeddings endpoint.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

pytest.importorskip("chromadb")
pytest.importorskip("fitz")

from research_library.library import db
from research_library.library.semantic import (
    get_related_papers,
    get_semantic_collection,
    index_paper,
    semantic_search,
)
from research_library.analysis.embeddings import get_embedding_client
from research_library.analysis.llm.base import LLMError


def _has_minimax_key() -> bool:
    return bool(
        (os.environ.get("QF_LLM_API_KEY") or os.environ.get("MINIMAX_API_KEY") or "").strip()
    )


def test_minimax_embedding_client_matches_qf_env():
    """Uses ``get_embedding_client('minimax')`` → ``MiniMaxOpenAIEmbeddings.from_env()`` (QF_LLM_* first)."""
    if not _has_minimax_key():
        pytest.skip("Set QF_LLM_API_KEY or MINIMAX_API_KEY for live MiniMax test")
    ec = get_embedding_client("minimax")
    try:
        vecs = ec.embed_texts(
            ["金属丰度与恒星形成", "LMC reddening and extinction"],
        )
    except LLMError as e:
        msg = str(e).lower()
        if "1008" in str(e) or "balance" in msg or "insufficient" in msg:
            pytest.skip(f"MiniMax account / billing: {e}")
        raise
    assert len(vecs) == 2
    assert len(vecs[0]) == len(vecs[1]) > 8
    assert ec.embedding_dim == len(vecs[0])


def test_semantic_index_search_related_with_real_embeddings(tmp_path: Path):
    if not _has_minimax_key():
        pytest.skip("Set QF_LLM_API_KEY or MINIMAX_API_KEY for live MiniMax test")
    import fitz

    root = tmp_path.resolve()
    os.environ["RESEARCH_LIBRARY_DATA_DIR"] = str(root)

    (root / "pdfs").mkdir(parents=True, exist_ok=True)
    pdf_path = root / "pdfs" / "live_semantic.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text(
        (72, 72),
        "The Salpeter IMF has slope 2.35. Low-mass stars dominate the stellar mass budget.",
    )
    page.insert_text(
        (72, 110),
        "LMC star clusters show age spreads. Photometry requires careful extinction corrections.",
    )
    doc.save(str(pdf_path))
    doc.close()

    conn = db.connect()
    db.init_schema(conn)
    now = db._now_iso()  # noqa: SLF001
    conn.execute(
        """
        INSERT INTO papers (
            arxiv_id, bibcode, title, abstract, authors_json, categories_json,
            matched_keywords_json, published, source, pdf_relpath, created_at, updated_at
        ) VALUES (?,?,?,?, '[]','[]','[]',?, 'live_test', ?, ?, ?)
        """,
        (
            "live-semantic-arxiv-1",
            "2026Livetest....Z",
            "Live semantic A",
            "Abstract A",
            None,
            "pdfs/live_semantic.pdf",
            now,
            now,
        ),
    )
    pid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.execute(
        "INSERT INTO papers_fts(paper_id, title, abstract) VALUES (?,?,?)",
        (pid, "Live semantic A", "Abstract A"),
    )
    conn.commit()

    shutil.copy(pdf_path, root / "pdfs" / "live_semantic_b.pdf")
    conn.execute(
        """
        INSERT INTO papers (
            arxiv_id, bibcode, title, abstract, authors_json, categories_json,
            matched_keywords_json, published, source, pdf_relpath, created_at, updated_at
        ) VALUES (?,?,?,?, '[]','[]','[]',?, 'live_test', ?, ?, ?)
        """,
        (
            "live-semantic-arxiv-2",
            "2026Livetest2...Z",
            "Live semantic B",
            "Abstract B",
            None,
            "pdfs/live_semantic_b.pdf",
            now,
            now,
        ),
    )
    pid2 = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.execute(
        "INSERT INTO papers_fts(paper_id, title, abstract) VALUES (?,?,?)",
        (pid2, "Live semantic B", "Abstract B"),
    )
    conn.commit()

    ec = get_embedding_client("minimax")
    col = get_semantic_collection()
    try:
        r1 = index_paper(conn, pid, embed_client=ec, force=True, collection=col)
        r2 = index_paper(conn, pid2, embed_client=ec, force=True, collection=col)
    except LLMError as e:
        msg = str(e).lower()
        if "1008" in str(e) or "balance" in msg or "insufficient" in msg:
            pytest.skip(f"MiniMax account / billing: {e}")
        raise
    assert r1.get("ok") and r2.get("ok"), (r1, r2)

    hits = semantic_search(
        conn,
        "Salpeter initial mass function slope",
        limit=5,
        embed_client=ec,
        collection=col,
    )
    assert hits, "no hits from semantic_search"
    best_pid = hits[0]["paper_id"]
    assert best_pid in (pid, pid2)

    rel = get_related_papers(conn, pid, limit=4, embed_client=ec, collection=col)
    other = {r["paper_id"] for r in rel}
    assert pid2 in other, f"expected related paper {pid2}, got {other}"
