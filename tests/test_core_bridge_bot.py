from core.bridge_bot import NG360BridgeBot
from tests.conftest import run_async


def _base_contact():
    return {
        "id": "c1",
        "firstName": "John",
        "lastName": "Doe",
        "postalCode": "30101",
        "dateOfBirth": "06071958",
        "gender": "M",
        "maritalStatus": "Single",
        "occupation": "Other",
        "phone": "6626076394",
        "address1": "123 Main St",
        "city": "Acworth",
        "email": "john@example.com",
        "vehicles": [{"ownership_status": 3, "annual_mileage": 10000, "purchase_date": "03/01/2024"}],
    }


def test_contact_value_and_vehicles_helpers():
    bot = NG360BridgeBot(_base_contact())
    assert bot._contact_value("firstName") == "John"
    assert len(bot._vehicles()) == 1
    assert bot._slash_date("01022026") == "01/02/2026"


def test_require_page_raises_without_page():
    bot = NG360BridgeBot(_base_contact())
    try:
        bot._require_page()
    except RuntimeError as exc:
        assert "not initialized" in str(exc)
    else:
        assert False, "Expected RuntimeError"


def test_run_success_contract(monkeypatch):
    bot = NG360BridgeBot(_base_contact())

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(bot, "_start_browser", noop)
    monkeypatch.setattr(bot, "_close_browser", noop)
    monkeypatch.setattr(bot, "_capture_failure_screenshot", noop)

    steps = [
        "_login",
        "_select_state_and_product",
        "_search_and_add_customer",
        "_fill_client_info_page1",
        "_fill_client_info_page2",
        "_handle_prefill_verification",
        "_fill_property_info",
        "_fill_underwriting",
        "_skip_loss_history",
        "_fill_coverage_info",
        "_fill_driver_info",
        "_skip_driver_violations",
        "_fill_vehicle_info",
        "_fill_auto_underwriting",
    ]
    for step in steps:
        monkeypatch.setattr(bot, step, noop)

    async def fake_extract():
        return "$100.00", "$60.00", "$40.00"

    async def fake_pdf():
        return "artifacts/quote.pdf"

    monkeypatch.setattr(bot, "_extract_premiums", fake_extract)
    monkeypatch.setattr(bot, "_download_quote_pdf", fake_pdf)

    result = run_async(bot.run())
    assert result["success"] is True
    assert result["total_premium"] == "$100.00"
    assert result["home_premium"] == "$60.00"
    assert result["auto_premium"] == "$40.00"


def test_run_failure_contract(monkeypatch):
    bot = NG360BridgeBot(_base_contact())
    flag = {"shot": False}

    async def noop(*args, **kwargs):
        return None

    async def boom(*args, **kwargs):
        raise RuntimeError("fail")

    async def shot(*args, **kwargs):
        flag["shot"] = True

    monkeypatch.setattr(bot, "_start_browser", noop)
    monkeypatch.setattr(bot, "_close_browser", noop)
    monkeypatch.setattr(bot, "_capture_failure_screenshot", shot)
    monkeypatch.setattr(bot, "_login", boom)

    result = run_async(bot.run())
    assert result["success"] is False
    assert "fail" in result["error"]
    assert flag["shot"] is True
