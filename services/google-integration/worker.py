#!/usr/bin/env python
"""
RQ Worker for Google Integration auto-join jobs.

This script runs the RQ worker that processes auto-join jobs.
Run alongside the main FastAPI service.

Usage:
    python worker.py

Or with rq-scheduler for the periodic check:
    rqscheduler --host redis --port 6379 --db 0

Environment variables:
    REDIS_URL: Redis connection URL (default: redis://localhost:6379/0)
    DATABASE_URL: PostgreSQL connection URL
    BOT_MANAGER_URL: Bot manager service URL
"""

import os
import sys
import logging

from redis import Redis
from rq import Worker, Queue, Connection

# Add the service directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Redis configuration
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")


def run_worker():
    """Run the RQ worker for auto-join queue."""
    redis_conn = Redis.from_url(REDIS_URL)

    # Listen on the auto_join queue
    queues = [Queue("auto_join", connection=redis_conn)]

    logger.info(f"Starting RQ worker, listening on queues: {[q.name for q in queues]}")
    logger.info(f"Redis URL: {REDIS_URL}")

    with Connection(redis_conn):
        worker = Worker(queues)
        worker.work(with_scheduler=True)


if __name__ == "__main__":
    run_worker()
