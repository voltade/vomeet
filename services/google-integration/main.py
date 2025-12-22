"""
Google Calendar Integration Microservice for Vomeet.

Enables users to connect their Google Calendar and auto-join Google Meet meetings.

Scopes used:
- https://www.googleapis.com/auth/calendar.events.readonly - Read calendar events
- email - Get user's email identity
- profile - Get user's name and profile picture
"""

import os
import re
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional
from urllib.parse import urlencode

import httpx
import uvicorn
from fastapi import FastAPI, Depends, HTTPException, status, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared_models.database import get_db
from shared_models.models import User, GoogleIntegration, APIToken
from shared_models.schemas import (
    GoogleAuthUrlResponse,
    GoogleCallbackRequest,
    GoogleIntegrationResponse,
    GoogleIntegrationUpdate,
    CalendarEvent,
    CalendarEventAttendee,
    CalendarEventsResponse,
)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Vomeet Google Integration",
    description="Google Calendar integration for auto-joining Google Meet meetings",
    version="1.0.0",
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Google OAuth configuration
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "https://vomeet.io/google/callback")

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
GOOGLE_CALENDAR_API = "https://www.googleapis.com/calendar/v3"

# OAuth scopes
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/calendar.events.readonly",
    "email",
    "profile",
    "openid",
]


def extract_meet_code(text: Optional[str]) -> Optional[str]:
    """Extract Google Meet code (xxx-yyyy-zzz) from text."""
    if not text:
        return None
    # Match patterns like meet.google.com/xxx-yyyy-zzz
    pattern = r"meet\.google\.com/([a-z]{3}-[a-z]{4}-[a-z]{3})"
    match = re.search(pattern, text.lower())
    if match:
        return match.group(1)
    return None


async def get_current_user_from_api_key(request: Request, db: AsyncSession) -> User:
    """Get current user from X-API-Key header."""
    api_key = request.headers.get("X-API-Key")
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-API-Key header required",
        )

    stmt = select(APIToken).where(APIToken.token == api_key)
    result = await db.execute(stmt)
    token = result.scalar_one_or_none()

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )

    user = await db.get(User, token.user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )

    return user


async def refresh_access_token(integration: GoogleIntegration, db: AsyncSession) -> str:
    """Refresh the access token using the refresh token."""
    if not integration.refresh_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No refresh token available. Please reconnect Google account.",
        )

    async with httpx.AsyncClient() as client:
        response = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "refresh_token": integration.refresh_token,
                "grant_type": "refresh_token",
            },
        )

        if response.status_code != 200:
            logger.error(f"Failed to refresh token: {response.text}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Failed to refresh Google access token. Please reconnect.",
            )

        token_data = response.json()
        integration.access_token = token_data["access_token"]
        if "refresh_token" in token_data:
            integration.refresh_token = token_data["refresh_token"]
        if "expires_in" in token_data:
            integration.token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=token_data["expires_in"])
        await db.commit()

        return integration.access_token


async def get_valid_access_token(integration: GoogleIntegration, db: AsyncSession) -> str:
    """Get a valid access token, refreshing if necessary."""
    if integration.token_expires_at:
        expires_at = integration.token_expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= datetime.now(timezone.utc) + timedelta(minutes=5):
            return await refresh_access_token(integration, db)
    return integration.access_token


# --- Health Check ---
@app.get("/healthz", tags=["Health"])
async def healthz():
    """Health check endpoint."""
    return {"status": "ok"}


