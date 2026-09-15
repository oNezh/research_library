"""Mocked urllib tests for arXiv keyword-scan retries and scan exit status."""

from __future__ import annotations

import email.message
import io
import urllib.error
import urllib.request

import pytest

from research_library import arxiv_keywords as ak


class _Resp:
    def __init__(self, body: str | bytes):
        self._body = body.encode("utf-8") if isinstance(body, str) else body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *args: object) -> bool:
        return False


def _http_error(url: str, code: int, retry_after: str | None = None) -> urllib.error.HTTPError:
    hdrs = email.message.Message()
    if retry_after is not None:
        hdrs["Retry-After"] = retry_after
    return urllib.error.HTTPError(url, code, "err", hdrs, io.BytesIO(b""))


_EMPTY_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"></feed>
"""


def test_fetch_arxiv_retries_429_then_succeeds(monkeypatch):
    calls = {"n": 0}
    sleeps: list[float] = []

    def fake_urlopen(req, timeout=None):  # noqa: ARG001
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error("https://export.arxiv.org/api/query", 429, retry_after="2")
        return _Resp("<feed xmlns='http://www.w3.org/2005/Atom'/>")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(ak.time, "sleep", lambda s: sleeps.append(s))

    out = ak.fetch_arxiv("astro-ph.GA", retries=4, base_delay=0.01, max_delay=30)
    assert "<feed" in out
    assert calls["n"] == 2
    assert len(sleeps) == 1
    assert 2.0 <= sleeps[0] <= 3.0


def test_fetch_arxiv_exhausted_429(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):  # noqa: ARG001
        calls["n"] += 1
        raise _http_error("https://export.arxiv.org/api/query", 429)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(ak.time, "sleep", lambda s: None)

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        ak.fetch_arxiv("astro-ph.GA", retries=3, base_delay=0.01, max_delay=0.05)
    assert exc_info.value.code == 429
    assert calls["n"] == 3


def test_fetch_arxiv_404_not_retried(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):  # noqa: ARG001
        calls["n"] += 1
        raise _http_error("https://export.arxiv.org/api/query", 404)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(ak.time, "sleep", lambda s: None)

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        ak.fetch_arxiv("astro-ph.GA", retries=5, base_delay=0.01, max_delay=0.05)
    assert exc_info.value.code == 404
    assert calls["n"] == 1


def test_run_all_fetches_failed_prints_fetch_failed(monkeypatch, capsys):
    monkeypatch.setattr(ak.time, "sleep", lambda s: None)

    def boom(*args, **kwargs):  # noqa: ARG001
        raise _http_error("https://export.arxiv.org/api/query", 503)

    monkeypatch.setattr(ak, "fetch_arxiv", boom)
    code = ak.run(
        category="astro-ph.GA",
        days_back=2,
        cache_enabled=False,
        persist_db=False,
    )
    captured = capsys.readouterr()
    assert code == 2
    assert "FETCH_FAILED" in captured.out
    assert "NO_REPLY" not in captured.out


def test_run_empty_matches_prints_noreply(monkeypatch, capsys):
    monkeypatch.setattr(ak.time, "sleep", lambda s: None)
    monkeypatch.setattr(ak, "fetch_arxiv", lambda *a, **k: _EMPTY_FEED)
    code = ak.run(
        category="astro-ph.GA",
        days_back=2,
        cache_enabled=False,
        persist_db=False,
    )
    captured = capsys.readouterr()
    assert code == 0
    assert "NO_REPLY" in captured.out
    assert "FETCH_FAILED" not in captured.out


def test_run_retries_failed_category_later_in_same_run(monkeypatch, capsys):
    order: list[str] = []

    def fake_fetch(cat, **kwargs):  # noqa: ARG001
        order.append(cat)
        if cat == "astro-ph.GA" and order.count("astro-ph.GA") == 1:
            raise _http_error("https://export.arxiv.org/api/query", 429)
        return _EMPTY_FEED

    monkeypatch.setattr(ak.time, "sleep", lambda s: None)
    monkeypatch.setattr(ak, "fetch_arxiv", fake_fetch)
    code = ak.run(category="all", days_back=2, cache_enabled=False, persist_db=False)
    captured = capsys.readouterr()
    assert code == 0
    assert "NO_REPLY" in captured.out
    assert order[0] == "astro-ph.GA"
    assert order.count("astro-ph.GA") == 2
    first = order.index("astro-ph.GA")
    second = order.index("astro-ph.GA", first + 1)
    assert order[first + 1 : second], "other categories should run before the second-pass retry"
    assert "Retrying" in captured.err
