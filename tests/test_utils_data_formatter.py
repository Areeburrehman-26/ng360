import utils.data_formatter as fmt


def test_normalize_phone_number_valid_and_country_code():
    assert fmt.normalize_phone_number("(662) 607-6394") == "6626076394"
    assert fmt.normalize_phone_number("1-662-607-6394") == "6626076394"


def test_normalize_phone_number_invalid():
    try:
        fmt.normalize_phone_number("123")
    except ValueError:
        pass
    else:
        assert False, "Expected ValueError"


def test_split_phone_number_parts():
    assert fmt.split_phone_number("6626076394") == ("662", "607", "6394")


def test_format_date_mmddyyyy_supports_multiple_inputs():
    assert fmt.format_date_mmddyyyy("01/02/2026") == "01022026"
    assert fmt.format_date_mmddyyyy("01-02-2026") == "01022026"
    assert fmt.format_date_to_mmddyyyy("01022026") == "01022026"


def test_format_address_joins_non_empty_parts():
    assert fmt.format_address("line", "city", "GA", "30101") == "line, city, GA, 30101"