# --- OAuth Endpoints ---
@app.get("/auth", response_model=GoogleAuthUrlResponse, tags=["OAuth"])
async def get_google_auth_url(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Get Google OAuth authorization URL.

    Redirect the user to this URL to initiate Google sign-in and calendar access.
    """
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google OAuth not configured",
        )

    user = await get_current_user_from_api_key(request, db)

    # Use user_id as state for CSRF protection
    state = str(user.id)

    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(GOOGLE_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }

    auth_url = f"{GOOGLE_AUTH_URL}?{urlencode(params)}"

    return GoogleAuthUrlResponse(auth_url=auth_url)


@app.post("/callback", response_model=GoogleIntegrationResponse, tags=["OAuth"])
async def google_oauth_callback(
    callback: GoogleCallbackRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Handle Google OAuth callback.

    Exchange the authorization code for access tokens and store the integration.
    """
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google OAuth not configured",
        )

    user = await get_current_user_from_api_key(request, db)

    # Exchange code for tokens
    async with httpx.AsyncClient() as client:
        token_response = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "code": callback.code,
                "grant_type": "authorization_code",
                "redirect_uri": GOOGLE_REDIRECT_URI,
            },
        )

        if token_response.status_code != 200:
            logger.error(f"Token exchange failed: {token_response.text}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Failed to exchange authorization code",
            )

        token_data = token_response.json()

        # Get user info from Google
        userinfo_response = await client.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {token_data['access_token']}"},
        )

        if userinfo_response.status_code != 200:
            logger.error(f"Failed to get userinfo: {userinfo_response.text}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Failed to get Google user info",
            )

        userinfo = userinfo_response.json()

    # Check if integration already exists
    stmt = select(GoogleIntegration).where(GoogleIntegration.user_id == user.id)
    result = await db.execute(stmt)
    integration = result.scalar_one_or_none()

    token_expires_at = None
    if "expires_in" in token_data:
        token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=token_data["expires_in"])

    if integration:
        # Update existing integration
        integration.google_user_id = userinfo["id"]
        integration.email = userinfo.get("email", "")
        integration.name = userinfo.get("name")
        integration.picture = userinfo.get("picture")
        integration.access_token = token_data["access_token"]
        if "refresh_token" in token_data:
            integration.refresh_token = token_data["refresh_token"]
        integration.token_expires_at = token_expires_at
        integration.scopes = GOOGLE_SCOPES
    else:
        # Create new integration
        integration = GoogleIntegration(
            user_id=user.id,
            google_user_id=userinfo["id"],
            email=userinfo.get("email", ""),
            name=userinfo.get("name"),
            picture=userinfo.get("picture"),
            access_token=token_data["access_token"],
            refresh_token=token_data.get("refresh_token"),
            token_expires_at=token_expires_at,
            scopes=GOOGLE_SCOPES,
            auto_join_enabled=False,
        )
        db.add(integration)

    await db.commit()
    await db.refresh(integration)

    return GoogleIntegrationResponse.model_validate(integration)


# --- Integration Status Endpoints ---
@app.get("/status", response_model=Optional[GoogleIntegrationResponse], tags=["Integration"])
async def get_google_integration_status(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Get the current user's Google integration status.

    Returns the integration details or null if not connected.
    """
    user = await get_current_user_from_api_key(request, db)

    stmt = select(GoogleIntegration).where(GoogleIntegration.user_id == user.id)
    result = await db.execute(stmt)
    integration = result.scalar_one_or_none()

    if not integration:
        return None

    return GoogleIntegrationResponse.model_validate(integration)


@app.put("/settings", response_model=GoogleIntegrationResponse, tags=["Integration"])
async def update_google_integration_settings(
    update: GoogleIntegrationUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Update Google integration settings.

    Currently supports enabling/disabling auto-join for meetings.
    """
    user = await get_current_user_from_api_key(request, db)

    stmt = select(GoogleIntegration).where(GoogleIntegration.user_id == user.id)
    result = await db.execute(stmt)
    integration = result.scalar_one_or_none()

    if not integration:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Google integration not found. Please connect first.",
        )

    if update.auto_join_enabled is not None:
        integration.auto_join_enabled = update.auto_join_enabled

    await db.commit()
    await db.refresh(integration)

    return GoogleIntegrationResponse.model_validate(integration)


