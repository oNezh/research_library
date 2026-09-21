"""Offline tests for Zotero sync helpers."""

from __future__ import annotations

import json
import sqlite3

import pytest

from research_library.library import db as library_db
from research_library.library import zotero_sync as zs


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_LIBRARY_DATA_DIR", str(tmp_path / "data"))
    c = library_db.connect()
    library_db.ensure_schema(c)
    return c


def test_extract_ids_doi_and_arxiv():
    data = {
        "DOI": "10.3847/1538-4365/ac00b3",
        "url": "https://arxiv.org/abs/2101.05765",
    }
    doi, arxiv = zs._extract_ids(data)
    assert doi == "10.3847/1538-4365/ac00b3"
    assert arxiv == "2101.05765"


def test_extract_ids_from_archive_location():
    data = {"archive": "arXiv", "archiveLocation": "2409.13855v2"}
    doi, arxiv = zs._extract_ids(data)
    assert doi is None
    assert arxiv == "2409.13855"


def test_is_bibliographic_skips_attachment():
    assert zs._is_bibliographic({"data": {"itemType": "journalArticle"}}) is True
    assert zs._is_bibliographic({"data": {"itemType": "attachment"}}) is False


def test_upsert_zotero_link_and_lookup(conn):
    pid = library_db.insert_paper_minimal(
        conn,
        title="Test paper",
        doi="10.1000/xyz",
        commit=True,
    )
    library_db.upsert_zotero_link(
        conn,
        paper_id=pid,
        zotero_key="ABCD1234",
        zotero_version=3,
        doi="10.1000/xyz",
        commit=True,
    )
    assert library_db.get_paper_id_by_zotero_key(conn, "ABCD1234") == pid
    assert library_db.get_paper_id_by_doi(conn, "10.1000/xyz") == pid


def test_sync_state_version(conn):
    library_db.set_zotero_sync_version(conn, "12345", 99, commit=True)
    assert library_db.get_zotero_sync_version(conn, "12345") == 99
    library_db.set_zotero_sync_version(conn, "12345", 120, commit=True)
    assert library_db.get_zotero_sync_version(conn, "12345") == 120


def test_upsert_from_zotero_title_only(conn):
    pid = zs._upsert_from_zotero_item(
        conn,
        {"title": "Old ApJS", "abstractNote": "abstract", "creators": []},
        doi=None,
        arxiv_id=None,
        ads_doc=None,
        existing_id=None,
    )
    row = library_db.get_paper_row(conn, pid)
    assert row is not None
    assert row["title"] == "Old ApJS"
    assert row["source"] == "zotero_pull"


def test_upsert_paper_by_doi(conn):
    pid = library_db.upsert_paper(
        conn,
        arxiv_id=None,
        bibcode=None,
        doi="10.5555/abc",
        title="By DOI",
        commit=True,
    )
    pid2 = library_db.upsert_paper(
        conn,
        arxiv_id=None,
        bibcode=None,
        doi="10.5555/abc",
        title="By DOI updated",
        commit=True,
    )
    assert pid == pid2
    row = library_db.get_paper_row(conn, pid2)
    assert row["title"] == "By DOI updated"


def test_authors_json_to_creators():
    creators = zs._authors_json_to_creators(json.dumps(["Abbott, T.", "Smith"]))
    assert creators[0]["lastName"] == "Abbott"
    assert creators[0]["firstName"] == "T."


def test_status_without_credentials(conn, monkeypatch):
    monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
    monkeypatch.delenv("ZOTERO_LIBRARY_ID", raising=False)
    out = zs.status(conn)
    assert out["configured"] is False
    assert out["local"]["local_total"] >= 0


def test_link_matches_local_by_doi(conn, monkeypatch):
    pid = library_db.upsert_paper(
        conn,
        arxiv_id="2101.05765",
        bibcode=None,
        doi="10.3847/1538-4365/ac00b3",
        title="DES DR2",
        commit=True,
    )

    class FakeZot:
        def items(self, **kwargs):
            return [
                {
                    "key": "KEY0001",
                    "version": 1,
                    "data": {
                        "itemType": "journalArticle",
                        "DOI": "10.3847/1538-4365/ac00b3",
                        "title": "DES",
                    },
                }
            ]

    monkeypatch.setattr(zs, "_client", lambda: (FakeZot(), None))
    monkeypatch.setattr(zs, "_fetch_ads_doc", lambda d, a, bibcode=None: None)
    out = zs.link(conn)
    assert out["linked"] == 1
    assert library_db.get_paper_id_by_zotero_key(conn, "KEY0001") == pid


