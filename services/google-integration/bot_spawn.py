"""
Bot Spawn Job for Google Integration.

This module handles:
1. Querying scheduled_meetings for meetings starting within buffer time
2. Creating Meeting records directly in DB
3. Enqueueing RQ jobs for bot-manager to start containers

Runs as a periodic RQ job (every minute).

Architecture:
- google-integration creates Meeting records (has DB access)
- bot-manager worker starts containers (has orchestrator access)
- Communication via RQ job queue (no HTTP calls between services)
"""

import os
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any

from redis import Redis
from rq import Queue
import psycopg2
from psycopg2.extras import RealDictCursor

from shared_models.models import ScheduledMeetingStatus

logger = logging.getLogger(__name__)

# Database configuration
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "vomeet")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "postgres")

# Redis/RQ configuration
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
BOT_MANAGER_QUEUE = os.getenv("BOT_MANAGER_QUEUE", "bot-manager")

# Auto-join timing configuration
AUTO_JOIN_MINUTES_BEFORE = int(os.getenv("AUTO_JOIN_MINUTES_BEFORE", "15"))


def get_db_connection():
    """Get synchronous database connection for RQ worker."""
    return psycopg2.connect(
        host=DB_HOST,
        port=int(DB_PORT),
        database=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
    )


def get_rq_queue() -> Queue:
    """Get RQ queue for bot-manager tasks."""
    redis_conn = Redis.from_url(REDIS_URL)
    return Queue(BOT_MANAGER_QUEUE, connection=redis_conn)


def construct_meeting_url(platform: str, native_meeting_id: str, passcode: Optional[str] = None) -> Optional[str]:
    """Construct meeting URL from platform and native ID."""
    if platform == "google_meet":
        return f"https://meet.google.com/{native_meeting_id}"
    elif platform == "teams":
        # Teams URLs are stored directly in meeting_url field
        return None  # Will use meeting_url from scheduled_meeting
    elif platform == "zoom":
        url = f"https://zoom.us/j/{native_meeting_id}"
        if passcode:
            url += f"?pwd={passcode}"
        return url
    return None


