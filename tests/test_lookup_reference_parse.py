from research_library.lookup import parse_reference


def test_parse_mnras_commas():
    p = parse_reference("Baumgardt H., Vasiliev E., 2021, MNRAS, 505, 5957")
    assert p.first_author == "Baumgardt"
    assert p.year == "2021"
    assert p.journal == "MNRAS"
    assert p.volume == "505"
    assert p.page == "5957"
    assert p.issue is None


def test_parse_paren_year():
    p = parse_reference("Smith J. et al. (2020), ApJ, 896, 2")
    assert p.year == "2020"
    assert p.first_author == "Smith"
    assert p.journal == "ApJ"
    assert p.volume == "896"
    assert p.page == "2"


def test_parse_leading_bracket():
    p = parse_reference("[12] Author A., 2019, AJ, 157, 10")
    assert p.first_author == "Author"
    assert p.year == "2019"
    assert p.journal == "AJ"


def test_parse_volume_issue_page():
    p = parse_reference("Someone Y., 2020, ApJ, 500, 2, 123")
    assert p.volume == "500"
    assert p.issue == "2"
    assert p.page == "123"


def test_parse_aa_page_id():
    p = parse_reference("Author Z., 2018, A&A, 614, A74")
    assert p.journal == "A&A"
    assert p.volume == "614"
    assert p.page == "A74"


def test_strip_duplicate_year_token():
    p = parse_reference("Name K., 2020, 2020, MNRAS, 491, 100")
    assert p.journal == "MNRAS"
    assert p.volume == "491"
    assert p.page == "100"