def test_apply_row_and_ads_enriches_journal():
    tmpl = {
        "itemType": "journalArticle",
        "title": "",
        "abstractNote": "",
        "creators": [],
        "DOI": "",
        "url": "",
        "date": "",
        "extra": "",
        "publicationTitle": "",
        "journalAbbreviation": "",
        "volume": "",
        "issue": "",
        "pages": "",
        "archive": "",
        "archiveLocation": "",
    }
    row = {
        "title": "Local title",
        "abstract": "Local abstract",
        "authors_json": '["Smith, J."]',
        "bibcode": "2024ApJ...977...14C",
        "arxiv_id": "2409.13855",
        "doi": "10.3847/abc",
        "published": "2024-09-01",
        "source": "test",
    }
    ads = {
        "bibcode": "2024ApJ...977...14C",
        "title": ["ADS title"],
        "author": ["Cole, A."],
        "abstract": "ADS abstract",
        "year": "2024",
        "pub": "ApJ",
        "volume": "977",
        "issue": "1",
        "page": "14",
        "doi": ["10.3847/abc"],
        "identifier": ["arxiv:2409.13855"],
    }
    out = zs._apply_row_and_ads_to_zotero_data(tmpl, row, ads)
    assert out["title"] == "Local title"
    assert out["abstractNote"] == "Local abstract"
    assert out["publicationTitle"] == "ApJ"
    assert out["volume"] == "977"
    assert out["pages"] == "14"
    assert out["DOI"] == "10.3847/abc"
    assert "Bibcode" in out["extra"]


def test_ads_scalar_extracts_page_from_list():
    assert zs._ads_scalar(["273"]) == "273"
    assert zs._ads_scalar("14") == "14"
    assert zs._ads_scalar([]) is None


def test_apply_row_pages_not_list_literal():
    tmpl = {
        "itemType": "journalArticle",
        "title": "",
        "abstractNote": "",
        "creators": [],
        "DOI": "",
        "url": "",
        "date": "",
        "extra": "",
        "publicationTitle": "",
        "journalAbbreviation": "",
        "volume": "",
        "issue": "",
        "pages": "",
        "archive": "",
        "archiveLocation": "",
    }
    row = {"title": "T", "abstract": "", "authors_json": "[]", "source": "test"}
    ads = {"page": ["273"], "pub": "ApJ", "volume": "977"}
    out = zs._apply_row_and_ads_to_zotero_data(tmpl, row, ads)
    assert out["pages"] == "273"
    assert "[" not in out["pages"]


def test_apply_row_uses_parsed_authors_key():
    tmpl = {
        "itemType": "journalArticle",
        "title": "",
        "abstractNote": "",
        "creators": [{"creatorType": "author", "lastName": "Unknown", "firstName": ""}],
        "DOI": "",
        "url": "",
        "date": "",
        "extra": "",
        "publicationTitle": "",
        "journalAbbreviation": "",
        "volume": "",
        "issue": "",
        "pages": "",
        "archive": "",
        "archiveLocation": "",
    }
    row = {
        "title": "T",
        "abstract": "",
        "authors": ["Smith, J.", "Jones, A."],
        "source": "test",
    }
    out = zs._apply_row_and_ads_to_zotero_data(tmpl, row, None)
    assert out["creators"][0]["lastName"] == "Smith"
    assert out["creators"][0]["firstName"] == "J."
    assert out["creators"][1]["lastName"] == "Jones"


def test_extract_bibcode_from_extra():
    data = {
        "extra": "Bibcode: 1981MNRAS.194..809L; research_library source: test",
    }
    assert zs._extract_bibcode(data) == "1981MNRAS.194..809L"
    assert zs._extract_bibcode({"extra": ""}) is None


