from fastapi import HTTPException

import core.webhook_server as ws
from tests.conftest import run_async


class FakeQueueManager:
    def __init__(self):
        self.enqueued = []
        self.already = False

    def is_contact_already_queued(self, contact_id: str) -> bool:
        return self.already

    async def enqueue(self, **kwargs):
        self.enqueued.append(kwargs)

    async def get_status(self):
        return {"pending": len(self.enqueued)}


async def _fake_get_contact(contact_id: str):
    return {"firstName": "John", "lastName": "Doe", "customFields": []}


def test_webhook_rejects_missing_contact_id(monkeypatch):
    fake_qm = FakeQueueManager()
    monkeypatch.setattr(ws, "queue_manager", fake_qm)
    monkeypatch.setattr(ws, "get_contact", _fake_get_contact)
    monkeypatch.setattr(ws, "has_existing_quote", lambda _: False)
    monkeypatch.setattr(ws, "is_marked_ineligible", lambda _: False)

    try:
        run_async(ws.webhook({"state": "GA"}))
    except HTTPException as exc:
        assert exc.status_code == 400
    else:
        assert False, "Expected HTTPException"


def test_webhook_rejects_ineligible_state(monkeypatch):
    fake_qm = FakeQueueManager()
    monkeypatch.setattr(ws, "queue_manager", fake_qm)
    monkeypatch.setattr(ws, "record_ineligible_contact", lambda *args, **kwargs: _fake_async_none())

    result = run_async(ws.webhook({"contact_id": "c1", "state": "TX"}))
    assert result["accepted"] is False


async def _fake_async_none():
    return None


def test_webhook_enqueues_when_valid(monkeypatch):
    fake_qm = FakeQueueManager()
    monkeypatch.setattr(ws, "queue_manager", fake_qm)
    monkeypatch.setattr(ws, "get_contact", _fake_get_contact)
    monkeypatch.setattr(ws, "has_existing_quote", lambda _: False)
    monkeypatch.setattr(ws, "has_instant_autofill_tag", lambda _: False)
    monkeypatch.setattr(ws, "is_marked_ineligible", lambda _: False)

    result = run_async(ws.webhook({"contact_id": "c1", "state": "GA"}))
    assert result["accepted"] is True
    assert result["queued"] is True
    assert len(fake_qm.enqueued) == 1
    assert fake_qm.enqueued[0]["priority"] == ws.Priority.HIGH


def test_webhook_uses_contact_state_when_payload_missing_state(monkeypatch):
    fake_qm = FakeQueueManager()
    monkeypatch.setattr(ws, "queue_manager", fake_qm)

    async def _contact_with_state(contact_id: str):
        return {"firstName": "Jane", "lastName": "Smith", "state": "GA", "customFields": []}

    monkeypatch.setattr(ws, "get_contact", _contact_with_state)
    monkeypatch.setattr(ws, "has_existing_quote", lambda _: False)
    monkeypatch.setattr(ws, "has_instant_autofill_tag", lambda _: False)
    monkeypatch.setattr(ws, "is_marked_ineligible", lambda _: False)

    result = run_async(ws.webhook({"contact_id": "c2"}))
    assert result["accepted"] is True
    assert result["queued"] is True
    assert len(fake_qm.enqueued) == 1
    assert fake_qm.enqueued[0]["state"] == "GA"


def test_webhook_enqueues_extreme_for_instantautofill(monkeypatch):
    fake_qm = FakeQueueManager()
    monkeypatch.setattr(ws, "queue_manager", fake_qm)
    monkeypatch.setattr(ws, "get_contact", _fake_get_contact)
    monkeypatch.setattr(ws, "has_existing_quote", lambda _: False)
    monkeypatch.setattr(ws, "is_marked_ineligible", lambda _: False)
    monkeypatch.setattr(ws, "has_instant_autofill_tag", lambda _: True)

    result = run_async(ws.webhook({"contact_id": "c-extreme", "state": "GA"}))
    assert result["accepted"] is True
    assert result["queued"] is True
    assert len(fake_qm.enqueued) == 1
    assert fake_qm.enqueued[0]["priority"] == ws.Priority.EXTREME


def test_queue_status_endpoint(monkeypatch):
    fake_qm = FakeQueueManager()
    monkeypatch.setattr(ws, "queue_manager", fake_qm)

    result = run_async(ws.queue_status())
    assert result == {"pending": 0}