@app.delete("/disconnect", tags=["Integration"])
async def disconnect_google_integration(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Disconnect Google integration.

    Removes the stored tokens and disables auto-join.
    """
    user = await get_current_user_from_api_key(request, db)

    stmt = select(GoogleIntegration).where(GoogleIntegration.user_id == user.id)
    result = await db.execute(stmt)
    integration = result.scalar_one_or_none()

    if not integration:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Google integration not found",
        )

    await db.delete(integration)
    await db.commit()

    return {"status": "disconnected", "message": "Google integration removed successfully"}


# --- Calendar Endpoints ---
@app.get("/calendar/events", response_model=CalendarEventsResponse, tags=["Calendar"])
async def get_calendar_events(
    request: Request,
    time_min: Optional[datetime] = None,
    time_max: Optional[datetime] = None,
    max_results: int = 50,
    page_token: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """
    Get upcoming calendar events with Google Meet links.

    Returns events from the user's primary calendar.

    - **time_min**: Start time for events (defaults to now)
    - **time_max**: End time for events (defaults to 7 days from now)
    - **max_results**: Maximum number of events to return (default: 50, max: 250)
    - **page_token**: Token for pagination
    """
    user = await get_current_user_from_api_key(request, db)

    stmt = select(GoogleIntegration).where(GoogleIntegration.user_id == user.id)
    result = await db.execute(stmt)
    integration = result.scalar_one_or_none()

    if not integration:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Google integration not found. Please connect first.",
        )

    access_token = await get_valid_access_token(integration, db)

    # Set default time range
    if not time_min:
        time_min = datetime.now(timezone.utc)
    if not time_max:
        time_max = time_min + timedelta(days=7)

    # Ensure timezone awareness
    if time_min.tzinfo is None:
        time_min = time_min.replace(tzinfo=timezone.utc)
    if time_max.tzinfo is None:
        time_max = time_max.replace(tzinfo=timezone.utc)

    params = {
        "timeMin": time_min.isoformat(),
        "timeMax": time_max.isoformat(),
        "maxResults": min(max_results, 250),
        "singleEvents": "true",
        "orderBy": "startTime",
    }
    if page_token:
        params["pageToken"] = page_token

    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{GOOGLE_CALENDAR_API}/calendars/primary/events",
            headers={"Authorization": f"Bearer {access_token}"},
            params=params,
        )

        if response.status_code == 401:
            # Token might be expired, try to refresh
            access_token = await refresh_access_token(integration, db)
            response = await client.get(
                f"{GOOGLE_CALENDAR_API}/calendars/primary/events",
                headers={"Authorization": f"Bearer {access_token}"},
                params=params,
            )

        if response.status_code != 200:
            logger.error(f"Failed to fetch calendar events: {response.text}")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to fetch calendar events from Google",
            )

        data = response.json()

    events = []
    for item in data.get("items", []):
        # Skip cancelled events
        if item.get("status") == "cancelled":
            continue

        # Extract Google Meet link from various locations
        meet_link = None
        native_meeting_id = None

        # Check conferenceData for Google Meet
        conference_data = item.get("conferenceData", {})
        for entry_point in conference_data.get("entryPoints", []):
            if entry_point.get("entryPointType") == "video":
                uri = entry_point.get("uri", "")
                if "meet.google.com" in uri:
                    meet_link = uri
                    native_meeting_id = extract_meet_code(uri)
                    break

        # Also check location and description for Meet links
        if not meet_link:
            meet_link_from_location = extract_meet_code(item.get("location", ""))
            meet_link_from_desc = extract_meet_code(item.get("description", ""))
            if meet_link_from_location:
                native_meeting_id = meet_link_from_location
                meet_link = f"https://meet.google.com/{meet_link_from_location}"
            elif meet_link_from_desc:
                native_meeting_id = meet_link_from_desc
                meet_link = f"https://meet.google.com/{meet_link_from_desc}"

        # Parse start/end times
        start = item.get("start", {})
        end = item.get("end", {})

        start_time = start.get("dateTime") or start.get("date")
        end_time = end.get("dateTime") or end.get("date")

        if not start_time:
            continue

        # Parse datetime strings
        try:
            if "T" in start_time:
                start_dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
            else:
                start_dt = datetime.strptime(start_time, "%Y-%m-%d").replace(tzinfo=timezone.utc)

            if end_time:
                if "T" in end_time:
                    end_dt = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
                else:
                    end_dt = datetime.strptime(end_time, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            else:
                end_dt = start_dt + timedelta(hours=1)
        except ValueError as e:
            logger.warning(f"Failed to parse event time: {e}")
            continue

        # Parse attendees
        attendees = []
        for attendee in item.get("attendees", []):
            attendees.append(
                CalendarEventAttendee(
                    email=attendee.get("email", ""),
                    display_name=attendee.get("displayName"),
                    response_status=attendee.get("responseStatus"),
                    is_organizer=attendee.get("organizer", False),
                    is_self=attendee.get("self", False),
                )
            )

        events.append(
            CalendarEvent(
                id=item["id"],
                summary=item.get("summary"),
                description=item.get("description"),
                start_time=start_dt,
                end_time=end_dt,
                google_meet_link=meet_link,
                native_meeting_id=native_meeting_id,
                location=item.get("location"),
                attendees=attendees,
                organizer_email=item.get("organizer", {}).get("email"),
                status=item.get("status", "confirmed"),
                html_link=item.get("htmlLink"),
            )
        )

    return CalendarEventsResponse(
        events=events,
        next_page_token=data.get("nextPageToken"),
        total_count=len(events),
    )


@app.get("/calendar/upcoming-meets", response_model=CalendarEventsResponse, tags=["Calendar"])
async def get_upcoming_google_meets(
    request: Request,
    hours: int = 24,
    db: AsyncSession = Depends(get_db),
):
    """
    Get upcoming Google Meet meetings only.

    Filters calendar events to show only those with Google Meet links
    in the next specified hours.

    - **hours**: Number of hours to look ahead (default: 24)
    """
    user = await get_current_user_from_api_key(request, db)

    stmt = select(GoogleIntegration).where(GoogleIntegration.user_id == user.id)
    result = await db.execute(stmt)
    integration = result.scalar_one_or_none()

    if not integration:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Google integration not found. Please connect first.",
        )

    # Get all events and filter for those with Meet links
    all_events_response = await get_calendar_events(
        request=request,
        time_min=datetime.now(timezone.utc),
        time_max=datetime.now(timezone.utc) + timedelta(hours=hours),
        max_results=100,
        db=db,
    )

    # Filter to only events with Google Meet links
    meet_events = [e for e in all_events_response.events if e.google_meet_link]

    return CalendarEventsResponse(
        events=meet_events,
        next_page_token=None,
        total_count=len(meet_events),
    )


# --- Main Execution ---
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