def test_upsert_preserves_local_authors_on_unknown_zotero_pull(conn):
    pid = library_db.upsert_paper(
        conn,
        arxiv_id=None,
        bibcode="1981MNRAS.194..809L",
        doi=None,
        title="Turbulence paper",
        abstract="Local abstract",
        authors=["Larson, R. B."],
        published="1981-01-01",
        commit=True,
    )
    zs._upsert_from_zotero_item(
        conn,
        {
            "title": "Turbulence paper",
            "abstractNote": "",
            "creators": [{"creatorType": "author", "lastName": "Unknown", "firstName": ""}],
            "date": "MNRAS",
        },
        doi=None,
        arxiv_id=None,
        ads_doc=None,
        existing_id=pid,
    )
    row = conn.execute(
        "SELECT authors_json, published FROM papers WHERE id = ?",
        (pid,),
    ).fetchone()
    assert "Larson" in row["authors_json"]
    assert row["published"] == "1981-01-01"


def test_upsert_preserves_published_when_zotero_date_is_journal(conn):
    pid = library_db.upsert_paper(
        conn,
        arxiv_id=None,
        bibcode="1990PASP..102.1181B",
        doi=None,
        title="UBVRI passbands",
        authors=["Bessell, M. S."],
        published="1990-01-01",
        commit=True,
    )
    zs._upsert_from_zotero_item(
        conn,
        {
            "title": "UBVRI passbands",
            "creators": [{"creatorType": "author", "lastName": "Unknown", "firstName": ""}],
            "date": "PASP",
        },
        doi=None,
        arxiv_id=None,
        ads_doc=None,
        existing_id=pid,
    )
    row = conn.execute("SELECT published FROM papers WHERE id = ?", (pid,)).fetchone()
    assert row["published"] == "1990-01-01"


def test_fetch_ads_doc_uses_bibcode(monkeypatch):
    calls: list[str] = []

    def fake_retry(query: str):
        calls.append(query)
        if 'bibcode:"1981MNRAS.194..809L"' in query:
            return {"bibcode": "1981MNRAS.194..809L", "author": ["Larson, R. B."]}
        return None

    monkeypatch.setattr(zs, "_ads_query_doc_retry", fake_retry)
    doc = zs._fetch_ads_doc(None, None, bibcode="1981MNRAS.194..809L")
    assert doc is not None
    assert calls == ['bibcode:"1981MNRAS.194..809L"']


def test_push_requires_client(conn, monkeypatch):
    monkeypatch.setattr(
        zs,
        "_client",
        lambda: (None, {"ok": False, "error": "missing"}),
    )
    out = zs.push(conn)
    assert out["ok"] is False
    assert "missing" in out["error"]


def test_zotero_item_for_update_skips_trashed():
    class FakeZot:
        def item(self, key):
            return {
                "key": key,
                "version": 5,
                "data": {"itemType": "journalArticle", "title": "Old", "deleted": 1},
            }

    payload, trashed = zs._zotero_item_for_update(FakeZot(), "TRASH01", {"title": "T"})
    assert payload is None
    assert trashed is True


def test_zotero_item_for_update_strips_deleted(monkeypatch):
    class FakeZot:
        def item(self, key):
            return {
                "key": key,
                "version": 5,
                "data": {"itemType": "journalArticle", "title": "Old"},
            }

    monkeypatch.setattr(zs, "_fetch_ads_for_row", lambda row: None)
    payload, trashed = zs._zotero_item_for_update(FakeZot(), "GOOD01", {"title": "New"})
    assert trashed is False
    assert payload is not None
    assert "deleted" not in payload["data"]


def test_flush_zotero_updates_per_item_on_batch_failure():
    calls: List[List[Dict[str, Any]]] = []

    class FakeZot:
        def update_items(self, items):
            calls.append(list(items))
            if len(items) > 1:
                raise RuntimeError("Invalid keys: deleted")
            if items[0]["data"].get("bad"):
                raise RuntimeError("bad item")

    batch = [
        {"data": {"key": "A", "title": "ok"}},
        {"data": {"key": "B", "bad": True, "title": "fail"}},
        {"data": {"key": "C", "title": "ok2"}},
    ]
    meta = [(1, "A"), (2, "B"), (3, "C")]
    errors: List[Dict[str, Any]] = []
    n = zs._flush_zotero_updates(FakeZot(), batch, meta, errors)
    assert n == 2
    assert len(errors) == 1
    assert errors[0]["zotero_key"] == "B"
    assert len(calls) == 4
    assert len(calls[0]) == 3
    assert all(len(c) == 1 for c in calls[1:])
