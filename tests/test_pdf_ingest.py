"""Tests for PDF identifier extraction and ingest orchestration (no live ADS)."""

from __future__ import annotations

import pytest

from research_library.library import pdf_identifiers as pid
from research_library.library import pdf_ingest as ping


def test_doi_arxiv_from_strings() -> None:
    raw = "Preprint doi:10.1016/j.apj.2020.123456 see arXiv:2011.00001v2"
    assert pid._doi_from_text(raw) == "10.1016/j.apj.2020.123456"
    assert pid._arxiv_from_text(raw) == "2011.00001"


def test_title_candidate_skips_et_al_line() -> None:
    clean = "Smith et al. (2020) Abstract starts here and continues"
    assert pid._title_candidate_from_clean_text(clean) is None


def test_extract_pdf_identifiers_monkeypatch(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_read(path: str):
        return (
            "Dark Matter Halos In Cosmological Simulations\n",
            "10.1234/test doi stuff arXiv:1801.00001\n",
            None,
        )

    monkeypatch.setattr(pid, "read_pdf_text_layers", fake_read)
    d = pid.extract_pdf_identifiers("/tmp/x.pdf")
    assert d["doi"] == "10.1234/test"
    assert d["arxiv_id"] == "1801.00001"
    assert d["title_candidate"] is not None
    assert "Dark Matter" in d["title_candidate"]


def test_resolve_extracted_strong_id_blocks_title(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADS_API_TOKEN", "fake")
    calls: list[str] = []

    def boom(q: str, rows: int = 10, **kwargs):  # noqa: ARG001
        calls.append(q)
        return {"response": {"docs": []}}

    monkeypatch.setattr(ping, "ads_query", boom)
    r = ping.resolve_extracted_to_ads_match(
        {"title_candidate": "Some Title", "doi": None, "arxiv_id": None},
        require_strong_id=True,
    )
    assert r["ok"] is False
    assert calls == []


def test_ingest_pdf_dry_run_no_ads(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("RESEARCH_LIBRARY_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ADS_API_TOKEN", raising=False)
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.1\n%\xe2\xe3\xcf\xd3\n")
    from research_library.library import db as library_db

    conn = library_db.connect()
    out = ping.ingest_pdf_file(conn, str(pdf), dry_run=True, require_strong_id=False)
    assert out["ok"] is False
    assert "ADS_API_TOKEN" in (out.get("error") or "")


def test_ingest_pdf_syncs_references(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("RESEARCH_LIBRARY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ADS_API_TOKEN", "fake")
    pdf = tmp_path / "c.pdf"
    pdf.write_bytes(b"%PDF-1.1\n")

    def fake_fetch(bc: str) -> list:
        assert bc == "2020ApJ...888...10T"
        return ["2008ApJ...678...99C", "2099ApJ...999..9Z"]

    monkeypatch.setattr(
        "research_library.library.citations.fetch_ads_reference_bibcodes",
        fake_fetch,
    )
    monkeypatch.setattr(ping, "ads_query", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))

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
        "extracted": {"doi": None, "arxiv_id": "2001.00001"},
        "candidates": [],
        "match_method": "arxiv",
        "bibcode": "2020ApJ...888...10T",
        "error": None,
    }

    from research_library.library import db as library_db

    conn = library_db.connect()
    out = ping.ingest_pdf_file(
        conn,
        str(pdf),
        dry_run=False,
        preresolved=resolved,
        extracted_override={"arxiv_id": "2001.00001"},
        copy_to_pdfs=False,
        symlink_to_pdfs=False,
    )
    assert out["ok"] is True
    assert out.get("references_sync", {}).get("ok") is True
    assert out["references_sync"]["edges_written"] == 2
    pid = int(out["paper_id"])
    n = conn.execute(
        "SELECT COUNT(*) FROM paper_references WHERE from_paper_id = ?", (pid,)
    ).fetchone()[0]
    assert n == 2


def test_ingest_pdf_skips_references_when_disabled(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("RESEARCH_LIBRARY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ADS_API_TOKEN", "fake")

    def boom(bc: str) -> list:  # noqa: ARG001
        raise AssertionError("should not fetch references")

    monkeypatch.setattr(
        "research_library.library.citations.fetch_ads_reference_bibcodes",
        boom,
    )

    pdf = tmp_path / "d.pdf"
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
        "extracted": {},
        "candidates": [],
        "match_method": "arxiv",
        "bibcode": "2020ApJ...888...10T",
        "error": None,
    }
    from research_library.library import db as library_db

    conn = library_db.connect()
    out = ping.ingest_pdf_file(
        conn,
        str(pdf),
        dry_run=False,
        preresolved=resolved,
        copy_to_pdfs=False,
        sync_references=False,
    )
    assert out["ok"] is True
    assert "references_sync" not in out
    pid = int(out["paper_id"])
    n = conn.execute(
        "SELECT COUNT(*) FROM paper_references WHERE from_paper_id = ?", (pid,)
    ).fetchone()[0]
    assert n == 0
    monkeypatch.setenv("RESEARCH_LIBRARY_DATA_DIR", str(tmp_path))
    pdf = tmp_path / "b.pdf"
    pdf.write_bytes(b"%PDF-1.1\n")

    queried: list[str] = []

    def no_ads(*a, **k):
        queried.append("should_not_run")
        raise AssertionError("ads_query should not run when preresolved")

    monkeypatch.setattr(ping, "ads_query", no_ads)
    monkeypatch.setattr(ping, "extract_pdf_identifiers", lambda _p: (_ for _ in ()).throw(AssertionError()))

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
        "extracted": {"doi": None, "arxiv_id": "2001.00001"},
        "candidates": [],
        "match_method": "arxiv",
        "bibcode": "2020ApJ...888...10T",
        "error": None,
    }

    from research_library.library import db as library_db

    conn = library_db.connect()
    out = ping.ingest_pdf_file(
        conn,
        str(pdf),
        dry_run=True,
        preresolved=resolved,
        extracted_override={"arxiv_id": "2001.00001"},
    )
    assert queried == []
    assert out["ok"] is True
    assert out.get("pdf_relpath_would_be")
