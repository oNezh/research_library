"""Offline tests for the FastAPI server (papers/search/jobs)."""

from __future__ import annotations

import time

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from research_library.library import db as library_db
from research_library.server import jobs as jobs_mod


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_LIBRARY_DATA_DIR", str(tmp_path / "data"))
    import research_library.server.routes.jobs as jobs_routes

    jobs_routes._manager = None

    conn = library_db.connect()
    library_db.ensure_schema(conn)
    pid = library_db.upsert_paper(
        conn,
        arxiv_id="2101.05765",
        bibcode="2021ApJS..255...20A",
        doi="10.3847/1538-4365/ac00b3",
        title="DES DR2 catalog",
        abstract="Dark Energy Survey data release with photometry.",
        authors=["Abbott, T.", "Smith, J."],
        published="2021-07-01",
        commit=True,
    )
    conn.execute(
        "INSERT INTO paper_tags(paper_id, tag_key, tag_value, source, created_at) "
        "VALUES (?, 'survey', '', 'manual', ?)",
        (pid, library_db._now_iso()),
    )
    conn.commit()
    conn.close()

    from research_library.server.app import create_app

    app = create_app()
    with TestClient(app) as c:
        c.paper_id = pid
        yield c


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["papers"] == 1
    assert "tokens" in data


def test_list_papers(client):
    r = client.get("/api/papers")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 1
    assert data["items"][0]["title"] == "DES DR2 catalog"
    assert data["items"][0]["authors"] == ["Abbott, T.", "Smith, J."]
    assert data["items"][0]["has_pdf"] is False


def test_list_papers_fts_filter(client):
    r = client.get("/api/papers", params={"q": "photometry"})
    assert r.json()["total"] == 1
    r = client.get("/api/papers", params={"q": "neutrino"})
    assert r.json()["total"] == 0


def test_list_papers_year_filter(client):
    assert client.get("/api/papers", params={"year_from": 2020}).json()["total"] == 1
    assert client.get("/api/papers", params={"year_from": 2022}).json()["total"] == 0


def test_list_papers_tag_filter(client):
    assert client.get("/api/papers", params={"tag": "survey"}).json()["total"] == 1
    assert client.get("/api/papers", params={"tag": "nope"}).json()["total"] == 0


def test_list_papers_missing_metadata_sync_tag(client):
    r = client.get("/api/papers", params={"missing_tag": "metadata_synced"})
    assert r.status_code == 200
    assert r.json()["total"] == 1
    client.post(
        f"/api/papers/{client.paper_id}/tags",
        json={"tag_key": "metadata_synced", "tag_value": "manual"},
    )
    r2 = client.get("/api/papers", params={"missing_tag": "metadata_synced"})
    assert r2.json()["total"] == 0


def test_paper_facets_pending_metadata_sync(client):
    data = client.get("/api/papers/facets").json()
    assert "pending_metadata_sync" in data
    assert data["pending_metadata_sync"] == 1
    assert data["metadata_synced_tag"] == "metadata_synced"


def test_paper_detail(client):
    r = client.get(f"/api/papers/{client.paper_id}")
    assert r.status_code == 200
    data = r.json()
    assert data["title"] == "DES DR2 catalog"
    assert data["tags"] == [{"tag_key": "survey", "tag_value": ""}]
    assert data["chunks"] == 0
    assert data["zotero"] is None
    assert client.get("/api/papers/99999").status_code == 404


def test_patch_paper(client):
    r = client.patch(
        f"/api/papers/{client.paper_id}",
        json={"title": "Updated title", "authors": ["New, A."]},
    )
    assert r.status_code == 200
    assert r.json()["paper"]["title"] == "Updated title"
    r2 = client.get("/api/papers", params={"q": "Updated"})
    assert r2.json()["total"] == 1


def test_tags_add_remove(client):
    r = client.post(f"/api/papers/{client.paper_id}/tags", json={"tag_key": "toread"})
    assert r.status_code == 200
    detail = client.get(f"/api/papers/{client.paper_id}").json()
    assert any(t["tag_key"] == "toread" for t in detail["tags"])
    client.delete(f"/api/papers/{client.paper_id}/tags/toread")
    detail = client.get(f"/api/papers/{client.paper_id}").json()
    assert not any(t["tag_key"] == "toread" for t in detail["tags"])


def test_facets(client):
    r = client.get("/api/papers/facets")
    assert r.status_code == 200
    data = r.json()
    assert data["years"][0]["year"] == "2021"
    assert data["tags"][0]["tag"] == "survey"


def test_search_fts(client):
    r = client.get("/api/search", params={"q": "photometry", "mode": "fts"})
    assert r.status_code == 200
    assert len(r.json()["results"]) == 1


def test_pdf_missing(client):
    r = client.get(f"/api/papers/{client.paper_id}/pdf")
    assert r.status_code == 404


def test_delete_paper(client):
    r = client.delete(f"/api/papers/{client.paper_id}")
    assert r.status_code == 200
    assert client.get("/api/papers").json()["total"] == 0


