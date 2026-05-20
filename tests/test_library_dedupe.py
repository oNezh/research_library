"""Tests for library.db.dedupe_papers."""

from __future__ import annotations

import sqlite3

from research_library.library import db


def _memory_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    db.init_schema(conn)
    db.ensure_schema(conn)
    return conn


def _ins(
    conn: sqlite3.Connection,
    *,
    bibcode: str | None,
    arxiv_id: str | None,
    pdf_relpath: str | None,
    title: str = "t",
) -> int:
    conn.execute(
        """
        INSERT INTO papers (
            arxiv_id, bibcode, title, abstract, authors_json, categories_json,
            matched_keywords_json, published, source, pdf_relpath, created_at, updated_at
        ) VALUES (?, ?, ?, '', '[]', '[]', '[]', NULL, 'test', ?, '2020-01-01', '2020-01-01')
        """,
        (arxiv_id, bibcode, title, pdf_relpath),
    )
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def test_dedupe_same_pdf_relpath_keeps_chunkier_row() -> None:
    conn = _memory_conn()
    p1 = _ins(
        conn,
        bibcode="2020ApJ...888...1A",
        arxiv_id=None,
        pdf_relpath="pdfs/foo.pdf",
    )
    p2 = _ins(
        conn,
        bibcode="2016MNRAS.463L..17A",
        arxiv_id=None,
        pdf_relpath="pdfs/foo.pdf",
    )
    conn.execute(
        """
        INSERT INTO paper_chunks (paper_id, chunk_ord, char_start, char_end, text, text_hash, created_at)
        VALUES (?, 0, 0, 10, 'hello', '', '2020-01-01')
        """,
        (p1,),
    )
    conn.execute(
        "INSERT INTO papers_fts(paper_id, title, abstract) VALUES (?,?,?)",
        (p1, "t", ""),
    )
    conn.execute(
        "INSERT INTO papers_fts(paper_id, title, abstract) VALUES (?,?,?)",
        (p2, "t2", ""),
    )
    conn.commit()

    out = db.dedupe_papers(conn, dry_run=False, chroma_delete=False)
    conn.commit()
    assert out["merge_operations"] == 1
    assert out["papers_removed"] == 1
    kept = int(
        conn.execute("SELECT id FROM papers WHERE pdf_relpath = ?", ("pdfs/foo.pdf",)).fetchone()[0]
    )
    assert kept == p1
    assert conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 1
    nch = conn.execute(
        "SELECT COUNT(*) FROM paper_chunks WHERE paper_id = ?", (p1,)
    ).fetchone()[0]
    assert int(nch) == 1


def test_dedupe_dry_run_no_delete() -> None:
    conn = _memory_conn()
    _ins(conn, bibcode="2021MNRAS.111..111A", arxiv_id=None, pdf_relpath="pdfs/a.pdf")
    _ins(conn, bibcode="2021MNRAS.222..222B", arxiv_id=None, pdf_relpath="pdfs/a.pdf")
    conn.commit()
    out = db.dedupe_papers(conn, dry_run=True, chroma_delete=False)
    assert out["papers_removed"] == 1
    assert out["merge_operations"] == 1
    assert conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 2


def test_dedupe_same_arxiv_normalized() -> None:
    conn = _memory_conn()
    _ins(
        conn,
        bibcode="2023ApJ...900...1X",
        arxiv_id="2301.05410",
        pdf_relpath="pdfs/x1.pdf",
    )
    _ins(
        conn,
        bibcode=None,
        arxiv_id="2301.05410v2",
        pdf_relpath="pdfs/x2.pdf",
    )
    conn.commit()
    out = db.dedupe_papers(conn, dry_run=False, chroma_delete=False)
    conn.commit()
    assert out["merge_operations"] == 1
    assert conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 1


def test_list_paper_ids_with_pdf_missing_chunks() -> None:
    conn = _memory_conn()
    a = _ins(conn, bibcode="2020ApJ...1A", arxiv_id=None, pdf_relpath="pdfs/a.pdf")
    b = _ins(conn, bibcode="2020ApJ...2B", arxiv_id=None, pdf_relpath="pdfs/b.pdf")
    conn.execute(
        """
        INSERT INTO paper_chunks (paper_id, chunk_ord, char_start, char_end, text, text_hash, created_at)
        VALUES (?, 0, 0, 3, 'x', '', '2020-01-01')
        """,
        (a,),
    )
    _ins(conn, bibcode=None, arxiv_id=None, pdf_relpath=None)
    conn.commit()
    missing = db.list_paper_ids_with_pdf_missing_chunks(conn)
    assert missing == [b]
