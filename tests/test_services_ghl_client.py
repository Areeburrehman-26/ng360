import services.ghl_client as ghl
from ghl_contact_fieldids import (
    FIELD_ID_COVERAGE_A,
    FIELD_ID_DRV1_LIC_NUM,
    FIELD_ID_VEH1_MAKE,
    FIELD_ID_VEH1_YEAR,
)
from tests.conftest import run_async


def test_get_custom_field_by_id_and_existing_quote(monkeypatch):
    contact = {"customFields": [{"id": "X", "value": "123"}]}
    monkeypatch.setattr(ghl, "FIELD_ID_PRICE", "X")
    assert ghl.get_custom_field_by_id(contact, "X") == "123"
    assert ghl.has_existing_quote(contact) is True


def test_has_existing_quote_uses_ng_auto_price(monkeypatch):
    contact = {"customFields": [{"id": "Y", "value": "50"}]}
    monkeypatch.setattr(ghl, "FIELD_ID_PRICE", "X")
    monkeypatch.setattr(ghl, "FIELD_ID_NG_QUOTE_PRICE", "Y")
    assert ghl.has_existing_quote(contact) is True


def test_enrich_contact_from_custom_fields_builds_vehicles():
    contact = {
        "firstName": "Ann",
        "lastName": "Smith",
        "customFields": [
            {"id": FIELD_ID_VEH1_YEAR, "value": "2020"},
            {"id": FIELD_ID_VEH1_MAKE, "value": "Honda"},
            {"id": FIELD_ID_DRV1_LIC_NUM, "value": "123456789"},
        ],
    }
    out = ghl.enrich_contact_from_custom_fields(contact)
    assert len(out["vehicles"]) >= 1
    assert out["vehicles"][0]["year"] == "2020"
    assert out["vehicles"][0]["make"] == "Honda"
    assert out["driverLicenseNumber"] == "123456789"


def test_enrich_contact_from_custom_fields_maps_coverage_a():
    contact = {
        "customFields": [
            {"id": FIELD_ID_COVERAGE_A, "value": "$512,000"},
        ],
    }
    out = ghl.enrich_contact_from_custom_fields(contact)
    assert out["coverage_a"] == "$512,000"


def test_has_instant_autofill_tag():
    assert ghl.has_instant_autofill_tag({"tags": ["instantautofill"]}) is True
    assert ghl.has_instant_autofill_tag({"tags": []}) is False


def test_record_successful_quote_maps_fields_and_tag(monkeypatch):
    monkeypatch.setattr(ghl, "FIELD_ID_PRICE", "p")
    monkeypatch.setattr(ghl, "FIELD_ID_QUOTE_STATUS", "s")
    monkeypatch.setattr(ghl, "FIELD_ID_NG_QUOTE_PRICE", "n")
    monkeypatch.setattr(ghl, "FIELD_ID_AUTO_QUOTE_STATUS", "a")
    monkeypatch.setattr(ghl, "FIELD_ID_AUTO_QUOTE_URL", "u")
    monkeypatch.setattr(ghl, "FIELD_ID_NG_QUOTE_PDF", "f")

    captured = {}

    async def fake_update(contact_id, updates):
        captured["updates"] = updates

    async def fake_tag(contact_id, tag):
        captured["tag"] = tag

    monkeypatch.setattr(ghl, "update_contact_fields", fake_update)
    monkeypatch.setattr(ghl, "add_tag_to_contact", fake_tag)

    run_async(
        ghl.record_successful_quote(
            contact_id="c1",
            total_premium="$100",
            home_premium="$60",
            auto_premium="$40",
            drive_url="url",
            pay_plan="AS Monthly",
        )
    )

    assert captured["updates"]["p"] == "$100"
    assert captured["updates"]["s"] == ghl.STATUS_COMPLETED
    assert captured["updates"]["n"] == "$40"
    assert captured["updates"]["a"] == ghl.STATUS_COMPLETED
    assert captured["updates"]["u"] == "url"
    assert captured["updates"]["f"] == "url"
    assert captured["tag"] == ghl.TAG_NG_SUCCESS


