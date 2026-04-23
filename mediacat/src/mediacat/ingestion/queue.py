"""Redis-backed job queue for ingestion tasks.

Provides a simple async producer/consumer interface built on Redis lists.
Jobs are JSON-serialised dicts with at least ``job_id``, ``connector``,
``action``, and ``payload`` keys.

Reliability
-----------
* Uses ``BLMOVE`` (Redis 6.2+) to atomically move a job from the
  pending queue to a processing queue.  If the worker crashes, the job
  remains in the processing queue for recovery.
* A periodic reaper re-enqueues stale jobs from the processing queue.
* Jobs carry a ``_raw`` attribute holding the exact bytes that entered
  the processing queue, ensuring ``LREM`` always finds the correct entry
  (avoids JSON re-serialisation byte-mismatch).
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# Queue key names in Redis
QUEUE_PENDING = "mediacat:jobs:pending"
QUEUE_PROCESSING = "mediacat:jobs:processing"
QUEUE_DEAD = "mediacat:jobs:dead"
# Hash: job_id -> ISO-8601 timestamp of when the job entered QUEUE_PROCESSING.
# Used by reap_stale() so age is measured from processing start, not creation.
HASH_PROCESSING_TIMES = "mediacat:jobs:processing_times"

# Maximum time a job may sit in the processing queue before the reaper
# considers it stale and re-enqueues it (seconds).
STALE_JOB_TIMEOUT = 600


@dataclass(slots=True)
class Job:
    """A serialisable ingestion job."""

    connector: str
    action: str  # "fetch_release" | "search_releases" | "full_sync"
    payload: dict[str, Any] = field(default_factory=dict)
    job_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    attempt: int = 0
    max_attempts: int = 5

    # The raw bytes that were popped from Redis, used for LREM.
    # Not serialised — internal bookkeeping only.
    _raw: bytes | None = field(default=None, repr=False, compare=False)

    def to_json(self) -> str:
        d = asdict(self)
        d.pop("_raw", None)
        return json.dumps(d, ensure_ascii=False, default=str)

    @classmethod
    def from_json(cls, raw: str | bytes) -> Job:
        raw_bytes = raw if isinstance(raw, bytes) else raw.encode("utf-8")
        data = json.loads(raw_bytes)
        data.pop("_raw", None)
        job = cls(
            **{k: v for k, v in data.items() if k in cls.__dataclass_fields__ and k != "_raw"}
        )
        job._raw = raw_bytes
        return job


class JobQueue:
    """Async interface to the Redis job queue.

    Parameters
    ----------
    redis
        An ``redis.asyncio.Redis`` instance.
    """

    def __init__(self, redis: Any) -> None:
        self._redis = redis

    async def enqueue(self, job: Job) -> str:
        """Add a job to the pending queue.  Returns the job ID."""
        await self._redis.lpush(QUEUE_PENDING, job.to_json())
        logger.info("Enqueued job %s (%s/%s)", job.job_id, job.connector, job.action)
        return job.job_id

    async def dequeue(self, timeout: float = 5.0) -> Job | None:  # noqa: ASYNC109
        """Blocking pop from pending -> processing.  Returns None on timeout."""
        result = await self._redis.blmove(
            QUEUE_PENDING,
            QUEUE_PROCESSING,
            timeout,
            "RIGHT",
            "LEFT",
        )
        if result is None:
            return None
        job = Job.from_json(result)
        # Record when this job entered the processing queue so reap_stale()
        # measures staleness from processing start, not job creation.
        await self._redis.hset(
            HASH_PROCESSING_TIMES,
            job.job_id,
            datetime.now(UTC).isoformat(),
        )
        return job

    async def complete(self, job: Job) -> None:
        """Remove a completed job from the processing queue."""
        # Use the exact raw bytes that were moved into the processing
        # queue, not a re-serialised version (avoids byte-mismatch).
        raw = job._raw or job.to_json().encode("utf-8")
        await self._redis.lrem(QUEUE_PROCESSING, 1, raw)
        await self._redis.hdel(HASH_PROCESSING_TIMES, job.job_id)
        logger.info("Completed job %s", job.job_id)

    async def fail(self, job: Job, *, requeue: bool = True) -> None:
        """Handle a failed job: requeue or send to dead-letter queue."""
        # Remove using original raw bytes BEFORE mutating the job.
        raw = job._raw or job.to_json().encode("utf-8")
        await self._redis.lrem(QUEUE_PROCESSING, 1, raw)
        await self._redis.hdel(HASH_PROCESSING_TIMES, job.job_id)

        job.attempt += 1
        if requeue and job.attempt < job.max_attempts:
            await self._redis.lpush(QUEUE_PENDING, job.to_json())
            logger.warning(
                "Requeued job %s (attempt %d/%d)",
                job.job_id,
                job.attempt,
                job.max_attempts,
            )
        else:
            await self._redis.lpush(QUEUE_DEAD, job.to_json())
            logger.error("Job %s moved to dead-letter queue", job.job_id)

    async def reap_stale(self, max_age_seconds: int = STALE_JOB_TIMEOUT) -> int:
        """Re-enqueue jobs stuck in the processing queue beyond max_age.

        Age is measured from when the job entered the processing queue
        (recorded in HASH_PROCESSING_TIMES), falling back to created_at for
        jobs that pre-date this tracking field.

        Returns the number of jobs reaped.
        """
        raw_items = await self._redis.lrange(QUEUE_PROCESSING, 0, -1)
        # Fetch all processing-start timestamps in one round-trip.
        processing_times: dict[bytes, bytes] = await self._redis.hgetall(HASH_PROCESSING_TIMES)
        reaped = 0
        now = datetime.now(UTC)

        for raw in raw_items:
            try:
                job = Job.from_json(raw)
                # Use the recorded processing-start time; fall back to created_at.
                started_bytes = processing_times.get(job.job_id.encode())
                if started_bytes:
                    started = datetime.fromisoformat(started_bytes.decode())
                else:
                    started = datetime.fromisoformat(job.created_at)
                age = (now - started).total_seconds()
                if age > max_age_seconds:
                    await self._redis.lrem(QUEUE_PROCESSING, 1, raw)
                    await self._redis.hdel(HASH_PROCESSING_TIMES, job.job_id)
                    job.attempt += 1
                    if job.attempt < job.max_attempts:
                        await self._redis.lpush(QUEUE_PENDING, job.to_json())
                        logger.warning("Reaped stale job %s (age=%.0fs)", job.job_id, age)
                    else:
                        await self._redis.lpush(QUEUE_DEAD, job.to_json())
                        logger.error(
                            "Stale job %s exhausted retries, moved to dead-letter", job.job_id
                        )
                    reaped += 1
            except Exception:
                logger.exception("Error reaping job from processing queue")
        return reaped

    async def pending_count(self) -> int:
        return await self._redis.llen(QUEUE_PENDING)  # type: ignore[no-any-return]

    async def processing_count(self) -> int:
        return await self._redis.llen(QUEUE_PROCESSING)  # type: ignore[no-any-return]

    async def dead_count(self) -> int:
        return await self._redis.llen(QUEUE_DEAD)  # type: ignore[no-any-return]
