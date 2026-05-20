"""Semantic pipeline: FTS-only (no Chroma) or Chroma + fake embedding vectors."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

pytest.importorskip("fitz")

import fitz

from research_library.library import db
from research_library.library.semantic import (
    chunk_text,
    get_related_papers,
    get_semantic_collection,
    index_paper,
    semantic_search,
)


class FakeEmb:
    embedding_dim = 8

    def embed_texts(self, texts, *, model=None, for_query=False):
        out = []
        for t in texts:
            h = hashlib.sha256(t.encode()).digest()
            vec = [((h[i % 32] + i) % 97) / 97.0 for i in range(self.embedding_dim)]
            out.append(vec)
        return out


def test_chunk_text_overlap():
    triples = chunk_text("x" * 3000, size=900, overlap=120)
    assert len(triples) >= 4
    assert triples[0][0] == 0


def test_index_search_related_offline(tmp_path: Path, monkeypatch):
    pytest.importorskip("chromadb")
    monkeypatch.setenv("RESEARCH_LIBRARY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RESEARCH_SEMANTIC_BACKEND", "vector")
    root = tmp_path.resolve()
    (root / "pdfs").mkdir(parents=True, exist_ok=True)
    pdf_path = root / "pdfs" / "off.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "IMF slope Salpeter 2.35 stellar masses.")
    page.insert_text((72, 100), "LMC extinction maps and photometry.")
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
        ) VALUES (?,?,?,?, '[]','[]','[]',?, 't', ?, ?, ?)
        """,
        ("o1", "2026off.......Z", "P1", "A", None, "pdfs/off.pdf", now, now),
    )
    p1 = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.execute(
        "INSERT INTO papers_fts(paper_id, title, abstract) VALUES (?,?,?)",
        (p1, "P1", "A"),
    )
    conn.commit()
    shutil.copy(pdf_path, root / "pdfs" / "off2.pdf")
    conn.execute(
        """
        INSERT INTO papers (
            arxiv_id, bibcode, title, abstract, authors_json, categories_json,
            matched_keywords_json, published, source, pdf_relpath, created_at, updated_at
        ) VALUES (?,?,?,?, '[]','[]','[]',?, 't', ?, ?, ?)
        """,
        ("o2", "2026off2......Z", "P2", "B", None, "pdfs/off2.pdf", now, now),
    )
    p2 = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.execute(
        "INSERT INTO papers_fts(paper_id, title, abstract) VALUES (?,?,?)",
        (p2, "P2", "B"),
    )
    conn.commit()

    fe = FakeEmb()
    col = get_semantic_collection()
    assert index_paper(conn, p1, embed_client=fe, force=True, collection=col)["ok"]
    assert index_paper(conn, p2, embed_client=fe, force=True, collection=col)["ok"]

    hits = semantic_search(conn, "Salpeter IMF", limit=3, embed_client=fe, collection=col)
    assert hits and hits[0]["paper_id"] in (p1, p2)
    rel = get_related_papers(conn, p1, limit=3, embed_client=fe, collection=col)
    assert p2 in {r["paper_id"] for r in rel}


def test_fts_backend_chunks_without_chroma(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("RESEARCH_LIBRARY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RESEARCH_SEMANTIC_BACKEND", "fts")
    root = tmp_path.resolve()
    (root / "pdfs").mkdir(parents=True, exist_ok=True)
    pdf_path = root / "pdfs" / "fts.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "IMF slope Salpeter 2.35 stellar masses.")
    page.insert_text((72, 100), "LMC extinction maps and photometry.")
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
        ) VALUES (?,?,?,?, '[]','[]','[]',?, 't', ?, ?, ?)
        """,
        ("f1", "2026fts1......Z", "Salpeter IMF paper", "Initial mass function.", None, "pdfs/fts.pdf", now, now),
    )
    p1 = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.execute(
        "INSERT INTO papers_fts(paper_id, title, abstract) VALUES (?,?,?)",
        (p1, "Salpeter IMF paper", "Initial mass function."),
    )
    conn.commit()
    shutil.copy(pdf_path, root / "pdfs" / "fts2.pdf")
    conn.execute(
        """
        INSERT INTO papers (
            arxiv_id, bibcode, title, abstract, authors_json, categories_json,
            matched_keywords_json, published, source, pdf_relpath, created_at, updated_at
        ) VALUES (?,?,?,?, '[]','[]','[]',?, 't', ?, ?, ?)
        """,
        ("f2", "2026fts2......Z", "LMC dust maps", "Extinction in Magellanic Clouds.", None, "pdfs/fts2.pdf", now, now),
    )
    p2 = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.execute(
        "INSERT INTO papers_fts(paper_id, title, abstract) VALUES (?,?,?)",
        (p2, "LMC dust maps", "Extinction in Magellanic Clouds."),
    )
    conn.commit()

    assert index_paper(conn, p1, force=True)["ok"]
    assert index_paper(conn, p2, force=True)["ok"]
    hits = semantic_search(conn, "Salpeter IMF", limit=5)
    assert hits and hits[0]["paper_id"] in (p1, p2)
    rel = get_related_papers(conn, p1, limit=4)
    assert p2 in {r["paper_id"] for r in rel}
