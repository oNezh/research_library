"""Tests for app settings API."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("RESEARCH_LIBRARY_DATA_DIR", str(data_dir))
    import research_library.server.routes.jobs as jobs_routes

    jobs_routes._manager = None

    from research_library.library import db as library_db

    conn = library_db.connect()
    library_db.ensure_schema(conn)
    conn.close()

    from research_library.server.app import create_app

    app = create_app()
    with TestClient(app) as c:
        c.data_dir = data_dir
        yield c


def test_get_settings_default(client):
    r = client.get("/api/settings")
    assert r.status_code == 200
    data = r.json()
    assert "settings" in data
    assert data["configured"]["ads"] is False
    assert "llm" in data["settings"]


def test_patch_settings_persists(client):
    r = client.patch(
        "/api/settings",
        json={
            "ads": {"api_token": "secret-ads-token"},
            "llm": {
                "provider": "openai_compat",
                "api_key": "sk-test-key",
                "base_url": "https://api.openai.com/v1",
                "model": "gpt-4o-mini",
            },
        },
    )
    assert r.status_code == 200
    data = r.json()
    assert data["configured"]["ads"] is True
    assert data["configured"]["llm"] is True
    assert "secret" not in json.dumps(data["settings"])
    assert "…" in data["settings"]["ads"]["api_token"]

    path = client.data_dir / "app_settings.json"
    assert path.is_file()
    saved = json.loads(path.read_text())
    assert saved["ads"]["api_token"] == "secret-ads-token"

    health = client.get("/api/health").json()
    assert health["tokens"]["ads"] is True
    assert health["tokens"]["llm"] is True


def test_patch_empty_secret_preserves(client):
    client.patch(
        "/api/settings",
        json={"ads": {"api_token": "keep-me"}},
    )
    r = client.patch(
        "/api/settings",
        json={"ads": {"api_token": ""}},
    )
    assert r.status_code == 200
    saved = json.loads((client.data_dir / "app_settings.json").read_text())
    assert saved["ads"]["api_token"] == "keep-me"


def test_patch_invalid_provider(client):
    r = client.patch(
        "/api/settings",
        json={"llm": {"provider": "invalid_provider"}},
    )
    assert r.status_code == 422


def test_apply_skips_defaults_without_settings_file(tmp_path, monkeypatch):
    import os

    monkeypatch.setenv("RESEARCH_LIBRARY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("RESEARCH_LOCAL_EMBEDDING_MODEL", raising=False)
    from research_library.app_settings import apply_to_environ

    apply_to_environ()
    assert not (os.environ.get("RESEARCH_LOCAL_EMBEDDING_MODEL") or "").strip()
