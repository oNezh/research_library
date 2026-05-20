"""Tests for the new ``http_get_with_retry`` exponential-backoff wrapper."""

from __future__ import annotations

import urllib.error

import pytest

from research_library import lookup as _l


def test_retry_succeeds_after_transient_503(monkeypatch):
    calls = {"n": 0}

    def fake_get(url, headers=None, timeout=30):  # noqa: ARG001
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.HTTPError(url, 503, "busy", hdrs=None, fp=None)
        return b"ok"

    monkeypatch.setattr(_l, "http_get", fake_get)
    monkeypatch.setenv("RESEARCH_HTTP_RETRY_ATTEMPTS", "4")
    monkeypatch.setenv("RESEARCH_HTTP_RETRY_BASE_DELAY", "0.01")
    out = _l.http_get_with_retry("https://example/", attempts=4)
    assert out == b"ok"
    assert calls["n"] == 3


def test_retry_gives_up_after_attempts(monkeypatch):
    def fake_get(url, headers=None, timeout=30):  # noqa: ARG001
        raise urllib.error.HTTPError(url, 502, "bad gateway", hdrs=None, fp=None)

    monkeypatch.setattr(_l, "http_get", fake_get)
    monkeypatch.setenv("RESEARCH_HTTP_RETRY_BASE_DELAY", "0.01")
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        _l.http_get_with_retry("https://example/", attempts=2)
    assert exc_info.value.code == 502


def test_4xx_not_retried(monkeypatch):
    calls = {"n": 0}

    def fake_get(url, headers=None, timeout=30):  # noqa: ARG001
        calls["n"] += 1
        raise urllib.error.HTTPError(url, 401, "auth", hdrs=None, fp=None)

    monkeypatch.setattr(_l, "http_get", fake_get)
    monkeypatch.setenv("RESEARCH_HTTP_RETRY_BASE_DELAY", "0.01")
    with pytest.raises(urllib.error.HTTPError):
        _l.http_get_with_retry("https://example/", attempts=3)
    assert calls["n"] == 1
