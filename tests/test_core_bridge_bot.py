from core.bridge_bot import normalize_run_bot_result


def test_quote_proposal_pdf_filename_uses_contact_name():
    from core.bridge_bot import _quote_proposal_contact_name_slug, _quote_proposal_pdf_filename

    slug = _quote_proposal_contact_name_slug(
        {"firstName": "John", "lastName": "Edwards"}
    )
    assert slug == "John_Edwards"
    name = _quote_proposal_pdf_filename(slug, "123456", "20260528_120000")
    assert name == "John_Edwards_quote_proposal_123456_20260528_120000.pdf"


def test_parse_premium_summary_table_rows():
    from core.bridge_bot import _parse_premium_summary_table_rows

    rows = [
        ("Auto Premium", "2,645.82"),
        ("Home Premium", "2,734.00"),
        ("Total", "5,379.82"),
    ]
    out = _parse_premium_summary_table_rows(rows)
    assert out["auto_premium"] == "$2,645.82"
    assert out["home_premium"] == "$2,734.00"
    assert out["premium"] == "$5,379.82"


def test_normalize_run_bot_result_success():
    raw = {
        "premium": "$100.00",
        "pdf_path": "artifacts/quote.pdf",
        "error": None,
        "home_premium": "$60.00",
        "auto_premium": "$40.00",
        "pay_plan": "PIF",
    }
    result = normalize_run_bot_result(raw)
    assert result["success"] is True
    assert result["total_premium"] == "$100.00"
    assert result["home_premium"] == "$60.00"
    assert result["auto_premium"] == "$40.00"
    assert result["pay_plan"] == "PIF"
    assert result["error"] is None


def test_normalize_run_bot_result_error():
    raw = {"premium": None, "pdf_path": None, "error": "login failed"}
    result = normalize_run_bot_result(raw)
    assert result["success"] is False
    assert result["error"] == "login failed"


def test_normalize_run_bot_result_missing_artifacts():
    raw = {"premium": "$100.00", "pdf_path": None, "error": None}
    result = normalize_run_bot_result(raw)
    assert result["success"] is False
    assert result["error"] == "Missing premium or PDF"


def test_normalize_run_bot_result_defaults_home_auto():
    raw = {"premium": "$100.00", "pdf_path": "artifacts/quote.pdf", "error": None}
    result = normalize_run_bot_result(raw)
    assert result["success"] is True
    assert result["home_premium"] == "$0.00"
    assert result["auto_premium"] == "$0.00"
    assert result["pay_plan"] == ""
