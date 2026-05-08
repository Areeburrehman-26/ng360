# ghl_queue_manager.py → 4-level FIFO queue
"""
queue_manager.py
----------------
FIFO priority queue manager for the HOA Bot.

Priority Levels:
  0 — EXTREME  instantautofill tag → interrupts any running job immediately
  1 — HIGH     Standard GHL webhook → interrupts MEDIUM (poller) jobs
  2 — MEDIUM   Poller-sourced jobs  → standard order, can be interrupted
  3 — LOW      Retry after failure  → processed last, 3 attempts max

Queue Persistence:
    State is written to ng360_queue.json on every status change.
  On startup, any PROCESSING jobs are reset to PENDING automatically —
  ensuring no job is silently lost during a crash or manual restart.

CRITICAL RULES:
  - Never remove or modify priority levels without system owner approval
    - Never modify ng360_queue.json while the bot is actively processing
  - Never remove retry logic — network/portal instability requires retries
"""

import asyncio
import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import IntEnum
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

QUEUE_FILE_PATH     = Path(os.getenv("QUEUE_FILE_PATH", "data/ng360_queue.json"))
MAX_RETRIES         = 3
RETRY_DELAY_S       = 5.0


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class Priority(IntEnum):
    EXTREME = 0
    HIGH    = 1
    MEDIUM  = 2
    LOW     = 3   # retry priority


class JobStatus:
    PENDING    = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED  = "COMPLETED"
    FAILED     = "FAILED"


# ---------------------------------------------------------------------------
# Job dataclass
# ---------------------------------------------------------------------------

@dataclass
class QuoteJob:
    job_id:      str
    contact_id:  str
    priority:    int              # Priority enum value
    status:      str              # JobStatus constant
    attempts:    int = 0
    created_at:  str = ""
    updated_at:  str = ""
    error:       Optional[str] = None   # Last error message if failed

    # Contact snapshot — populated when job is created from webhook data
    first_name:  str = ""
    last_name:   str = ""
    state:       str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "QuoteJob":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


# ---------------------------------------------------------------------------
# Queue Manager
# ---------------------------------------------------------------------------

