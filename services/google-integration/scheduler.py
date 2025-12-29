"""
RQ Scheduler for auto-joining Google Meet meetings.

This module contains:
- Job functions for checking upcoming meetings and spawning bots
- Scheduler setup using rq-scheduler
- Webhook notifications for meeting.created events
"""

import os
import logging
import hmac
import hashlib
import json
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any

import httpx
from redis import Redis
from rq import Queue
from rq_scheduler import Scheduler
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from shared_models.models import (
    Account,
    AccountUser,
    AccountUserGoogleIntegration,
)

# Configure logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Environment configuration
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
BOT_MANAGER_URL = os.getenv("BOT_MANAGER_URL", "http://bot-manager:8000")

# Database configuration - individual components for passwords with special chars
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "vomeet")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "postgres")

# Legacy: DATABASE_URL for backwards compatibility
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/vomeet")

# Auto-join timing configuration
AUTO_JOIN_MINUTES_BEFORE = int(os.getenv("AUTO_JOIN_MINUTES_BEFORE", "2"))  # Join X minutes before meeting starts
AUTO_JOIN_CHECK_INTERVAL = int(os.getenv("AUTO_JOIN_CHECK_INTERVAL", "60"))  # Check every 60 seconds

# Google Calendar API
GOOGLE_CALENDAR_API = "https://www.googleapis.com/calendar/v3"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


def get_sync_db_url() -> str:
    """Convert async database URL to sync for RQ workers."""
    db_url = DATABASE_URL
    if db_url.startswith("postgresql+asyncpg://"):
        return db_url.replace("postgresql+asyncpg://", "postgresql://")
    return db_url


def get_redis_connection() -> Redis:
    """Get Redis connection for RQ."""
    return Redis.from_url(REDIS_URL)


def get_scheduler() -> Scheduler:
    """Get RQ scheduler instance."""
    conn = get_redis_connection()
    return Scheduler(connection=conn)


def get_queue() -> Queue:
    """Get RQ queue for auto-join jobs."""
    conn = get_redis_connection()
    return Queue("auto_join", connection=conn)


