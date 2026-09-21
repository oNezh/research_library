"""FTS hyphen sanitization and ADS esource PDF link preference."""

from research_library.library.db import _fts_match_terms
from research_library.lookup import _pdf_links_from_esource_records


def test_fts_match_terms_splits_hyphen():
    assert _fts_match_terms("alpha-enhanced") == "alpha AND enhanced"
    assert "isochrones" in _fts_match_terms("alpha-enhanced isochrones")


def test_ads_pdf_not_overwritten_by_adsscan():
    recs = [
        {
            "link_type": "ADS_PDF",
            "url": "https://articles.adsabs.harvard.edu/pdf/1993ApJ...414..580S",
        },
        {
            "link_type": "ADS_SCAN",
            "url": "https://ui.adsabs.harvard.edu/x/ESOURCE|ADSSCAN",
        },
    ]
    got = _pdf_links_from_esource_records(recs)
    assert "articles.adsabs.harvard.edu/pdf/" in (got["ads"] or "")

    got_rev = _pdf_links_from_esource_records(list(reversed(recs)))
    assert "articles.adsabs.harvard.edu/pdf/" in (got_rev["ads"] or "")
