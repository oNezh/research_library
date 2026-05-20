"""manual bibcode / metadata helpers for ingest_pdf_file."""

from research_library.library.pdf_ingest import preresolved_from_manual_bibcode


def test_preresolved_from_manual_bibcode_blank():
    r = preresolved_from_manual_bibcode("")
    assert r["ok"] is False
    assert "empty" in (r.get("error") or "")
