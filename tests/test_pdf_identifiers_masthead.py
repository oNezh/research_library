"""Masthead-based title / DOI ordering for :func:`extract_pdf_identifiers`."""

from research_library.library.pdf_identifiers import (
    _split_front_matter,
    _title_from_front_matter,
    extract_pdf_identifiers,
)


def test_title_from_mnras_masthead():
    front = """
5
2
MNRAS 000, 1–14 (2025)
Preprint 30 April 2025
Compiled using MNRAS LATEX style file v3.3
Tidal structures of six globular clusters from the Wide Field Survey
Telescope (WFST) pilot survey
Zhen Wan1,2, Lulu Fan1,2,5
1Department of Astronomy
ABSTRACT
We study clusters.
"""
    _split_front_matter(front)
    t = _title_from_front_matter(front)
    assert t is not None
    assert "WFST" in t
    assert "Zhen Wan" not in t


def test_extract_skips_raw_doi_when_masthead_title(monkeypatch, tmp_path):
    """Raw strings may list unrelated DOIs first; masthead title must block raw fallback."""
    from research_library.library import pdf_identifiers as pi

    fake_clean = """
MNRAS 000, 1–20 (2025)
Our important paper title about globular clusters here
Author One1
1Department of X, University of Y, Somewhere
ABSTRACT
Text.

1 INTRODUCTION
More.
"""
    monkeypatch.setattr(
        pi,
        "read_pdf_text_layers",
        lambda _p: (fake_clean, "10.1051/0004-6361/202451930 junk", None),
    )
    out = extract_pdf_identifiers(str(tmp_path / "x.pdf"))
    assert out["title_candidate"] and "globular clusters" in out["title_candidate"]
    assert out["doi"] is None
    assert out["arxiv_id"] is None