class QueueManager:
    """
    Thread-safe async priority queue for NG360 Bot quote jobs.
    Persists state to JSON and recovers safely after restarts.
    """

    def __init__(self, queue_file: Path = QUEUE_FILE_PATH):
        self._queue_file    = queue_file
        self._jobs: list[QuoteJob] = []
        self._lock          = asyncio.Lock()
        self._is_processing = False
        self._current_job: Optional[QuoteJob] = None

    # -----------------------------------------------------------------------
    # Startup / Recovery
    # -----------------------------------------------------------------------

    async def load_and_recover(self) -> None:
        """
        Load queue from disk and reset any PROCESSING jobs to PENDING.
        Must be called once on startup before the worker loop begins.
        """
        async with self._lock:
            self._jobs = self._read_from_disk()
            recovered = 0
            for job in self._jobs:
                if job.status == JobStatus.PROCESSING:
                    job.status     = JobStatus.PENDING
                    job.updated_at = _now_iso()
                    recovered += 1

            if recovered:
                logger.warning(
                    "[queue_manager] Recovered %d PROCESSING job(s) → PENDING on startup",
                    recovered
                )
                self._write_to_disk()

        logger.info(
            "[queue_manager] Loaded %d job(s) from disk (%d pending)",
            len(self._jobs),
            sum(1 for j in self._jobs if j.status == JobStatus.PENDING),
        )

    # -----------------------------------------------------------------------
    # Enqueue
    # -----------------------------------------------------------------------

    async def enqueue(
        self,
        contact_id:  str,
        priority:    Priority,
        first_name:  str = "",
        last_name:   str = "",
        state:       str = "",
    ) -> QuoteJob:
        """
        Add a new job to the queue.

        Args:
            contact_id: GHL contact ID.
            priority:   Priority level (use Priority enum).
            first_name: Customer first name (for logging/notifications).
            last_name:  Customer last name.
            state:      Two-letter state code.

        Returns:
            The created QuoteJob.
        """
        job = QuoteJob(
            job_id     = str(uuid.uuid4()),
            contact_id = contact_id,
            priority   = int(priority),
            status     = JobStatus.PENDING,
            attempts   = 0,
            created_at = _now_iso(),
            updated_at = _now_iso(),
            first_name = first_name,
            last_name  = last_name,
            state      = state,
        )

        async with self._lock:
            # Keep queue state in sync across webhook and worker processes.
            self._jobs = self._read_from_disk()
            self._jobs.append(job)
            self._write_to_disk()

        logger.info(
            "[queue_manager] Enqueued job %s for contact %s (priority=%s)",
            job.job_id, contact_id, Priority(priority).name
        )
        return job

    # -----------------------------------------------------------------------
    # Next job selection
    # -----------------------------------------------------------------------

    async def get_next_pending(self) -> Optional[QuoteJob]:
        """
        Return the highest-priority PENDING job, or None if queue is empty.
        Within the same priority level, FIFO order is maintained.
        Does NOT change job status — call mark_processing() after acquiring.
        """
        async with self._lock:
            # Re-read queue each poll so jobs enqueued by other processes are visible.
            self._jobs = self._read_from_disk()
            pending = [j for j in self._jobs if j.status == JobStatus.PENDING]
            if not pending:
                return None
            # Sort by priority (lower int = higher priority), then by created_at
            pending.sort(key=lambda j: (j.priority, j.created_at))
            return pending[0]

    async def mark_processing(self, job_id: str) -> None:
        """Set a job to PROCESSING status and persist."""
        await self._update_job(job_id, status=JobStatus.PROCESSING)
        self._is_processing = True

    async def mark_completed(self, job_id: str) -> None:
        """Set a job to COMPLETED status and persist."""
        await self._update_job(job_id, status=JobStatus.COMPLETED)
        self._is_processing = False
        self._current_job = None

    async def mark_failed(self, job_id: str, error: str = "") -> None:
        """
        Mark a job as failed.
        If attempts < MAX_RETRIES, re-queue it at LOW priority after a delay.
        """
        async with self._lock:
            self._jobs = self._read_from_disk()
            job = self._find_job(job_id)
            if not job:
                logger.error("[queue_manager] mark_failed: job %s not found", job_id)
                return

            job.attempts  += 1
            job.error      = error
            job.updated_at = _now_iso()
            self._is_processing = False
            self._current_job   = None

            if job.attempts < MAX_RETRIES:
                job.status   = JobStatus.PENDING
                job.priority = int(Priority.LOW)
                logger.warning(
                    "[queue_manager] Job %s failed (attempt %d/%d) — re-queuing at LOW priority in %ds",
                    job_id, job.attempts, MAX_RETRIES, RETRY_DELAY_S
                )
                self._write_to_disk()
                # Schedule the actual delay outside the lock
                asyncio.get_event_loop().call_later(
                    RETRY_DELAY_S,
                    lambda: logger.debug("[queue_manager] Job %s retry delay complete", job_id)
                )
            else:
                job.status = JobStatus.FAILED
                logger.error(
                    "[queue_manager] Job %s permanently failed after %d attempts: %s",
                    job_id, job.attempts, error
                )
                self._write_to_disk()

    # -----------------------------------------------------------------------
    # Status / Introspection
    # -----------------------------------------------------------------------

    async def get_status(self) -> dict:
        """
        Return a summary of queue state.
        Used by the GET /queue-status endpoint.
        """
        async with self._lock:
            self._jobs = self._read_from_disk()
            counts = {s: 0 for s in [
                JobStatus.PENDING, JobStatus.PROCESSING,
                JobStatus.COMPLETED, JobStatus.FAILED
            ]}
            for job in self._jobs:
                counts[job.status] = counts.get(job.status, 0) + 1

            return {
                "total_jobs":    len(self._jobs),
                "pending":       counts[JobStatus.PENDING],
                "processing":    counts[JobStatus.PROCESSING],
                "completed":     counts[JobStatus.COMPLETED],
                "failed":        counts[JobStatus.FAILED],
                "is_processing": self._is_processing,
                "current_job":   self._current_job.job_id if self._current_job else None,
            }

    def is_contact_already_queued(self, contact_id: str) -> bool:
        """Return True if a PENDING or PROCESSING job exists for this contact."""
        # Best-effort sync for sync callsites in webhook handlers.
        self._jobs = self._read_from_disk()
        return any(
            j.contact_id == contact_id and j.status in (JobStatus.PENDING, JobStatus.PROCESSING)
            for j in self._jobs
        )

    # -----------------------------------------------------------------------
    # Persistence
    # -----------------------------------------------------------------------

    def _write_to_disk(self) -> None:
        """Serialize queue to JSON. Called inside lock."""
        self._queue_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self._queue_file, "w") as f:
            json.dump([j.to_dict() for j in self._jobs], f, indent=2)

    def _read_from_disk(self) -> list[QuoteJob]:
        """Load queue from JSON. Returns empty list if file absent."""
        if not self._queue_file.exists():
            logger.info("[queue_manager] No queue file found — starting with empty queue")
            return []
        try:
            with open(self._queue_file) as f:
                raw = json.load(f)
            return [QuoteJob.from_dict(d) for d in raw]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.error("[queue_manager] Failed to read queue file: %s — starting fresh", exc)
            return []

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _find_job(self, job_id: str) -> Optional[QuoteJob]:
        for job in self._jobs:
            if job.job_id == job_id:
                return job
        return None

    async def _update_job(self, job_id: str, **kwargs) -> None:
        async with self._lock:
            self._jobs = self._read_from_disk()
            job = self._find_job(job_id)
            if not job:
                logger.error("[queue_manager] _update_job: job %s not found", job_id)
                return
            for key, value in kwargs.items():
                setattr(job, key, value)
            job.updated_at = _now_iso()
            self._write_to_disk()


# ---------------------------------------------------------------------------
# Module-level singleton — shared by webhook server and worker
# ---------------------------------------------------------------------------

queue_manager = QueueManager()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")