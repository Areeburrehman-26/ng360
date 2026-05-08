import json
from pathlib import Path

import scripts.force_requeue_pending as script


def test_force_requeue_pending_no_file(tmp_path):
    script.force_requeue_pending(tmp_path / "missing.json")


def test_force_requeue_pending_resets_processing(tmp_path):
    qf = tmp_path / "queue.json"
    qf.write_text(
        json.dumps(
            [
                {"job_id": "1", "status": "PROCESSING", "first_name": "A", "last_name": "B"},
                {"job_id": "2", "status": "PENDING", "first_name": "C", "last_name": "D"},
            ]
        )
    )

    script.force_requeue_pending(qf)
    jobs = json.loads(qf.read_text())
    assert jobs[0]["status"] == "PENDING"
    backups = list(tmp_path.glob("quote_queue_backup_*.json"))
    assert backups, "Expected backup file"


def test_force_requeue_pending_invalid_json_exits(tmp_path):
    qf = tmp_path / "queue.json"
    qf.write_text("{bad")

    try:
        script.force_requeue_pending(qf)
    except SystemExit:
        pass
    else:
        assert False, "Expected SystemExit"
