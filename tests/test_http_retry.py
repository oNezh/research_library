"""Tests for the new ``http_get_with_retry`` exponential-backoff wrapper."""

from __future__ import annotations

import email.message
import urllib.error

import pytest

from research_library import lookup as _l
from research_library.http_retry import backoff_seconds, parse_retry_after


def test_parse_retry_after_seconds_and_invalid():
    hdrs = email.message.Message()
    hdrs["Retry-After"] = "12"
    assert parse_retry_after(hdrs) == 12.0
    hdrs2 = email.message.Message()
    hdrs2["Retry-After"] = "not-a-date"
    assert parse_retry_after(hdrs2) is None
    assert parse_retry_after(None) is None


def test_backoff_honors_retry_after_without_exceeding_cap():
    hdrs = email.message.Message()
    hdrs["Retry-After"] = "999"
    err = urllib.error.HTTPError("https://example/", 429, "rate", hdrs, None)
    wait = backoff_seconds(0, base=1.0, max_delay=5.0, err=err, jitter=False)
    assert wait == 5.0


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


def test_retry_after_header_is_honored(monkeypatch):
    calls = {"n": 0}
    sleeps: list[float] = []

    def fake_get(url, headers=None, timeout=30):  # noqa: ARG001
        calls["n"] += 1
        if calls["n"] == 1:
            hdrs = email.message.Message()
            hdrs["Retry-After"] = "3"
            raise urllib.error.HTTPError(url, 429, "rate", hdrs, None)
        return b"ok"

    monkeypatch.setattr(_l, "http_get", fake_get)
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))
    monkeypatch.setenv("RESEARCH_HTTP_RETRY_BASE_DELAY", "0.01")
    monkeypatch.setenv("RESEARCH_HTTP_RETRY_MAX_DELAY", "30")
    out = _l.http_get_with_retry("https://example/", attempts=3)
    assert out == b"ok"
    assert calls["n"] == 2
    assert sleeps and 3.0 <= sleeps[0] <= 4.5


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
