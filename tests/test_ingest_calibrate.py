"""Tests for calibrated PDF ingest confidence / pending metadata."""

from __future__ import annotations

import pytest

from research_library.library import db as library_db
from research_library.library import ingest_calibrate as ic
from research_library.library import pdf_ingest as ping


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_LIBRARY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESEARCH_INGEST_AUTO_SEMANTIC_INDEX", "0")
    c = library_db.connect()
    library_db.ensure_schema(c)
    return c


def test_assess_strong_id_confident():
    out = ic.assess_ingest_confidence(
        {"arxiv_id": "2001.00001", "title_candidate": "Anything"},
        {
            "ok": True,
            "match_method": "arxiv",
            "doc": {"title": ["Real Title Here"], "bibcode": "2020ApJ...1T"},
        },
    )
    assert out["confident"] is True


def test_assess_weak_title_not_confident():
    out = ic.assess_ingest_confidence(
        {"title_candidate": "Totally different words here"},
        {
            "ok": True,
            "match_method": "title",
            "doc": {"title": ["Galactic dynamics and dark matter halos"]},
        },
    )
    assert out["confident"] is False
    assert out["reason"] == "title_weak"


def test_confirm_requires_identifiers(conn):
    pid = ic.create_pending_pdf_paper(
        conn,
        pdf_relpath="pdfs/x.pdf",
        extracted={},
        reason="test",
    )
    out = ic.confirm_pending_metadata(conn, pid, refresh_from_ads=False)
    assert out["ok"] is False
    assert "title" in (out.get("error") or "")


def test_confirm_manual_fields_clears_pending(conn, monkeypatch):
    pid = ic.create_pending_pdf_paper(
        conn,
        pdf_relpath="pdfs/x.pdf",
        extracted={"arxiv_id": "2001.00001"},
        reason="test",
    )
    conn.execute(
        "UPDATE papers SET title = ?, bibcode = ? WHERE id = ?",
        ("A Real Confirmed Title", "2020ApJ...888...10T", pid),
    )
    conn.commit()

    monkeypatch.setattr(
        ic,
        "after_confident_ingest",
        lambda *_a, **_k: {"paper_id": pid, "semantic_index": {"ok": True, "skipped": True}},
    )
    out = ic.confirm_pending_metadata(conn, pid, refresh_from_ads=False)
    assert out["ok"] is True
    assert ic.has_pending_metadata(conn, pid) is False


def test_ingest_confident_status(conn, monkeypatch, tmp_path):
    monkeypatch.setenv("ADS_API_TOKEN", "fake")
    pdf = tmp_path / "data" / "t.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"%PDF-1.1\n")
    resolved = {
        "ok": True,
        "doc": {
            "bibcode": "2020ApJ...888...10T",
            "title": ["Test Paper Title"],
            "abstract": "Hello",
            "author": ["Test, A."],
            "year": 2020,
            "identifier": ["arxiv:2001.00001"],
        },
        "extracted": {"arxiv_id": "2001.00001"},
        "candidates": [],
        "match_method": "arxiv",
        "bibcode": "2020ApJ...888...10T",
        "error": None,
    }
    out = ping.ingest_pdf_file(
        conn,
        str(pdf),
        preresolved=resolved,
        extracted_override={"arxiv_id": "2001.00001"},
        copy_to_pdfs=False,
        sync_references=False,
    )
    assert out["ok"] is True
    assert out["status"] == "confident"
    assert out["confidence"]["confident"] is True
    assert ic.has_pending_metadata(conn, int(out["paper_id"])) is False
