"""Tests for arXiv keyword settings API."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("RESEARCH_LIBRARY_DATA_DIR", str(data_dir))
    import research_library.server.routes.jobs as jobs_routes

    jobs_routes._manager = None

    from research_library.server.app import create_app

    app = create_app()
    with TestClient(app) as c:
        yield c


def test_arxiv_keywords_get_default(client):
    r = client.get("/api/arxiv/keywords")
    assert r.status_code == 200
    data = r.json()
    assert "globular cluster" in data["phrases"]
    assert "globular cluster" in data["text"]


def test_arxiv_keywords_patch(client):
    r = client.patch("/api/arxiv/keywords", json={"text": "tidal stream\n星流"})
    assert r.status_code == 200
    data = r.json()
    assert data["phrases"] == ["tidal stream", "星流"]
    r2 = client.get("/api/arxiv/keywords")
    assert r2.json()["phrases"] == ["tidal stream", "星流"]
