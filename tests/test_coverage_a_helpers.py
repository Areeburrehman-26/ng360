from core.bridge_bot import _coverage_a_digits, _coverage_a_from_contact


def test_coverage_a_digits():
    assert _coverage_a_digits("$512,000") == "512000"
    assert _coverage_a_digits("") is None
    assert _coverage_a_digits("0") is None


def test_coverage_a_from_contact():
    assert _coverage_a_from_contact({"coverage_a": "452937"}) == "452937"
    assert _coverage_a_from_contact({}) is None