def compute_signature(payload: str, secret: str) -> str:
    """Compute HMAC-SHA256 signature for webhook payload."""
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def send_webhook(
    webhook_url: str,
    webhook_secret: Optional[str],
    event_type: str,
    payload: Dict[str, Any],
) -> bool:
    """Send a webhook notification."""
    try:
        payload_json = json.dumps(payload, default=str)

        headers = {
            "Content-Type": "application/json",
            "X-Vomeet-Event": event_type,
            "X-Vomeet-Timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # Add HMAC signature if secret is configured
        if webhook_secret:
            signature = compute_signature(payload_json, webhook_secret)
            headers["X-Vomeet-Signature"] = f"sha256={signature}"

        with httpx.Client() as client:
            response = client.post(
                webhook_url,
                content=payload_json,
                headers=headers,
                timeout=30.0,
            )

            if 200 <= response.status_code < 300:
                logger.info(f"Successfully sent {event_type} webhook to {webhook_url}")
                return True
            else:
                logger.warning(f"{event_type} webhook to {webhook_url} returned status {response.status_code}")
                return False

    except httpx.RequestError as e:
        logger.error(f"Failed to send {event_type} webhook to {webhook_url}: {e}")
        return False


def refresh_token_sync(
    refresh_token: str,
    client_id: str,
    client_secret: str,
) -> Optional[str]:
    """Synchronously refresh OAuth access token."""
    with httpx.Client() as client:
        response = client.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
        if response.status_code == 200:
            return response.json().get("access_token")
        logger.error(f"Failed to refresh token: {response.text}")
        return None


def get_upcoming_meets_sync(
    access_token: str,
    minutes_ahead: int = 15,
) -> list:
    """Synchronously fetch upcoming Google Meet events."""
    time_min = datetime.now(timezone.utc)
    time_max = time_min + timedelta(minutes=minutes_ahead)

    params = {
        "timeMin": time_min.isoformat(),
        "timeMax": time_max.isoformat(),
        "maxResults": 50,
        "singleEvents": "true",
        "orderBy": "startTime",
    }

    with httpx.Client() as client:
        response = client.get(
            f"{GOOGLE_CALENDAR_API}/calendars/primary/events",
            headers={"Authorization": f"Bearer {access_token}"},
            params=params,
        )

        if response.status_code != 200:
            logger.error(f"Failed to fetch calendar events: {response.text}")
            return []

        data = response.json()

    events = []
    total_events = len(data.get("items", []))
    logger.info(f"Found {total_events} total calendar events in next {minutes_ahead} minutes")

    for item in data.get("items", []):
        logger.debug(f"Processing event: {item.get('summary', 'Untitled')} (ID: {item.get('id')})")

        if item.get("status") == "cancelled":
            logger.debug(f"Skipping cancelled event: {item.get('summary', 'Untitled')}")
            continue

        # Extract Google Meet link
        meet_link = None
        native_meeting_id = None

        conference_data = item.get("conferenceData", {})
        for entry_point in conference_data.get("entryPoints", []):
            if entry_point.get("entryPointType") == "video":
                uri = entry_point.get("uri", "")
                if "meet.google.com" in uri:
                    meet_link = uri
                    # Extract meeting code (xxx-yyyy-zzz format)
                    import re

                    match = re.search(r"([a-z]{3,4}-[a-z]{4}-[a-z]{3,4})", uri)
                    if match:
                        native_meeting_id = match.group(1)
                    break

        if not meet_link or not native_meeting_id:
            continue

        # Parse start time
        start = item.get("start", {})
        start_time_str = start.get("dateTime") or start.get("date")
        if not start_time_str:
            continue

        try:
            if "T" in start_time_str:
                start_time = datetime.fromisoformat(start_time_str.replace("Z", "+00:00"))
            else:
                start_time = datetime.strptime(start_time_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue

        # Extract attendees
        attendees = []
        for attendee in item.get("attendees", []):
            attendee_info = {
                "email": attendee.get("email"),
                "name": attendee.get("displayName"),
                "response_status": attendee.get("responseStatus"),  # needsAction, declined, tentative, accepted
                "is_organizer": attendee.get("organizer", False),
                "is_self": attendee.get("self", False),
            }
            attendees.append(attendee_info)

        events.append(
            {
                "event_id": item["id"],
                "summary": item.get("summary", "Untitled Meeting"),
                "start_time": start_time,
                "native_meeting_id": native_meeting_id,
                "meet_link": meet_link,
                "is_creator_self": item.get("creator", {}).get("self", False),
                "is_organizer_self": item.get("organizer", {}).get("self", False),
                "attendees": attendees,
            }
        )

    return events


def spawn_bot_sync(
    api_key: str,
    native_meeting_id: str,
    bot_name: str,
    event_summary: str,
) -> Optional[Dict[str, Any]]:
    """Synchronously call bot-manager to spawn a bot.

    Returns the meeting data on success, None on failure.
    """
    logger.info(f"Spawning bot for meeting '{event_summary}' ({native_meeting_id}) with name '{bot_name}'")

    with httpx.Client() as client:
        request_data = {
            "platform": "google_meet",
            "native_meeting_id": native_meeting_id,
            "bot_name": bot_name,
        }
        logger.debug(f"Bot spawn request: {request_data}")

        response = client.post(
            f"{BOT_MANAGER_URL}/bots",
            headers={"X-API-Key": api_key},
            json=request_data,
            timeout=30.0,
        )

        logger.info(f"Bot spawn response status: {response.status_code}")

        if response.status_code == 201:
            logger.info(f"Successfully spawned bot for meeting '{event_summary}' ({native_meeting_id})")
            return response.json()
        elif response.status_code == 409:
            logger.info(f"Bot already exists for meeting '{event_summary}' ({native_meeting_id})")
            return {"already_exists": True}  # Not an error, bot is already there
        else:
            logger.error(f"Failed to spawn bot for '{event_summary}': {response.status_code} - {response.text}")
            return None


def process_auto_join_for_user(
    account_user_id: int,
    account_id: int,
    external_user_id: str,
    refresh_token: str,
    client_id: str,
    client_secret: str,
    api_key: str,
    bot_name: str,
    auto_join_mode: str,
    webhook_url: Optional[str] = None,
    webhook_secret: Optional[str] = None,
):
    """
    Process auto-join for a single user.
    This function is enqueued as an RQ job.
    """
    logger.info(f"Processing auto-join for account_user {account_user_id} (external: {external_user_id})")

    # Refresh access token
    access_token = refresh_token_sync(refresh_token, client_id, client_secret)
    if not access_token:
        logger.error(f"Failed to refresh token for account_user {account_user_id}")
        return

    # Get upcoming meetings (within next 15 minutes)
    events = get_upcoming_meets_sync(access_token, minutes_ahead=15)
    if not events:
        logger.info(f"No upcoming Google Meet events found for account_user {account_user_id}")
        return

    logger.info(f"Found {len(events)} upcoming Google Meet events for account_user {account_user_id}")

    for event in events:
        logger.info(
            f"Processing event: '{event['summary']}' starting at {event['start_time']} (ID: {event['native_meeting_id']})"
        )

    now = datetime.now(timezone.utc)
    join_threshold = now + timedelta(minutes=AUTO_JOIN_MINUTES_BEFORE)

    # Get Redis connection for deduplication
    redis_conn = get_redis_connection()

    # Dedup TTL: 20 minutes (slightly longer than 15-min look-ahead window + buffer for clock skew)
    # Short enough to allow retries if bot fails, long enough to prevent double-spawning
    DEDUP_TTL_SECONDS = 1200  # 20 minutes

    for event in events:
        logger.debug(f"Evaluating event '{event['summary']}' at {event['start_time']} (threshold: {join_threshold})")

        # Skip if meeting hasn't started yet and is more than AUTO_JOIN_MINUTES_BEFORE away
        if event["start_time"] > join_threshold:
            logger.debug(
                f"Skipping '{event['summary']}' - starts at {event['start_time']}, too early to join (threshold: {join_threshold})"
            )
            continue

        # Apply auto_join_mode filter
        if auto_join_mode == "my_events_only":
            if not (event["is_creator_self"] or event["is_organizer_self"]):
                logger.debug(f"Skipping '{event['summary']}' - user is not creator/organizer (mode: my_events_only)")
                continue

        # Event-based deduplication: Use event_id + start_time to handle rescheduled meetings
        # This ensures that if the same meeting is rescheduled to a new time, we'll join it again
        event_start_iso = event["start_time"].isoformat()
        event_start_key = event["start_time"].strftime("%Y%m%d%H%M")  # Compact key for Redis dedup
        dedup_key = f"auto_join:spawned:{account_id}:{event['event_id']}:{event_start_key}"

        # Also track the previous start time for this event to detect rescheduling (stored as ISO)
        event_time_key = f"auto_join:event_time:{account_id}:{event['event_id']}"
        previous_start_time = redis_conn.get(event_time_key)

        is_rescheduled = False
        previous_start_iso = None
        if previous_start_time:
            previous_start_iso = (
                previous_start_time.decode("utf-8") if isinstance(previous_start_time, bytes) else previous_start_time
            )
            if previous_start_iso != event_start_iso:
                is_rescheduled = True
                logger.info(
                    f"Meeting '{event['summary']}' was rescheduled from {previous_start_iso} to {event_start_iso}"
                )

        if redis_conn.exists(dedup_key) and not is_rescheduled:
            logger.debug(f"Skipping '{event['summary']}' - already attempted to join (dedup key exists)")
            continue

        logger.info(
            f"Auto-joining meeting '{event['summary']}' ({event['native_meeting_id']}) for user {external_user_id}"
        )

        # Spawn the bot
        logger.info(f"Calling spawn_bot_sync for meeting {event['native_meeting_id']}")
        meeting_data = spawn_bot_sync(
            api_key=api_key,
            native_meeting_id=event["native_meeting_id"],
            bot_name=bot_name or "Notetaker",
            event_summary=event["summary"],
        )

        if meeting_data:
            logger.info(f"Bot spawn successful for meeting {event['native_meeting_id']}")
        else:
            logger.error(f"Bot spawn failed for meeting {event['native_meeting_id']}")

        # Mark as attempted with 30 minute TTL
        if meeting_data:
            redis_conn.setex(dedup_key, DEDUP_TTL_SECONDS, "1")
            # Store the current start time (ISO format) for future rescheduling detection (24 hour TTL)
            redis_conn.setex(event_time_key, 86400, event_start_iso)

            # Send meeting.rescheduled webhook if this is a rescheduled meeting
            if webhook_url and is_rescheduled:
                reschedule_payload = {
                    "event": "meeting.rescheduled",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "meeting": {
                        "id": meeting_data.get("id"),
                        "bot_id": meeting_data.get("id"),
                        "platform": "google_meet",
                        "native_meeting_id": event["native_meeting_id"],
                        "meeting_url": meeting_data.get("constructed_meeting_url"),
                        "status": meeting_data.get("status"),
                        "created_at": meeting_data.get("created_at"),
                    },
                    "calendar_event": {
                        "event_id": event["event_id"],
                        "title": event["summary"],
                        "scheduled_at": event_start_iso,
                        "previous_scheduled_at": previous_start_iso,
                        "is_creator_self": event.get("is_creator_self", False),
                        "is_organizer_self": event.get("is_organizer_self", False),
                        "attendees": event.get("attendees", []),
                    },
                    "user": {
                        "external_user_id": external_user_id,
                        "account_user_id": account_user_id,
                        "account_id": account_id,
                    },
                }
                send_webhook(webhook_url, webhook_secret, "meeting.rescheduled", reschedule_payload)

            # Send meeting.created webhook if not already exists and webhook is configured
            elif webhook_url and not meeting_data.get("already_exists"):
                webhook_payload = {
                    "event": "meeting.created",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "meeting": {
                        "id": meeting_data.get("id"),
                        "bot_id": meeting_data.get("id"),  # bot_id is the meeting id
                        "platform": "google_meet",
                        "native_meeting_id": event["native_meeting_id"],
                        "meeting_url": meeting_data.get("constructed_meeting_url"),
                        "status": meeting_data.get("status"),
                        "created_at": meeting_data.get("created_at"),
                    },
                    "calendar_event": {
                        "event_id": event["event_id"],
                        "title": event["summary"],
                        "scheduled_at": event["start_time"].isoformat(),
                        "is_creator_self": event.get("is_creator_self", False),
                        "is_organizer_self": event.get("is_organizer_self", False),
                        "attendees": event.get("attendees", []),
                    },
                    "user": {
                        "external_user_id": external_user_id,
                        "account_user_id": account_user_id,
                        "account_id": account_id,
                    },
                }

                send_webhook(webhook_url, webhook_secret, "meeting.created", webhook_payload)


def check_and_enqueue_auto_joins():
    """
    Main scheduler job that checks all users with auto_join_enabled
    and enqueues individual auto-join jobs.

    This runs periodically via rq-scheduler.
    """
    import psycopg2
    from psycopg2.extras import RealDictCursor

    logger.info("Running auto-join check...")

    # Use individual DB components to avoid URL parsing issues with special chars in password
    conn = psycopg2.connect(
        host=DB_HOST,
        port=int(DB_PORT),
        database=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
    )

    queue = get_queue()

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Find all users with auto_join_enabled
            cur.execute("""
                SELECT 
                    augi.id as integration_id,
                    augi.account_user_id,
                    augi.refresh_token,
                    augi.bot_name,
                    augi.auto_join_mode,
                    au.external_user_id,
                    au.account_id,
                    a.api_key,
                    a.google_client_id,
                    a.google_client_secret,
                    a.webhook_url,
                    a.webhook_secret
                FROM account_user_google_integrations augi
                JOIN account_users au ON au.id = augi.account_user_id
                JOIN accounts a ON a.id = au.account_id
                WHERE augi.auto_join_enabled = true
                  AND augi.refresh_token IS NOT NULL
                  AND a.google_client_id IS NOT NULL
                  AND a.google_client_secret IS NOT NULL
                  AND a.api_key IS NOT NULL
            """)

            users = cur.fetchall()
            logger.info(f"Found {len(users)} users with auto-join enabled")

            for user in users:
                logger.info(
                    f"Enqueuing auto-join job for account_user {user['account_user_id']} (external: {user['external_user_id']}, mode: {user['auto_join_mode']})"
                )

                # Enqueue individual job for each user
                # Use string path so worker can import the function properly
                queue.enqueue(
                    "scheduler.process_auto_join_for_user",
                    account_user_id=user["account_user_id"],
                    account_id=user["account_id"],
                    external_user_id=user["external_user_id"],
                    refresh_token=user["refresh_token"],
                    client_id=user["google_client_id"],
                    client_secret=user["google_client_secret"],
                    api_key=user["api_key"],
                    bot_name=user["bot_name"],
                    auto_join_mode=user["auto_join_mode"],
                    webhook_url=user["webhook_url"],
                    webhook_secret=user["webhook_secret"],
                    job_timeout=120,  # 2 minute timeout per user
                )
                logger.debug(f"Enqueued auto-join job for account_user {user['account_user_id']}")

    finally:
        conn.close()

    logger.info("Auto-join check completed")


def setup_scheduler():
    """
    Set up the RQ scheduler with periodic auto-join check job.
    Call this on application startup.
    """
    scheduler = get_scheduler()

    # Clear any existing auto-join jobs
    for job in scheduler.get_jobs():
        if "check_and_enqueue_auto_joins" in str(job.func_name):
            scheduler.cancel(job)
            logger.info(f"Cancelled existing scheduler job: {job.id}")

    # Schedule the auto-join check to run every AUTO_JOIN_CHECK_INTERVAL seconds
    # Use string path so worker can import the function properly
    scheduler.schedule(
        scheduled_time=datetime.now(timezone.utc),
        func="scheduler.check_and_enqueue_auto_joins",
        interval=AUTO_JOIN_CHECK_INTERVAL,
        repeat=None,  # Repeat indefinitely
        queue_name="auto_join",
    )

    logger.info(f"Scheduled auto-join check every {AUTO_JOIN_CHECK_INTERVAL} seconds")
    return scheduler


if __name__ == "__main__":
    # Run the scheduler (for testing)
    scheduler = setup_scheduler()
    scheduler.run()
