"""
Calendar Sync Module for Google Integration.

This module handles:
1. Syncing calendar events from Google Calendar to scheduled_meetings table
2. Sending webhooks on meeting.created, meeting.updated, meeting.cancelled, meeting.rescheduled
3. Called from push notification handler when Google notifies of calendar changes
"""

import os
import re
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List

import httpx
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from shared_models.models import (
    Account,
    AccountUser,
    AccountUserGoogleIntegration,
    ScheduledMeeting,
    ScheduledMeetingStatus,
)
from utils.webhook import send_webhook

logger = logging.getLogger(__name__)

# Google Calendar API
GOOGLE_CALENDAR_API = "https://www.googleapis.com/calendar/v3"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

# How far ahead to sync calendar events (in days)
SYNC_DAYS_AHEAD = int(os.getenv("CALENDAR_SYNC_DAYS_AHEAD", "7"))


def extract_meet_code(text: str) -> Optional[str]:
    """Extract Google Meet code from text (xxx-yyyy-zzz format)."""
    if not text:
        return None
    match = re.search(r"([a-z]{3,4}-[a-z]{4}-[a-z]{3,4})", text)
    return match.group(1) if match else None


def extract_teams_link(text: str) -> Optional[str]:
    """Extract Microsoft Teams meeting link from text."""
    if not text:
        return None
    match = re.search(r"(https?://teams\.microsoft\.com/[^\s]+|https?://teams\.live\.com/[^\s]+)", text)
    return match.group(1) if match else None


async def refresh_access_token(
    integration: AccountUserGoogleIntegration,
    account: Account,
    db: AsyncSession,
) -> Optional[str]:
    """Refresh OAuth access token if needed."""
    # Check if token is still valid
    if integration.token_expires_at and integration.token_expires_at > datetime.now(timezone.utc) + timedelta(
        minutes=5
    ):
        return integration.access_token

    async with httpx.AsyncClient() as client:
        response = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": account.google_client_id,
                "client_secret": account.google_client_secret,
                "refresh_token": integration.refresh_token,
                "grant_type": "refresh_token",
            },
        )

        if response.status_code == 200:
            data = response.json()
            integration.access_token = data["access_token"]
            if "expires_in" in data:
                integration.token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=data["expires_in"])
            await db.commit()
            return integration.access_token

        logger.error(f"Failed to refresh token: {response.text}")
        return None


async def fetch_calendar_events(
    access_token: str,
    days_ahead: int = 7,
) -> List[Dict[str, Any]]:
    """Fetch calendar events from Google Calendar API."""
    time_min = datetime.now(timezone.utc)
    time_max = time_min + timedelta(days=days_ahead)

    params = {
        "timeMin": time_min.isoformat(),
        "timeMax": time_max.isoformat(),
        "maxResults": 250,
        "singleEvents": "true",
        "orderBy": "startTime",
    }

    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{GOOGLE_CALENDAR_API}/calendars/primary/events",
            headers={"Authorization": f"Bearer {access_token}"},
            params=params,
        )

        if response.status_code != 200:
            logger.error(f"Failed to fetch calendar events: {response.text}")
            return []

        data = response.json()

    events = []
    for item in data.get("items", []):
        # Parse event data
        event_data = parse_calendar_event(item)
        if event_data:
            events.append(event_data)

    return events


