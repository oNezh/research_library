from research_library.library.pdf_ingest import _doc_dois, _is_arxiv_eprint_bibcode, _prefer_ads_docs


def test_is_arxiv_eprint_bibcode():
    assert _is_arxiv_eprint_bibcode("2024arXiv241108991U")
    assert not _is_arxiv_eprint_bibcode("2025A&A...693A..69A")


def test_prefer_ads_docs_journal_before_eprint():
    docs = [
        {"bibcode": "2024arXiv241108991U", "year": 2024},
        {"bibcode": "2025A&A...693A..69A", "year": 2025},
    ]
    ranked = _prefer_ads_docs(docs)
    assert ranked[0]["bibcode"] == "2025A&A...693A..69A"


def test_doc_dois_from_identifier():
    d = {"doi": ["10.1234/foo"], "identifier": ["doi:10.9999/other"]}
    assert "10.1234/foo" in _doc_dois(d)
    assert "10.9999/other" in _doc_dois(d)
