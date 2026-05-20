"""Verify the new concurrent child-acquire helper preserves per-job results."""

from __future__ import annotations

from research_library.analysis import pdf as _pdf


def test_acquire_children_concurrent_resolves_each_job(monkeypatch):
    captured: dict[str, list[str]] = {"calls": []}

    def fake_acquire_pdf(sref, conn, dest, **kwargs):  # noqa: ARG001
        captured["calls"].append(dest)
        return dest, "ok_test"

    def fake_parse_catalog_line(s, conn, use_ads=True):  # noqa: ARG001
        class _Ref:
            bibcode = s
            arxiv_id = None
            doi = None
            resolution_note = "test"

        return _Ref()

    def fake_standard_ref_for_reference_line(line, conn):  # noqa: ARG001
        class _Ref:
            bibcode = None
            arxiv_id = None
            doi = None
            resolution_note = "test_freeform"

        return _Ref()

    def fake_find_local_pdf_path(conn, bibcode=None, arxiv_id=None):  # noqa: ARG001
        return None

    monkeypatch.setattr(_pdf, "acquire_pdf", fake_acquire_pdf)
    monkeypatch.setattr(_pdf, "parse_catalog_line", fake_parse_catalog_line)
    monkeypatch.setattr(
        _pdf,
        "_standard_ref_for_reference_line",
        fake_standard_ref_for_reference_line,
    )
    from research_library.library import db as library_db

    monkeypatch.setattr(library_db, "find_local_pdf_path", fake_find_local_pdf_path)

    jobs = [
        {"n": 1, "ref_line": "Smith 2020 ApJ", "bibcode": "2020ApJ...1S", "dest": "/tmp/a.pdf"},
        {"n": 2, "ref_line": "Doe 2021 MNRAS", "bibcode": None, "dest": "/tmp/b.pdf"},
        {"n": 3, "ref_line": "Roe 2022 A&A", "bibcode": "2022A&A....2R", "dest": "/tmp/c.pdf"},
    ]
    out = _pdf._acquire_children_concurrent(jobs, conn_lib=None, max_workers=3)
    assert len(out) == 3
    by_n = {o["n"]: o for o in out}
    assert by_n[1]["child_path"] == "/tmp/a.pdf"
    assert by_n[2]["child_path"] == "/tmp/b.pdf"
    assert by_n[3]["child_path"] == "/tmp/c.pdf"
    assert set(captured["calls"]) == {"/tmp/a.pdf", "/tmp/b.pdf", "/tmp/c.pdf"}


def test_acquire_children_concurrent_handles_empty():
    assert _pdf._acquire_children_concurrent([], conn_lib=None, max_workers=4) == []


def test_acquire_children_concurrent_serial_path(monkeypatch):
    def fake_acquire_pdf(sref, conn, dest, **kwargs):  # noqa: ARG001
        return None, "miss"

    def fake_standard(line, conn):  # noqa: ARG001
        class _Ref:
            bibcode = None
            arxiv_id = None
            doi = None
            resolution_note = "n"

        return _Ref()

    monkeypatch.setattr(_pdf, "acquire_pdf", fake_acquire_pdf)
    monkeypatch.setattr(_pdf, "_standard_ref_for_reference_line", fake_standard)
    out = _pdf._acquire_children_concurrent(
        [{"n": 1, "ref_line": "x", "bibcode": None, "dest": "/tmp/x"}],
        conn_lib=None,
        max_workers=1,
    )
    assert out[0]["child_path"] is None
    assert out[0]["note"] == "miss"