def parse_calendar_event(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Parse a Google Calendar event item into our format."""
    event_id = item.get("id")
    if not event_id:
        return None

    # Check if cancelled
    is_cancelled = item.get("status") == "cancelled"

    # Extract meeting link and platform
    meet_link = None
    teams_link = None
    native_meeting_id = None
    platform = None

    if not is_cancelled:
        conference_data = item.get("conferenceData", {})
        for entry_point in conference_data.get("entryPoints", []):
            if entry_point.get("entryPointType") == "video":
                uri = entry_point.get("uri", "")
                if "meet.google.com" in uri:
                    meet_link = uri
                    native_meeting_id = extract_meet_code(uri)
                    platform = "google_meet"
                    break
                elif "teams.microsoft.com" in uri or "teams.live.com" in uri:
                    teams_link = uri
                    platform = "teams"
                    break

        # Fallback: check location and description
        if not meet_link and not teams_link:
            location = item.get("location", "")
            description = item.get("description", "")

            meet_code = extract_meet_code(location) or extract_meet_code(description)
            if meet_code:
                native_meeting_id = meet_code
                meet_link = f"https://meet.google.com/{meet_code}"
                platform = "google_meet"
            else:
                teams_link = extract_teams_link(location) or extract_teams_link(description)
                if teams_link:
                    platform = "teams"

    # Parse times
    start = item.get("start", {})
    end = item.get("end", {})
    start_time_str = start.get("dateTime") or start.get("date")
    end_time_str = end.get("dateTime") or end.get("date")

    if not start_time_str:
        return None

    try:
        if "T" in start_time_str:
            start_time = datetime.fromisoformat(start_time_str.replace("Z", "+00:00"))
        else:
            start_time = datetime.strptime(start_time_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)

        end_time = None
        if end_time_str:
            if "T" in end_time_str:
                end_time = datetime.fromisoformat(end_time_str.replace("Z", "+00:00"))
            else:
                end_time = datetime.strptime(end_time_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None

    # Extract attendees
    attendees = []
    for attendee in item.get("attendees", []):
        attendees.append(
            {
                "email": attendee.get("email"),
                "name": attendee.get("displayName"),
                "response_status": attendee.get("responseStatus"),
                "is_organizer": attendee.get("organizer", False),
                "is_self": attendee.get("self", False),
            }
        )

    return {
        "calendar_event_id": event_id,
        "title": item.get("summary", "Untitled Meeting"),
        "description": item.get("description"),
        "platform": platform,
        "native_meeting_id": native_meeting_id,
        "meeting_url": meet_link or teams_link,
        "scheduled_start_time": start_time,
        "scheduled_end_time": end_time,
        "is_creator_self": item.get("creator", {}).get("self", False),
        "is_organizer_self": item.get("organizer", {}).get("self", False),
        "is_cancelled": is_cancelled,
        "attendees": attendees,
    }


def build_webhook_payload(
    event_type: str,
    scheduled_meeting: ScheduledMeeting,
    account_user: AccountUser,
    changes: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build webhook payload for calendar events."""
    payload = {
        "event": event_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "calendar_event": {
            "id": scheduled_meeting.id,
            "calendar_event_id": scheduled_meeting.calendar_event_id,
            "calendar_provider": scheduled_meeting.calendar_provider,
            "title": scheduled_meeting.title,
            "description": scheduled_meeting.description,
            "platform": scheduled_meeting.platform,
            "native_meeting_id": scheduled_meeting.native_meeting_id,
            "meeting_url": scheduled_meeting.meeting_url,
            "scheduled_start_time": scheduled_meeting.scheduled_start_time.isoformat()
            if scheduled_meeting.scheduled_start_time
            else None,
            "scheduled_end_time": scheduled_meeting.scheduled_end_time.isoformat()
            if scheduled_meeting.scheduled_end_time
            else None,
            "is_creator_self": scheduled_meeting.is_creator_self,
            "is_organizer_self": scheduled_meeting.is_organizer_self,
            "status": scheduled_meeting.status,
            "attendees": scheduled_meeting.attendees or [],
        },
        "user": {
            "external_user_id": account_user.external_user_id,
            "account_user_id": account_user.id,
            "account_id": account_user.account_id,
        },
    }

    # Include bot_id if a bot has been spawned (get latest from bot_meetings relationship)
    if scheduled_meeting.bot_meetings:
        # Get the most recent active meeting
        active_meeting = next(
            (m for m in scheduled_meeting.bot_meetings if m.status not in ("completed", "failed")), None
        )
        if active_meeting:
            payload["calendar_event"]["bot_id"] = active_meeting.id

    # Include changes for update events
    if changes:
        payload["changes"] = changes

    return payload


async def sync_calendar_for_user(
    integration: AccountUserGoogleIntegration,
    account_user: AccountUser,
    account: Account,
    db: AsyncSession,
) -> Dict[str, int]:
    """
    Sync calendar events for a user and send webhooks for changes.

    Returns counts of created, updated, cancelled events.
    """
    logger.info(f"Syncing calendar for account_user {account_user.id} (external: {account_user.external_user_id})")

    # Refresh access token if needed
    access_token = await refresh_access_token(integration, account, db)
    if not access_token:
        logger.error(f"Failed to get access token for account_user {account_user.id}")
        return {"created": 0, "updated": 0, "cancelled": 0, "error": "token_refresh_failed"}

    # Fetch calendar events
    events = await fetch_calendar_events(access_token, SYNC_DAYS_AHEAD)
    logger.info(f"Fetched {len(events)} calendar events for account_user {account_user.id}")

    # Get existing scheduled meetings for this integration
    # Include COMPLETED to handle rescheduled meetings (allows reactivation after bot failure)
    # Exclude only CANCELLED (user explicitly cancelled, don't resurrect)
    stmt = select(ScheduledMeeting).where(
        and_(
            ScheduledMeeting.integration_id == integration.id,
            ScheduledMeeting.status != ScheduledMeetingStatus.CANCELLED.value,
        )
    )
    result = await db.execute(stmt)
    existing_meetings = {m.calendar_event_id: m for m in result.scalars().all()}

    counts = {"created": 0, "updated": 0, "cancelled": 0, "rescheduled": 0}
    seen_event_ids = set()

    for event_data in events:
        calendar_event_id = event_data["calendar_event_id"]
        seen_event_ids.add(calendar_event_id)

        # Skip events without video meeting links (based on auto_join_mode)
        if integration.auto_join_mode == "my_events_only":
            if not (event_data["is_creator_self"] or event_data["is_organizer_self"]):
                continue

        # Skip events without meeting links
        if not event_data.get("meeting_url"):
            continue

        existing = existing_meetings.get(calendar_event_id)

        if event_data["is_cancelled"]:
            # Handle cancelled event
            if existing and existing.status != ScheduledMeetingStatus.CANCELLED.value:
                existing.status = ScheduledMeetingStatus.CANCELLED.value
                existing.last_synced_at = datetime.now(timezone.utc)
                await db.commit()
                counts["cancelled"] += 1

                # Send meeting.cancelled webhook
                if account.webhook_url:
                    payload = build_webhook_payload("meeting.cancelled", existing, account_user)
                    send_webhook(account.webhook_url, account.webhook_secret, "meeting.cancelled", payload)

        elif existing:
            # Check for updates
            changes = {}
            is_rescheduled = False

            # Check for time change (rescheduled)
            if existing.scheduled_start_time != event_data["scheduled_start_time"]:
                changes["scheduled_start_time"] = {
                    "old": existing.scheduled_start_time.isoformat() if existing.scheduled_start_time else None,
                    "new": event_data["scheduled_start_time"].isoformat(),
                }
                is_rescheduled = True

            # Check for other updates
            if existing.title != event_data["title"]:
                changes["title"] = {"old": existing.title, "new": event_data["title"]}
            if existing.description != event_data["description"]:
                changes["description"] = {"old": existing.description, "new": event_data["description"]}
            if existing.attendees != event_data["attendees"]:
                changes["attendees"] = {"old": existing.attendees, "new": event_data["attendees"]}

            if changes:
                # Update the record
                existing.title = event_data["title"]
                existing.description = event_data["description"]
                existing.platform = event_data["platform"]
                existing.native_meeting_id = event_data["native_meeting_id"]
                existing.meeting_url = event_data["meeting_url"]
                existing.scheduled_start_time = event_data["scheduled_start_time"]
                existing.scheduled_end_time = event_data["scheduled_end_time"]
                existing.is_creator_self = event_data["is_creator_self"]
                existing.is_organizer_self = event_data["is_organizer_self"]
                existing.attendees = event_data["attendees"]
                existing.last_synced_at = datetime.now(timezone.utc)

                # If rescheduled, reset status to allow new bot spawn at new time
                # - BOT_REQUESTED: Bot was queued but meeting time changed
                # - COMPLETED: Previous bot finished/failed, give it another chance
                # - BOT_ACTIVE: Leave as-is, bot is currently in meeting
                if is_rescheduled and existing.status in (
                    ScheduledMeetingStatus.BOT_REQUESTED.value,
                    ScheduledMeetingStatus.COMPLETED.value,
                ):
                    existing.status = ScheduledMeetingStatus.SCHEDULED.value
                    logger.info(
                        f"Reset scheduled_meeting {existing.id} to SCHEDULED due to reschedule (was {existing.status})"
                    )

                await db.commit()
                await db.refresh(existing)

                # Send appropriate webhook
                if account.webhook_url:
                    if is_rescheduled:
                        counts["rescheduled"] += 1
                        payload = build_webhook_payload("meeting.rescheduled", existing, account_user, changes)
                        send_webhook(account.webhook_url, account.webhook_secret, "meeting.rescheduled", payload)
                    else:
                        counts["updated"] += 1
                        payload = build_webhook_payload("meeting.updated", existing, account_user, changes)
                        send_webhook(account.webhook_url, account.webhook_secret, "meeting.updated", payload)

        else:
            # Create new scheduled meeting
            new_meeting = ScheduledMeeting(
                account_id=account.id,
                account_user_id=account_user.id,
                integration_id=integration.id,
                calendar_event_id=calendar_event_id,
                calendar_provider="google",
                title=event_data["title"],
                description=event_data["description"],
                platform=event_data["platform"],
                native_meeting_id=event_data["native_meeting_id"],
                meeting_url=event_data["meeting_url"],
                scheduled_start_time=event_data["scheduled_start_time"],
                scheduled_end_time=event_data["scheduled_end_time"],
                is_creator_self=event_data["is_creator_self"],
                is_organizer_self=event_data["is_organizer_self"],
                status=ScheduledMeetingStatus.SCHEDULED.value,
                attendees=event_data["attendees"],
                last_synced_at=datetime.now(timezone.utc),
            )
            db.add(new_meeting)
            await db.commit()
            await db.refresh(new_meeting)
            counts["created"] += 1

            # Send meeting.created webhook
            if account.webhook_url:
                payload = build_webhook_payload("meeting.created", new_meeting, account_user)
                send_webhook(account.webhook_url, account.webhook_secret, "meeting.created", payload)

    # Handle events that were deleted from calendar (not in current fetch)
    for calendar_event_id, existing in existing_meetings.items():
        if calendar_event_id not in seen_event_ids:
            # Event was deleted from calendar
            if existing.status not in [ScheduledMeetingStatus.CANCELLED.value, ScheduledMeetingStatus.COMPLETED.value]:
                existing.status = ScheduledMeetingStatus.CANCELLED.value
                existing.last_synced_at = datetime.now(timezone.utc)
                await db.commit()
                counts["cancelled"] += 1

                # Send meeting.cancelled webhook
                if account.webhook_url:
                    payload = build_webhook_payload("meeting.cancelled", existing, account_user)
                    send_webhook(account.webhook_url, account.webhook_secret, "meeting.cancelled", payload)

    logger.info(
        f"Calendar sync complete for account_user {account_user.id}: "
        f"created={counts['created']}, updated={counts['updated']}, "
        f"rescheduled={counts['rescheduled']}, cancelled={counts['cancelled']}"
    )

    return counts
