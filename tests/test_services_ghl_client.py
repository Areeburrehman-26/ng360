import services.ghl_client as ghl
from tests.conftest import run_async


def test_get_custom_field_by_id_and_existing_quote(monkeypatch):
    contact = {"customFields": [{"id": "X", "value": "123"}]}
    monkeypatch.setattr(ghl, "FIELD_ID_PRICE", "X")
    assert ghl.get_custom_field_by_id(contact, "X") == "123"
    assert ghl.has_existing_quote(contact) is True


def test_has_instant_autofill_tag():
    assert ghl.has_instant_autofill_tag({"tags": ["instantautofill"]}) is True
    assert ghl.has_instant_autofill_tag({"tags": []}) is False


def test_record_successful_quote_maps_fields_and_tag(monkeypatch):
    monkeypatch.setattr(ghl, "FIELD_ID_PRICE", "p")
    monkeypatch.setattr(ghl, "FIELD_ID_QUOTE_STATUS", "s")

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
    assert captured["tag"] == ghl.TAG_NG_SUCCESS


def test_record_failed_quote_missing_data_tag(monkeypatch):
    captured = {}

    async def fake_update(contact_id, updates):
        captured["updates"] = updates

    async def fake_tag(contact_id, tag):
        captured["tag"] = tag

    monkeypatch.setattr(ghl, "FIELD_ID_QUOTE_STATUS", "s")
    monkeypatch.setattr(ghl, "update_contact_fields", fake_update)
    monkeypatch.setattr(ghl, "add_tag_to_contact", fake_tag)

    run_async(ghl.record_failed_quote("c1", reason="x", missing_data=True))
    assert captured["updates"]["s"] == ghl.STATUS_FAILED
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
    monkeypatch.setattr(ghl, "update_contact_fields", fake_update)
    monkeypatch.setattr(ghl, "add_tag_to_contact", fake_tag)

    run_async(ghl.record_ineligible_contact("c2", "ineligible state 'TX'"))
    assert captured["updates"]["n"] == "ineligible state 'TX'"
    assert captured["updates"]["s"] == ghl.STATUS_INELIGIBLE
    assert captured["tag"] == ghl.TAG_NG_NOT_ELIGIBLE
