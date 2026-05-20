"""Smoke tests for the new WAL / busy_timeout / pdf_relpath index in db.connect."""

from __future__ import annotations

import os

import pytest

from research_library import config as _cfg
from research_library.library import db as library_db


@pytest.fixture()
def tmp_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_LIBRARY_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("RESEARCH_LIBRARY_DATA_DIR_RESOLVED", raising=False)
    yield tmp_path


def test_connect_enables_wal_and_busy_timeout(tmp_data_dir):
    conn = library_db.connect()
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        bt = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        sync = conn.execute("PRAGMA synchronous").fetchone()[0]
    finally:
        conn.close()
    assert str(mode).lower() == "wal"
    assert int(bt) >= 5000
    assert int(sync) in (1, 2)  # NORMAL=1, FULL=2


def test_pdf_relpath_index_created(tmp_data_dir):
    conn = library_db.connect()
    try:
        library_db.ensure_schema(conn)
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_papers_pdf_relpath'"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1


def test_paper_id_for_absolute_pdf_uses_index_path(tmp_data_dir):
    conn = library_db.connect()
    try:
        library_db.ensure_schema(conn)
        # create a fake PDF inside the data dir and a matching paper row
        pdf_dir = tmp_data_dir / "pdfs"
        pdf_dir.mkdir(parents=True, exist_ok=True)
        pdf = pdf_dir / "x.pdf"
        pdf.write_bytes(b"%PDF-1.4\n%%EOF")
        rel = "pdfs/x.pdf"
        pid = library_db.upsert_paper(
            conn,
            arxiv_id="9999.99999",
            title="x",
            pdf_relpath=rel,
            commit=False,
        )
        conn.commit()
        got = library_db.paper_id_for_absolute_pdf(conn, str(pdf))
    finally:
        conn.close()
    assert got == pid


def test_upsert_paper_commit_false_does_not_commit(tmp_data_dir):
    conn = library_db.connect()
    try:
        library_db.ensure_schema(conn)
        pid = library_db.upsert_paper(
            conn,
            arxiv_id="0000.00001",
            title="batch_t",
            commit=False,
        )
        # row visible in same connection but not yet persisted: rollback should drop it
        assert conn.execute("SELECT title FROM papers WHERE id = ?", (pid,)).fetchone()[0] == "batch_t"
        conn.rollback()
        row = conn.execute("SELECT id FROM papers WHERE arxiv_id = ?", ("0000.00001",)).fetchone()
    finally:
        conn.close()
    assert row is None
