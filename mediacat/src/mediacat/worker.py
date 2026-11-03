"""Background worker — processes ingestion jobs from the Redis queue.

Entry point: ``python -m mediacat.worker``

This module:
1. Connects to Redis and the database.
2. Polls the job queue (``BLMOVE``-based, at-least-once delivery).
3. Dispatches each job to the appropriate connector.
4. Runs OCR + vision + rule-engine pipelines on fetched data.
5. Writes results to the database and flags items for review.

The worker runs as a separate container in the Docker Compose stack
(see ``deploy/docker-compose.yaml``).
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_shutdown = asyncio.Event()

#: Touched on every loop pass; the compose healthcheck fails when it is older
#: than 60 s (the image's own healthcheck probes the web app's port, which the
#: worker doesn't serve).  /tmp is a tmpfs in the container.
HEARTBEAT = Path(
    "/tmp/mediacat-worker.heartbeat"  # noqa: S108  # container tmpfs, not shared  # nosec B108
)


def _beat() -> None:
    try:
        HEARTBEAT.touch()
    except OSError as exc:  # read-only or missing /tmp: only the healthcheck suffers
        logger.debug("heartbeat not written: %s", exc)


def _handle_signal(sig: int, frame: Any) -> None:
    """Signal handler for graceful shutdown."""
    logger.info("Received signal %s — shutting down", signal.Signals(sig).name)
    _shutdown.set()


async def run_worker() -> None:
    """Main worker loop.

    In the first generation, this is a skeleton that:
    - Logs startup
    - Enters a polling loop
    - Exits on SIGTERM/SIGINT

    Full implementation wires:
    - ``ingestion.queue.JobQueue`` for dequeue
    - ``ingestion.registry.load_connectors`` for dispatch
    - ``storage.pipeline.ImagePipeline`` for media processing
    - ``rules.engine.create_rule_engine`` for decoding
    - ``vision.adapter.HybridVision`` for transcription
    - ``llm.adapter.HybridLlm`` for comparison/anomaly
    """
    logger.info("MediaCat worker starting")

    # Placeholder: poll loop until shutdown
    while not _shutdown.is_set():
        _beat()
        try:
            # In production: job = await queue.dequeue(timeout=5.0)
            await asyncio.wait_for(_shutdown.wait(), timeout=5.0)
        except TimeoutError:
            continue

    logger.info("MediaCat worker stopped")


def main() -> None:
    """CLI entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        logger.info("Worker interrupted")
        sys.exit(0)


if __name__ == "__main__":
    main()
