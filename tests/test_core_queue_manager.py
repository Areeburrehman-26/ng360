import json
from pathlib import Path

from core.queue_manager import JobStatus, Priority, QueueManager
from tests.conftest import run_async


def test_load_and_recover_resets_processing_jobs(tmp_path):
    queue_file = tmp_path / "ng360_queue.json"
    queue_file.write_text(
        json.dumps([
            {
                "job_id": "1",
                "contact_id": "c1",
                "priority": 1,
                "status": "PROCESSING",
                "attempts": 0,
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
            }
        ]),
        encoding="utf-8",
    )

    qm = QueueManager(queue_file=queue_file)
    run_async(qm.load_and_recover())

    data = json.loads(queue_file.read_text(encoding="utf-8"))
    assert data[0]["status"] == JobStatus.PENDING


def test_enqueue_and_get_next_pending_priority_order(tmp_path):
    qm = QueueManager(queue_file=tmp_path / "q.json")
    run_async(qm.enqueue("c-low", Priority.LOW))
    run_async(qm.enqueue("c-high", Priority.HIGH))

    nxt = run_async(qm.get_next_pending())
    assert nxt is not None
    assert nxt.contact_id == "c-high"


def test_mark_failed_retries_then_terminal_failure(tmp_path):
    qm = QueueManager(queue_file=tmp_path / "q.json")
    job = run_async(qm.enqueue("contact-1", Priority.HIGH))

    run_async(qm.mark_failed(job.job_id, "e1"))
    first = run_async(qm.get_next_pending())
    assert first is not None
    assert first.priority == Priority.LOW

    run_async(qm.mark_failed(job.job_id, "e2"))
    run_async(qm.mark_failed(job.job_id, "e3"))

    statuses = run_async(qm.get_status())
    assert statuses["failed"] == 1


def test_is_contact_already_queued(tmp_path):
    qm = QueueManager(queue_file=tmp_path / "q.json")
    run_async(qm.enqueue("abc", Priority.HIGH))
    assert qm.is_contact_already_queued("abc") is True
    assert qm.is_contact_already_queued("missing") is False