def test_job_lifecycle(client, monkeypatch):
    import research_library.server.routes.jobs as jobs_routes

    mgr = jobs_routes.get_manager()

    def fake_handler(conn, params, report):
        report(50, "halfway")
        return {"echo": params.get("x")}

    mgr.register("test_echo", fake_handler)

    r = client.post("/api/jobs", json={"kind": "test_echo", "params": {"x": 42}})
    assert r.status_code == 200
    job_id = r.json()["job_id"]

    deadline = time.time() + 10
    status = None
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        status = job["status"]
        if status in ("done", "error"):
            break
        time.sleep(0.1)
    assert status == "done"
    assert job["result"] == {"echo": 42}

    lst = client.get("/api/jobs").json()
    assert lst["total"] >= 1


def test_job_unknown_kind(client):
    r = client.post("/api/jobs", json={"kind": "nope", "params": {}})
    assert r.status_code == 422


def test_job_error_state(client):
    import research_library.server.routes.jobs as jobs_routes

    mgr = jobs_routes.get_manager()

    def bad_handler(conn, params, report):
        raise RuntimeError("boom")

    mgr.register("test_fail", bad_handler)
    job_id = client.post("/api/jobs", json={"kind": "test_fail", "params": {}}).json()["job_id"]

    deadline = time.time() + 10
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "error"):
            break
        time.sleep(0.1)
    assert job["status"] == "error"
    assert "boom" in job["error"]


def test_graph_only_connected(client):
    from research_library.library import db as library_db

    conn = library_db.connect()
    now = library_db._now_iso()
    p2 = library_db.upsert_paper(
        conn,
        arxiv_id="2201.00001",
        bibcode="2022ApJ..900...1B",
        title="Isolated paper",
        abstract="No citations.",
        authors=["Solo, A."],
        published="2022-01-01",
        commit=True,
    )
    p3 = library_db.upsert_paper(
        conn,
        arxiv_id="2201.00002",
        bibcode="2022ApJ..900...2C",
        title="Cited paper",
        abstract="Has inbound edge.",
        authors=["Pair, B."],
        published="2022-01-01",
        commit=True,
    )
    conn.execute(
        "INSERT INTO paper_references(from_paper_id, ref_bibcode, created_at) VALUES (?, ?, ?)",
        (client.paper_id, "2022ApJ..900...2C", now),
    )
    conn.commit()
    conn.close()

    r = client.get("/api/graph", params={"only_connected": True, "max_papers": 500})
    assert r.status_code == 200
    data = r.json()
    paper_ids = {n["paper_id"] for n in data["nodes"] if n.get("kind") == "paper"}
    assert client.paper_id in paper_ids
    assert p3 in paper_ids
    assert p2 not in paper_ids
    assert data["stats"]["nodes_returned"] <= data["stats"]["papers"]


def test_graph_max_papers_truncated(client):
    from research_library.library import db as library_db

    conn = library_db.connect()
    for i in range(55):
        library_db.upsert_paper(
            conn,
            arxiv_id=f"2001.{i:05d}",
            bibcode=f"2020ApJ..{i:04d}...1X",
            title=f"Bulk paper {i}",
            abstract="x",
            authors=["A"],
            published="2020-01-01",
            commit=False,
        )
    conn.commit()
    conn.close()

    r = client.get("/api/graph", params={"max_papers": 50, "only_connected": False})
    assert r.status_code == 200
    data = r.json()
    paper_nodes = [n for n in data["nodes"] if n.get("kind") == "paper"]
    assert len(paper_nodes) <= 50
    assert data["stats"]["truncated"] is True


def test_paper_detail_concurrent_requests(client):
    import concurrent.futures

    pid = client.paper_id
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        codes = [pool.submit(client.get, f"/api/papers/{pid}").result().status_code for _ in range(16)]
    assert all(c == 200 for c in codes)


def test_paper_detail_skips_ads_by_default(client, monkeypatch):
    def _boom(_bibcodes):
        raise AssertionError("ADS should not be called without enrich_ads=true")

    import research_library.server.routes.papers as papers_routes

    monkeypatch.setattr(papers_routes, "_fetch_ads_ref_metadata", _boom)
    r = client.get(f"/api/papers/{client.paper_id}")
    assert r.status_code == 200

    called = {"n": 0}

    def _count(bibcodes):
        if bibcodes:
            called["n"] += 1
        return {}

    monkeypatch.setattr(papers_routes, "_fetch_ads_ref_metadata", _count)
    r2 = client.get(f"/api/papers/{client.paper_id}", params={"enrich_ads": True})
    assert r2.status_code == 200
    assert called["n"] == 0


def test_refresh_paper_metadata_endpoint(client, monkeypatch):
    import research_library.library.metadata_refresh as mr

    monkeypatch.setenv("ADS_API_TOKEN", "test-token")
    monkeypatch.setattr(
        mr,
        "refresh_paper_metadata",
        lambda _conn, pid: {
            "ok": True,
            "match_method": "bibcode",
            "updated": True,
            "changed": {"title": True},
            "paper": {
                "id": pid,
                "title": "Updated title from ADS",
                "authors": ["A. Author"],
                "abstract": "New abstract",
                "bibcode": "2021ApJS..255...20A",
                "arxiv_id": "2101.05765",
                "doi": "10.3847/1538-4365/ac00b3",
                "published": "2021-07-01",
                "source": "manual",
                "pdf_relpath": None,
                "notes": None,
                "has_pdf": False,
            },
        },
    )
    r = client.post(f"/api/papers/{client.paper_id}/refresh-metadata")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["match_method"] == "bibcode"
    assert data["paper"]["title"] == "Updated title from ADS"

