"""Tests for paper metadata refresh."""

from __future__ import annotations

import pytest

from research_library.library import db as library_db
from research_library.library import metadata_refresh as mr


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_LIBRARY_DATA_DIR", str(tmp_path / "data"))
    c = library_db.connect()
    library_db.ensure_schema(c)
    return c


def _sample_ads_doc() -> dict:
    return {
        "bibcode": "2023A&A...674A..42P",
        "title": [
            "Improving the open cluster census II. An all-sky cluster catalogue with Gaia DR3"
        ],
        "abstract": "We present an all-sky open cluster catalogue.",
        "author": ["Pang, X.", "Chen, Y."],
        "year": 2023,
        "doi": ["10.1051/0004-6361/202245678"],
        "identifier": ["doi:10.1051/0004-6361/202245678"],
    }


def test_refresh_by_bibcode_overwrites_wrong_title(conn, monkeypatch):
    monkeypatch.setenv("ADS_API_TOKEN", "test-token")
    pid = library_db.upsert_paper(
        conn,
        arxiv_id=None,
        bibcode="2023A&A...674A..42P",
        doi="10.1051/0004-6361/202245678",
        title="Gaia DR3 results",
        abstract="",
        authors=["Unknown"],
        published="2023-01-01",
        commit=True,
    )

    monkeypatch.setattr(mr, "_resolve_by_bibcode", lambda _bc: _sample_ads_doc())
    monkeypatch.setattr(mr, "_resolve_by_doi", lambda _d: None)
    monkeypatch.setattr(mr, "_resolve_by_arxiv", lambda _a: None)

    out = mr.refresh_paper_metadata(conn, pid)
    assert out["ok"] is True
    assert out["match_method"] == "bibcode"
    assert out["updated"] is True
    assert out["tagged"] is True
    assert "Improving the open cluster census II" in out["paper"]["title"]
    assert out["paper"]["authors"] == ["Pang, X.", "Chen, Y."]
    tag = conn.execute(
        "SELECT tag_value FROM paper_tags WHERE paper_id = ? AND tag_key = ?",
        (pid, mr.METADATA_SYNCED_TAG),
    ).fetchone()
    assert tag is not None
    assert str(tag[0]).startswith("bibcode:")


def test_pending_metadata_sync_excludes_tagged(conn, monkeypatch):
    monkeypatch.setenv("ADS_API_TOKEN", "test-token")
    pid1 = library_db.upsert_paper(
        conn,
        arxiv_id="2301.00001",
        title="Paper one",
        abstract="",
        authors=[],
        commit=True,
    )
    pid2 = library_db.upsert_paper(
        conn,
        arxiv_id="2301.00002",
        title="Paper two",
        abstract="",
        authors=[],
        commit=True,
    )
    mr.mark_metadata_synced(conn, pid1, "bibcode")
    conn.commit()
    pending = mr.list_pending_metadata_sync_ids(conn)
    assert pid1 not in pending
    assert pid2 in pending
    assert mr.count_pending_metadata_sync(conn) == 1


def test_refresh_pending_skips_tagged(conn, monkeypatch):
    monkeypatch.setenv("ADS_API_TOKEN", "test-token")
    pid = library_db.upsert_paper(
        conn,
        arxiv_id="2301.00003",
        bibcode="2023A&A...674A..42P",
        title="Old title",
        abstract="",
        authors=[],
        commit=True,
    )
    calls: list[int] = []

    def _refresh(_conn, paper_id):
        calls.append(paper_id)
        return {"ok": True, "match_method": "bibcode", "updated": True, "changed": {}, "tagged": True, "paper": {}}

    monkeypatch.setattr(mr, "refresh_paper_metadata", _refresh)
    mr.mark_metadata_synced(conn, pid, "bibcode")
    conn.commit()

    pid2 = library_db.upsert_paper(
        conn,
        arxiv_id="2301.00004",
        bibcode="2023A&A...674A..43P",
        title="Another",
        abstract="",
        authors=[],
        commit=True,
    )

    out = mr.refresh_pending_metadata(conn, delay=0)
    assert out["ok"] is True
    assert out["total"] == 1
    assert out["synced"] == 1
    assert calls == [pid2]


def test_refresh_prefers_bibcode_before_title(conn, monkeypatch):
    monkeypatch.setenv("ADS_API_TOKEN", "test-token")
    pid = library_db.upsert_paper(
        conn,
        arxiv_id=None,
        bibcode="2023A&A...674A..42P",
        title="Gaia DR3 results",
        abstract="",
        authors=[],
        published="2023-01-01",
        commit=True,
    )
    calls: list[str] = []

    def _bib(_bc):
        calls.append("bibcode")
        return _sample_ads_doc()

    def _title(_t, _a):
        calls.append("title")
        return _sample_ads_doc()

    monkeypatch.setattr(mr, "_resolve_by_bibcode", _bib)
    monkeypatch.setattr(mr, "_resolve_by_doi", lambda _d: None)
    monkeypatch.setattr(mr, "_resolve_by_arxiv", lambda _a: None)
    monkeypatch.setattr(mr, "_resolve_by_title", _title)
    monkeypatch.setattr(mr, "_resolve_by_author_year", lambda *_a: None)

    mr.refresh_paper_metadata(conn, pid)
    assert calls == ["bibcode"]


def test_refresh_requires_ads_token(conn, monkeypatch):
    monkeypatch.delenv("ADS_API_TOKEN", raising=False)
    pid = library_db.upsert_paper(
        conn,
        arxiv_id="2301.00001",
        title="Some paper",
        abstract="",
        authors=[],
        commit=True,
    )
    out = mr.refresh_paper_metadata(conn, pid)
    assert out["ok"] is False
    assert "ADS_API_TOKEN" in out["error"]