def test_record_successful_quote_omits_auto_price_when_empty(monkeypatch):
    monkeypatch.setattr(ghl, "FIELD_ID_PRICE", "p")
    monkeypatch.setattr(ghl, "FIELD_ID_QUOTE_STATUS", "s")
    monkeypatch.setattr(ghl, "FIELD_ID_NG_QUOTE_PRICE", "n")
    monkeypatch.setattr(ghl, "FIELD_ID_AUTO_QUOTE_STATUS", "a")

    captured = {}

    async def fake_update(contact_id, updates):
        captured["updates"] = updates

    async def fake_tag(contact_id, tag):
        captured["tag"] = tag

    monkeypatch.setattr(ghl, "update_contact_fields", fake_update)
    monkeypatch.setattr(ghl, "add_tag_to_contact", fake_tag)

    run_async(
        ghl.record_successful_quote(
            contact_id="c1",
            total_premium="$100",
            home_premium="$60",
            auto_premium="",
            drive_url="",
        )
    )

    assert "n" not in captured["updates"]
    assert captured["updates"]["p"] == "$100"
    assert captured["updates"]["s"] == ghl.STATUS_COMPLETED


def test_record_failed_quote_missing_data_tag(monkeypatch):
    captured = {}

    async def fake_update(contact_id, updates):
        captured["updates"] = updates

    async def fake_tag(contact_id, tag):
        captured["tag"] = tag

    monkeypatch.setattr(ghl, "FIELD_ID_QUOTE_STATUS", "s")
    monkeypatch.setattr(ghl, "FIELD_ID_AUTO_QUOTE_STATUS", "a")
    monkeypatch.setattr(ghl, "update_contact_fields", fake_update)
    monkeypatch.setattr(ghl, "add_tag_to_contact", fake_tag)

    run_async(ghl.record_failed_quote("c1", reason="x", missing_data=True))
    assert captured["updates"]["s"] == ghl.STATUS_FAILED
    assert captured["updates"]["a"] == ghl.STATUS_FAILED
    assert captured["tag"] == ghl.TAG_NG_MISSING_DATA


def test_record_processing_started_applies_processing_tag(monkeypatch):
    captured = {}

    async def fake_tag(contact_id, tag):
        captured["contact_id"] = contact_id
        captured["tag"] = tag

    monkeypatch.setattr(ghl, "add_tag_to_contact", fake_tag)

    run_async(ghl.record_processing_started("c9"))
    assert captured["contact_id"] == "c9"
    assert captured["tag"] == ghl.TAG_NG_PROCESSING


def test_record_ineligible_contact_updates_and_tags(monkeypatch):
    captured = {}

    async def fake_update(contact_id, updates):
        captured["updates"] = updates

    async def fake_tag(contact_id, tag):
        captured["tag"] = tag

    monkeypatch.setattr(ghl, "FIELD_ID_NOT_ELIGIBLE", "n")
    monkeypatch.setattr(ghl, "FIELD_ID_QUOTE_STATUS", "s")
    monkeypatch.setattr(ghl, "FIELD_ID_AUTO_QUOTE_STATUS", "a")
    monkeypatch.setattr(ghl, "update_contact_fields", fake_update)
    monkeypatch.setattr(ghl, "add_tag_to_contact", fake_tag)

    run_async(ghl.record_ineligible_contact("c2", "ineligible state 'TX'"))
    assert captured["updates"]["n"] == "ineligible state 'TX'"
    assert captured["updates"]["s"] == ghl.STATUS_INELIGIBLE
    assert captured["updates"]["a"] == ghl.STATUS_INELIGIBLE
    assert captured["tag"] == ghl.TAG_NG_NOT_ELIGIBLE