def spawn_bots_for_upcoming_meetings():
    """
    Query scheduled_meetings for calendar meetings starting soon and spawn bots.

    This function:
    1. Finds scheduled meetings ready for bot spawn
    2. Creates Meeting records directly in DB
    3. Enqueues RQ jobs for bot-manager to start containers

    Only processes calendar-synced meetings (calendar_provider != 'api').
    """
    logger.info("Checking for upcoming meetings to spawn bots")

    now = datetime.now(timezone.utc)
    spawn_threshold = now + timedelta(minutes=AUTO_JOIN_MINUTES_BEFORE)

    conn = get_db_connection()
    queue = get_rq_queue()
    spawned_count = 0

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Find scheduled meetings ready for bot spawn
            cur.execute(
                f"""
                SELECT 
                    sm.id,
                    sm.account_id,
                    sm.title,
                    sm.platform,
                    sm.native_meeting_id,
                    sm.meeting_url,
                    sm.scheduled_start_time,
                    sm.scheduled_end_time,
                    sm.data,
                    augi.bot_name,
                    a.max_concurrent_bots
                FROM scheduled_meetings sm
                LEFT JOIN account_user_google_integrations augi ON sm.integration_id = augi.id
                JOIN accounts a ON sm.account_id = a.id
                WHERE sm.status = '{ScheduledMeetingStatus.SCHEDULED.value}'
                  AND sm.calendar_provider != 'api'
                  AND sm.meeting_url IS NOT NULL
                  AND sm.native_meeting_id IS NOT NULL
                  AND sm.platform IS NOT NULL
                  AND sm.scheduled_start_time <= %s
                  AND sm.scheduled_start_time > %s
                  AND NOT EXISTS (
                      SELECT 1 FROM meetings m 
                      WHERE m.scheduled_meeting_id = sm.id 
                      AND m.status IN ('requested', 'joining', 'awaiting_admission', 'active')
                  )
                ORDER BY sm.scheduled_start_time ASC
                LIMIT 50
                """,
                (spawn_threshold, now - timedelta(hours=2)),
            )

            meetings_to_spawn = cur.fetchall()
            logger.info(f"Found {len(meetings_to_spawn)} meetings ready for bot spawn")

            for sm in meetings_to_spawn:
                logger.info(
                    f"Processing scheduled_meeting {sm['id']}: '{sm['title']}' starting at {sm['scheduled_start_time']}"
                )

                # Check concurrency limit
                if sm["max_concurrent_bots"] and sm["max_concurrent_bots"] > 0:
                    cur.execute(
                        """
                        SELECT COUNT(*) FROM meetings 
                        WHERE account_id = %s 
                        AND status IN ('requested', 'joining', 'awaiting_admission', 'active')
                        """,
                        (sm["account_id"],),
                    )
                    active_count = cur.fetchone()[0]
                    if active_count >= sm["max_concurrent_bots"]:
                        logger.warning(
                            f"Account {sm['account_id']} at concurrency limit "
                            f"({active_count}/{sm['max_concurrent_bots']}), skipping"
                        )
                        continue

                # Get bot name
                bot_name = sm.get("bot_name") or "Notetaker"
                if sm.get("data") and isinstance(sm["data"], dict):
                    bot_name = sm["data"].get("bot_name", bot_name)

                # Construct meeting URL
                meeting_url = sm["meeting_url"]
                if not meeting_url:
                    meeting_url = construct_meeting_url(
                        sm["platform"],
                        sm["native_meeting_id"],
                        sm["data"].get("passcode") if sm.get("data") else None,
                    )

                if not meeting_url:
                    logger.error(f"Cannot construct URL for scheduled_meeting {sm['id']}")
                    continue

                # Create Meeting record
                cur.execute(
                    """
                    INSERT INTO meetings (
                        scheduled_meeting_id,
                        account_id,
                        platform,
                        platform_specific_id,
                        scheduled_start_time,
                        scheduled_end_time,
                        status,
                        data,
                        created_at,
                        updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, 'requested', %s, NOW(), NOW())
                    RETURNING id
                    """,
                    (
                        sm["id"],
                        sm["account_id"],
                        sm["platform"],
                        sm["native_meeting_id"],
                        sm["scheduled_start_time"],
                        sm["scheduled_end_time"],
                        "{}",
                    ),
                )
                meeting_id = cur.fetchone()[0]

                # Update scheduled_meeting status
                cur.execute(
                    f"""
                    UPDATE scheduled_meetings 
                    SET status = '{ScheduledMeetingStatus.BOT_REQUESTED.value}',
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (sm["id"],),
                )
                # Don't commit yet - wait for enqueue to succeed

                logger.info(f"Created meeting {meeting_id} for scheduled_meeting {sm['id']}")

                # Enqueue RQ job for bot-manager to start the container
                try:
                    job = queue.enqueue(
                        "app.tasks.start_bot.start_bot_for_meeting",
                        meeting_id=meeting_id,
                        meeting_url=meeting_url,
                        platform=sm["platform"],
                        native_meeting_id=sm["native_meeting_id"],
                        bot_name=bot_name,
                        job_timeout=120,  # 2 minute timeout for container start
                    )
                    # Only commit after successful enqueue
                    conn.commit()
                    logger.info(f"Enqueued start_bot job {job.id} for meeting {meeting_id}")
                    spawned_count += 1
                except Exception as e:
                    logger.error(f"Failed to enqueue job for meeting {meeting_id}: {e}")
                    # Rollback the entire transaction (INSERT + UPDATE)
                    conn.rollback()

    except Exception as e:
        logger.error(f"Error in spawn_bots_for_upcoming_meetings: {e}", exc_info=True)
        conn.rollback()
    finally:
        conn.close()

    logger.info(f"Bot spawn job completed: spawned {spawned_count} bots")
    return {"spawned": spawned_count}


if __name__ == "__main__":
    # For testing
    logging.basicConfig(level=logging.DEBUG)
    spawn_bots_for_upcoming_meetings()
