"""Tests for library.report (reference bundling + formatting; no LLM)."""

from __future__ import annotations

import sqlite3

from research_library.library import db
from research_library.library.report import (
    enrich_chunks_with_references,
    _format_sources_for_llm,
)


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    db.init_schema(c)
    db.ensure_schema(c)
    return c


def test_enrich_chunks_with_references() -> None:
    conn = _conn()
    conn.execute(
        """
        INSERT INTO papers (
            id, arxiv_id, bibcode, title, abstract, authors_json, categories_json,
            matched_keywords_json, published, source, pdf_relpath, created_at, updated_at
        ) VALUES
        (1, NULL, '2020ApJ...900...1A', 'Main', '', '[]', '[]', '[]', NULL, 't', NULL, 'a', 'a'),
        (2, '2001.00001', '2019MNRAS.100..100Z', 'Cited target', '', '[]', '[]', '[]', NULL, 't', NULL, 'a', 'a')
        """
    )
    conn.execute(
        "INSERT INTO paper_references (from_paper_id, ref_bibcode, created_at) VALUES (1, '2019MNRAS.100..100Z', 'a')"
    )
    conn.commit()

    chunks = [
        {
            "paper_id": 1,
            "chunk_id": 10,
            "bibcode": "2020ApJ...900...1A",
            "snippet": "excerpt one",
            "distance": 0.1,
            "title": "Main",
        }
    ]
    out = enrich_chunks_with_references(conn, chunks, ref_limit_per_paper=10)
    assert len(out) == 1
    assert len(out[0]["references"]) == 1
    assert out[0]["references"][0]["ref_bibcode"] == "2019MNRAS.100..100Z"
    assert out[0]["references"][0]["to_paper_id"] == 2


def test_format_sources_includes_tags_and_refs() -> None:
    chunks = [
        {
            "paper_id": 7,
            "chunk_id": 3,
            "bibcode": "2021A&A...1....1A",
            "arxiv_id": "2101.00001",
            "title": "T",
            "snippet": "body",
            "distance": 0.2,
            "matched_query": "q",
            "references": [{"ref_bibcode": "2000AJ....99...99B", "title": "Other", "in_library": False}],
        }
    ]
    text, idx = _format_sources_for_llm(chunks, 50_000)
    assert "### S1 — retrieved excerpt" in text
    assert "2101.00001" in text
    assert "R1:" in text
    assert "2000AJ....99...99B" in text
    assert idx == [
        {
            "tag": "S1",
            "paper_id": 7,
            "chunk_id": 3,
            "bibcode": "2021A&A...1....1A",
            "arxiv_id": "2101.00001",
            "title": "T",
        }
    ]
