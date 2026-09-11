import canvas_readings as rd


def test_strips_case_number_parenthetical():
    assert rd._clean_title("Rocky Mountain Condiments (319-029)") == "Rocky Mountain Condiments"
    assert rd._clean_title("Ginkgo Bioworks (215001)") == "Ginkgo Bioworks"


def test_keeps_single_letter_case_designation():
    assert rd._clean_title("Ginkgo Bioworks (A)") == "Ginkgo Bioworks (A)"


def test_strips_bracketed_code():
    assert rd._clean_title("Contracts 101 [224023]") == "Contracts 101"


def test_strips_hbp_attribution():
    assert rd._clean_title("Some Note, HBP 2019") == "Some Note"
    assert rd._clean_title("What Is Strategy, HBR Classic") == "What Is Strategy"


def test_long_title_truncates_at_earliest_break():
    title = "Author Name, A Very Long Book Title About Things That Go On: And a Subtitle Too"
    assert len(title) > 60
    assert rd._clean_title(title) == "Author Name"


def test_session_filename_is_date_prefixed_and_safe():
    assert rd._session_filename("260902", "Rocky Mountain Condiments (319-029)") \
        == "260902 Rocky Mountain Condiments"
    assert rd._session_filename("260908", 'Q: "What/Next?"') == "260908 Q- -What-Next"


def test_config_file_not_written_into_repo():
    """Importing the scripts must not drop canvas_config.json in the checkout."""
    import canvas_refresh  # noqa: F401
    from conftest import ROOT
    assert not (ROOT / "canvas_config.json").exists()
