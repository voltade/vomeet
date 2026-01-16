"""
RQ Task for starting bot containers.

This task is enqueued by google-integration when spawning bots for calendar meetings.
It runs in the bot-manager worker context which has access to the orchestrator.
"""

import os
import logging
import asyncio
from typing import Optional

import redis
import psycopg2
from psycopg2.extras import RealDictCursor

logger = logging.getLogger(__name__)

# Database configuration
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "vomeet")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "postgres")

# Redis configuration for status publishing
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")


def get_db_connection():
    """Get synchronous database connection."""
    return psycopg2.connect(
        host=DB_HOST,
        port=int(DB_PORT),
        database=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
    )


def start_bot_for_meeting(
    meeting_id: int,
    meeting_url: str,
    platform: str,
    native_meeting_id: str,
    bot_name: str = "Notetaker",
) -> dict:
    """
    RQ task to start a bot container for a meeting.

    This task is enqueued by google-integration after it creates the Meeting record.
    The Meeting should already exist in 'requested' status.

    Args:
        meeting_id: ID of the Meeting record
        meeting_url: Full URL to join the meeting
        platform: Meeting platform (google_meet, teams, etc.)
        native_meeting_id: Platform-specific meeting ID
        bot_name: Name to display in the meeting

    Returns:
        dict with status and container_id
    """
    logger.info(f"Starting bot for meeting {meeting_id} (platform={platform})")

    # Import orchestrator here to avoid circular imports
    from app.orchestrators import start_bot_container

    conn = get_db_connection()

    try:
        # Verify meeting exists and is in correct status
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id, status FROM meetings WHERE id = %s", (meeting_id,))
            meeting = cur.fetchone()

            if not meeting:
                logger.error(f"Meeting {meeting_id} not found")
                return {"status": "error", "error": "meeting_not_found"}

            if meeting["status"] not in ("requested", "joining"):
                logger.warning(f"Meeting {meeting_id} in unexpected status: {meeting['status']}")
                return {"status": "skipped", "reason": f"invalid_status_{meeting['status']}"}

        # Start the container (async function, run in event loop)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            container_id = loop.run_until_complete(
                start_bot_container(
                    meeting_id=meeting_id,
                    meeting_url=meeting_url,
                    platform=platform,
                    native_meeting_id=native_meeting_id,
                    bot_name=bot_name,
                )
            )
        finally:
            loop.close()

        if container_id:
            # Update meeting with container ID
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE meetings 
                    SET bot_container_id = %s, updated_at = NOW()
                    WHERE id = %s
                    """,
                    (container_id, meeting_id),
                )
                conn.commit()

            logger.info(f"Started container {container_id} for meeting {meeting_id}")

            # Publish status to Redis
            try:
                redis_client = redis.from_url(REDIS_URL)
                import json
                from datetime import datetime

                payload = {
                    "type": "meeting.status",
                    "meeting": {
                        "id": meeting_id,
                        "platform": platform,
                        "native_id": native_meeting_id,
                    },
                    "payload": {"status": "requested", "container_id": container_id},
                    "ts": datetime.utcnow().isoformat(),
                }
                redis_client.publish(f"bm:meeting:{meeting_id}:status", json.dumps(payload))
            except Exception as e:
                logger.warning(f"Failed to publish status to Redis: {e}")

            return {"status": "success", "container_id": container_id}
        else:
            # Container start failed
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE meetings 
                    SET status = 'failed', 
                        data = data || '{"last_error": {"stage": "container_start", "message": "container_start_returned_none"}}'::jsonb,
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (meeting_id,),
                )
                # Also update scheduled_meeting status
                cur.execute(
                    """
                    UPDATE scheduled_meetings 
                    SET status = 'completed', updated_at = NOW()
                    WHERE id = (SELECT scheduled_meeting_id FROM meetings WHERE id = %s)
                    """,
                    (meeting_id,),
                )
                conn.commit()

            logger.error(f"Failed to start container for meeting {meeting_id}")
            return {"status": "error", "error": "container_start_failed"}

    except Exception as e:
        logger.error(f"Error starting bot for meeting {meeting_id}: {e}", exc_info=True)

        # Update meeting status to failed
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE meetings 
                    SET status = 'failed',
                        data = data || %s::jsonb,
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (f'{{"last_error": {{"stage": "container_start", "message": "{str(e)[:200]}"}}}}', meeting_id),
                )
                cur.execute(
                    """
                    UPDATE scheduled_meetings 
                    SET status = 'completed', updated_at = NOW()
                    WHERE id = (SELECT scheduled_meeting_id FROM meetings WHERE id = %s)
                    """,
                    (meeting_id,),
                )
                conn.commit()
        except Exception:
            pass

        return {"status": "error", "error": str(e)}
    finally:
        conn.close()
