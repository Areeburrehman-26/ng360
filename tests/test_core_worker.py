import asyncio
from types import SimpleNamespace

import core.worker as worker
from tests.conftest import run_async


class EndLoop(Exception):
    pass


class FakeQM:
    def __init__(self, job):
        self._job = job
        self.marked_completed = []
        self.marked_failed = []
        self.processed = []

    async def load_and_recover(self):
        return None

    async def get_next_pending(self):
        return self._job

    async def mark_processing(self, job_id):
        self.processed.append(job_id)

    async def mark_completed(self, job_id):
        self.marked_completed.append(job_id)

    async def mark_failed(self, job_id, error=""):
        self.marked_failed.append((job_id, error))


class FakeBot:
    def __init__(self, contact):
        self.contact = contact

    async def run(self):
        return {
            "success": True,
            "total_premium": "$100.00",
            "home_premium": "$60.00",
            "auto_premium": "$40.00",
            "pdf_path": __file__,
            "error": "",
        }


async def _fake_get_contact(contact_id: str):
    return {
        "id": contact_id,
        "firstName": "A",
        "lastName": "B",
        "postalCode": "30101",
        "dateOfBirth": "1980-01-01",
        "gender": "M",
        "maritalStatus": "Single",
        "occupation": "Other",
        "phone": "6626076394",
        "address1": "1 Main St",
        "city": "Acworth",
        "email": "a@example.com",
        "vehicles": [{"ownership_status": 3, "annual_mileage": 10000, "purchase_date": "03/01/2024"}],
        "customFields": [],
    }


async def _stop_sleep(_):
    raise EndLoop()


def test_worker_success_path(monkeypatch):
    job = SimpleNamespace(job_id="j1", first_name="A", last_name="B", state="GA", contact_id="c1")
    qm = FakeQM(job)

    calls = {"ghl": 0, "slack": 0}

    async def fake_record_successful_quote(**kwargs):
        calls["ghl"] += 1

    async def fake_notify_quote_success(**kwargs):
        calls["slack"] += 1

    monkeypatch.setattr(worker, "queue_manager", qm)
    monkeypatch.setattr(worker, "NG360BridgeBot", FakeBot)
    monkeypatch.setattr(worker, "get_contact", _fake_get_contact)
    monkeypatch.setattr(worker, "record_processing_started", lambda *_a, **_k: asyncio.sleep(0))
    monkeypatch.setattr(worker, "record_successful_quote", fake_record_successful_quote)
    monkeypatch.setattr(worker, "notify_quote_success", fake_notify_quote_success)
    monkeypatch.setattr(worker, "upload_quote_pdf", lambda *args, **kwargs: "")
    monkeypatch.setattr(worker, "record_failed_quote", lambda *args, **kwargs: asyncio.sleep(0))
    monkeypatch.setattr(worker, "notify_quote_failure", lambda *args, **kwargs: asyncio.sleep(0))
    monkeypatch.setattr(worker.asyncio, "sleep", _stop_sleep)

    try:
        run_async(worker.run_worker())
    except EndLoop:
        pass

    assert qm.marked_completed == ["j1"]
    assert calls["ghl"] == 1


def test_worker_marks_failed_when_pdf_missing(monkeypatch):
    class BotNoPdf(FakeBot):
        async def run(self):
            return {"success": True, "pdf_path": "", "error": ""}

    job = SimpleNamespace(job_id="j2", first_name="A", last_name="B", state="GA", contact_id="c2")
    qm = FakeQM(job)

    async def fake_record_failed_quote(*args, **kwargs):
        return None

    async def fake_notify_quote_failure(*args, **kwargs):
        return None

    monkeypatch.setattr(worker, "queue_manager", qm)
    monkeypatch.setattr(worker, "NG360BridgeBot", BotNoPdf)
    monkeypatch.setattr(worker, "get_contact", _fake_get_contact)
    monkeypatch.setattr(worker, "record_processing_started", lambda *_a, **_k: asyncio.sleep(0))
    monkeypatch.setattr(worker, "record_failed_quote", fake_record_failed_quote)
    monkeypatch.setattr(worker, "notify_quote_failure", fake_notify_quote_failure)
    monkeypatch.setattr(worker.asyncio, "sleep", _stop_sleep)

    try:
        run_async(worker.run_worker())
    except EndLoop:
        pass

    assert len(qm.marked_failed) == 1
