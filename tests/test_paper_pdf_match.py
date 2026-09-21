"""Tests for PDF/metadata mismatch detection and split."""

from __future__ import annotations

import pytest

from research_library.library import db as library_db
from research_library.library import paper_pdf_match as ppm


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_LIBRARY_DATA_DIR", str(tmp_path / "data"))
    c = library_db.connect()
    library_db.ensure_schema(c)
    return c


def test_is_junk_title_detects_call_for_papers():
    t = "CALL FOR PAPERS: Special issue on Symmetries and Integrability of Difference Equations Special issue on Symmetries and Integrability of Difference Equations"
    assert ppm.is_junk_title(t) is True


def test_is_junk_title_normal_paper():
    assert ppm.is_junk_title("Improving the open cluster census II") is False


def test_assess_mismatch_low_similarity(conn, monkeypatch, tmp_path):
    pdf = tmp_path / "data" / "pdfs" / "wrong.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4 fake")

    pid = library_db.upsert_paper(
        conn,
        arxiv_id=None,
        bibcode="2022yCat..13510082O",
        title="VizieR Online Data Catalog: Distances to Local Group galaxies. II. Fornax dSph (Oakes+, 2022)",
        abstract="",
        authors=["Oakes, P."],
        pdf_relpath="pdfs/wrong.pdf",
        commit=True,
    )

    monkeypatch.setattr(
        ppm,
        "extract_pdf_identifiers",
        lambda _p: {
            "title_candidate": "Relation between the geometric shape and rotation of Galactic globular clusters",
            "doi": None,
            "arxiv_id": None,
        },
    )
    monkeypatch.setattr(
        ppm,
        "resolve_extracted_to_ads_match",
        lambda *_a, **_k: {
            "ok": True,
            "bibcode": "2020A&A...640A..13C",
            "match_method": "title",
            "doc": {"title": ["Relation between the geometric shape and rotation of Galactic globular clusters"], "bibcode": "2020A&A...640A..13C"},
        },
    )

    out = ppm.assess_paper_pdf_match(conn, pid)
    assert out["ok"] is True
    assert out["mismatch"] is True
    assert out["matched"] is False


def test_split_detaches_pdf_and_creates_new_row(conn, monkeypatch, tmp_path):
    pdf = tmp_path / "data" / "pdfs" / "globular.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4 fake")

    pid = library_db.upsert_paper(
        conn,
        arxiv_id=None,
        bibcode="2022yCat..13510082O",
        title="VizieR catalog entry",
        abstract="",
        authors=[],
        pdf_relpath="pdfs/globular.pdf",
        commit=True,
    )

    monkeypatch.setattr(
        ppm,
        "assess_paper_pdf_match",
        lambda _c, _p: {
            "ok": True,
            "has_pdf": True,
            "mismatch": True,
            "junk_metadata": False,
            "extracted": {"title_candidate": "Globular clusters paper"},
        },
    )

    new_id_holder = {"id": None}

    def _fake_ingest(conn, pdf_abs, **kwargs):
        new_id_holder["id"] = library_db.upsert_paper(
            conn,
            arxiv_id=None,
            bibcode="2020A&A...640A..13C",
            title="Globular clusters paper",
            abstract="",
            authors=["Chen, Y."],
            pdf_relpath="pdfs/globular.pdf",
            commit=False,
        )
        return {
            "ok": True,
            "paper_id": new_id_holder["id"],
            "match_method": "title",
            "bibcode": "2020A&A...640A..13C",
            "extracted": {"title_candidate": "Globular clusters paper"},
        }

    from research_library.library import pdf_ingest

    monkeypatch.setattr(pdf_ingest, "ingest_pdf_file", _fake_ingest)

    out = ppm.split_paper_pdf(conn, pid)
    assert out["ok"] is True
    assert out["metadata_paper_id"] == pid
    assert out["pdf_paper_id"] == new_id_holder["id"]

    meta = library_db.get_paper_row(conn, pid)
    pdf_row = library_db.get_paper_row(conn, new_id_holder["id"])
    assert meta["pdf_relpath"] in (None, "")
    assert pdf_row["pdf_relpath"] == "pdfs/globular.pdf"


def test_export_eligibility_requires_metadata_synced(conn):
    from research_library.library.metadata_refresh import mark_metadata_synced

    pid_ok = library_db.upsert_paper(
        conn,
        arxiv_id=None,
        bibcode="2020A&A...640A..13C",
        title="Good paper title here",
        abstract="",
        authors=["A"],
        commit=True,
    )
    pid_no_sync = library_db.upsert_paper(
        conn,
        arxiv_id=None,
        bibcode="2021A&A...640A..14D",
        title="Unsynced paper title here",
        abstract="",
        authors=["B"],
        commit=True,
    )
    pid_no_bc = library_db.upsert_paper(
        conn,
        arxiv_id="1234.5678",
        bibcode=None,
        title="No bibcode paper title",
        abstract="",
        authors=["C"],
        commit=True,
    )
    mark_metadata_synced(conn, pid_ok, "bibcode")
    mark_metadata_synced(conn, pid_no_bc, "arxiv")
    conn.commit()

    out = ppm.export_eligibility(conn, [pid_ok, pid_no_sync, pid_no_bc], check_pdf_match=False)
    assert out["eligible"] == [pid_ok]
    reasons = {s["id"]: s["reason"] for s in out["skipped"]}
    assert reasons[pid_no_sync] == "missing_metadata_synced"
    assert reasons[pid_no_bc] == "no_bibcode"


def test_title_from_source_text_extracts_title():
    tex = r"""
\documentclass{article}
\title{My Cool Astronomy Result}
\author{Someone}
\begin{document}
\maketitle
\end{document}
"""
    assert ppm._title_from_source_text(tex, None) == "My Cool Astronomy Result"


def test_assess_uses_source_title_to_flag_pdf_wrong(conn, monkeypatch, tmp_path):
    pdf = tmp_path / "data" / "pdfs" / "wrong.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4 fake")

    pid = library_db.upsert_paper(
        conn,
        arxiv_id="9999.1111",
        bibcode="2020A&A...640A..13C",
        title="Relation between the geometric shape and rotation of Galactic globular clusters",
        abstract="",
        authors=["Chen"],
        pdf_relpath="pdfs/wrong.pdf",
        commit=True,
    )

    monkeypatch.setattr(
        ppm,
        "extract_pdf_identifiers",
        lambda _p: {
            "title_candidate": "Completely unrelated planetary nebula survey paper",
            "doi": None,
            "arxiv_id": None,
        },
    )
    monkeypatch.setattr(
        ppm,
        "resolve_extracted_to_ads_match",
        lambda *_a, **_k: {"ok": False, "bibcode": None, "match_method": None, "doc": None},
    )
    monkeypatch.setattr(
        ppm,
        "_read_source_title",
        lambda *_a, **_k: "Relation between the geometric shape and rotation of Galactic globular clusters",
    )

    out = ppm.assess_paper_pdf_match(conn, pid)
    assert out["mismatch"] is True
    assert out["issue_hint"] == "pdf_wrong"
    assert out["source_vs_meta"] == "match"
    assert out["source_vs_pdf"] == "mismatch"
