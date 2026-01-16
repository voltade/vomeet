"""
Google Calendar Integration Microservice for Vomeet.

Enables account users to connect their Google Calendar and auto-join Google Meet meetings.

B2B Account API: X-API-Key header with account api_key + external_user_id

Scopes used:
- https://www.googleapis.com/auth/calendar.events.readonly - Read calendar events
- https://www.googleapis.com/auth/userinfo.email - Get user's email identity
"""

import os
import re
import json
import hmac
import hashlib
import secrets
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional
from urllib.parse import urlencode
import base64

import httpx
import uvicorn
from fastapi import FastAPI, Depends, HTTPException, status, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared_models.database import get_db
from shared_models.models import (
    Account,
    AccountUser,
    AccountUserGoogleIntegration,
    ScheduledMeeting,
    ScheduledMeetingStatus,
)
from shared_models.schemas import (
    CalendarEvent,
    CalendarEventAttendee,
    CalendarEventsResponse,
    AccountCalendarAuthTokenRequest,
    AccountCalendarAuthTokenResponse,
    AccountUserGoogleIntegrationResponse,
    GoogleIntegrationUpdate,
)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Vomeet Google Integration",
    description="Google Calendar integration for auto-joining Google Meet meetings (B2B Account API)",
    version="2.0.0",
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Auto-join scheduler configuration
ENABLE_AUTO_JOIN_SCHEDULER = os.getenv("ENABLE_AUTO_JOIN_SCHEDULER", "false").lower() == "true"


@app.on_event("startup")
async def startup_event():
    """Initialize the auto-join scheduler on startup if enabled."""
    if ENABLE_AUTO_JOIN_SCHEDULER:
        try:
            from scheduler import setup_scheduler

            setup_scheduler()
            logger.info("Auto-join scheduler initialized")
        except Exception as e:
            logger.error(f"Failed to initialize auto-join scheduler: {e}")
    else:
        logger.info("Auto-join scheduler is disabled (set ENABLE_AUTO_JOIN_SCHEDULER=true to enable)")


# State signing key - should be set in production
STATE_SECRET_KEY = os.getenv("STATE_SECRET_KEY", secrets.token_hex(32))

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
GOOGLE_CALENDAR_API = "https://www.googleapis.com/calendar/v3"

# OAuth scopes - matching Google's required format
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/calendar.events.readonly",
    "https://www.googleapis.com/auth/userinfo.email",
]

# Push notification configuration
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL", "https://vomeet.io")  # Your public-facing URL
CHANNEL_EXPIRATION_DAYS = 6  # Google allows max 7 days, renew at 6 to be safe

# State token expiry (10 minutes)
STATE_EXPIRY_SECONDS = 600


def create_calendar_auth_token(account_user_id: int) -> str:
    """
    Create a signed calendar auth token for an account user.
    This token is included in the OAuth state and used to identify the user on callback.
    Format: base64(account_user_id:timestamp:signature)
    """
    timestamp = int(datetime.now(timezone.utc).timestamp())
    payload = f"au:{account_user_id}:{timestamp}"  # "au:" prefix for account user
    signature = hmac.new(STATE_SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()[:16]
    token_data = f"{payload}:{signature}"
    return base64.urlsafe_b64encode(token_data.encode()).decode()


def verify_calendar_auth_token(token: str) -> Optional[int]:
    """
    Verify calendar auth token and return account_user_id if valid.
    Returns None if invalid or expired.
    """
    try:
        token_data = base64.urlsafe_b64decode(token.encode()).decode()
        parts = token_data.split(":")
        if len(parts) != 4 or parts[0] != "au":
            return None

        _, account_user_id_str, timestamp_str, signature = parts
        account_user_id = int(account_user_id_str)
        timestamp = int(timestamp_str)

        # Verify signature
        payload = f"au:{account_user_id}:{timestamp}"
        expected_signature = hmac.new(STATE_SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()[:16]

        if not hmac.compare_digest(signature, expected_signature):
            logger.warning("Calendar auth token signature mismatch")
            return None

        # Check expiry
        now = int(datetime.now(timezone.utc).timestamp())
        if now - timestamp > STATE_EXPIRY_SECONDS:
            logger.warning("Calendar auth token expired")
            return None

        return account_user_id
    except Exception as e:
        logger.warning(f"Failed to verify calendar auth token: {e}")
        return None


def extract_meet_code(text: Optional[str]) -> Optional[str]:
    """Extract Google Meet code (xxx-yyyy-zzz) from text."""
    if not text:
        return None
    pattern = r"meet\.google\.com/([a-z]{3}-[a-z]{4}-[a-z]{3})"
    match = re.search(pattern, text.lower())
    if match:
        return match.group(1)
    return None


def extract_teams_link(text: Optional[str]) -> Optional[str]:
    """Extract Microsoft Teams meeting link from text."""
    if not text:
        return None
    # Teams links can be in various formats:
    # - https://teams.microsoft.com/l/meetup-join/...
    # - https://teams.live.com/meet/...
    patterns = [
        r'(https://teams\.microsoft\.com/l/meetup-join/[^\s<>"]+)',
        r'(https://teams\.live\.com/meet/[^\s<>"]+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return None


async def get_account_from_api_key(request: Request, db: AsyncSession) -> Account:
    """Get account from X-API-Key header."""
    api_key = request.headers.get("X-API-Key")
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-API-Key header required",
        )

    stmt = select(Account).where(Account.api_key == api_key, Account.enabled.is_(True))
    result = await db.execute(stmt)
    account = result.scalar_one_or_none()

    if not account:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )

    return account


async def get_or_create_account_user(account: Account, external_user_id: str, db: AsyncSession) -> AccountUser:
    """Get or create an account user by external_user_id."""
    stmt = select(AccountUser).where(
        AccountUser.account_id == account.id,
        AccountUser.external_user_id == external_user_id,
    )
    result = await db.execute(stmt)
    account_user = result.scalar_one_or_none()

    if not account_user:
        account_user = AccountUser(
            account_id=account.id,
            external_user_id=external_user_id,
        )
        db.add(account_user)
        await db.commit()
        await db.refresh(account_user)
        logger.info(f"Created account user {account_user.id} for account {account.id}")

    return account_user


def get_google_credentials(account: Account) -> tuple[str, str]:
    """Get Google OAuth credentials from account. Account must have their own credentials configured."""
    if not account.google_client_id or not account.google_client_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google OAuth not configured for this account",
        )

    return account.google_client_id, account.google_client_secret


async def refresh_account_user_token(
    integration: AccountUserGoogleIntegration,
    account: Account,
    db: AsyncSession,
) -> str:
    """Refresh the access token using the refresh token."""
    if not integration.refresh_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No refresh token available. Please reconnect Google account.",
        )

    client_id, client_secret = get_google_credentials(account)

    async with httpx.AsyncClient() as client:
        response = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
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
            # Strip timezone for naive TIMESTAMP column (stored as UTC)
            integration.token_expires_at = (
                datetime.now(timezone.utc) + timedelta(seconds=token_data["expires_in"])
            ).replace(tzinfo=None)
        await db.commit()

        return integration.access_token


async def get_valid_account_user_token(
    integration: AccountUserGoogleIntegration,
    account: Account,
    db: AsyncSession,
) -> str:
    """Get a valid access token, refreshing if necessary."""
    if integration.token_expires_at:
        expires_at = integration.token_expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= datetime.now(timezone.utc) + timedelta(minutes=5):
            return await refresh_account_user_token(integration, account, db)
    return integration.access_token


# --- Health Check ---
@app.get("/healthz", tags=["Health"])
async def healthz():
    """Health check endpoint."""
    return {"status": "ok"}


# --- Calendar Auth Token (B2B) ---
@app.post("/calendar/auth_token", response_model=AccountCalendarAuthTokenResponse, tags=["Calendar OAuth"])
async def get_calendar_auth_token(
    body: AccountCalendarAuthTokenRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    **Step 1: Get Calendar Auth Token**

    Get a calendar auth token for one of your users. This token is used in the
    OAuth state parameter when building the Google OAuth URL.

    **Request Body:**
    ```json
    {
        "external_user_id": "your-app-user-id-123"
    }
    ```

    **Response:**
    ```json
    {
        "calendar_auth_token": "...",
        "account_user_id": 456,
        "expires_in": 600
    }
    ```
    """
    account = await get_account_from_api_key(request, db)
    account_user = await get_or_create_account_user(account, body.external_user_id, db)
    token = create_calendar_auth_token(account_user.id)

    return AccountCalendarAuthTokenResponse(
        calendar_auth_token=token,
        account_user_id=account_user.id,
        expires_in=STATE_EXPIRY_SECONDS,
    )


# --- Calendar OAuth Callback ---
@app.get("/calendar/google_oauth_callback", tags=["Calendar OAuth"])
async def google_calendar_oauth_callback(
    request: Request,
    code: Optional[str] = Query(None, description="Authorization code from Google"),
    state: Optional[str] = Query(None, description="JSON state containing auth token and redirect URLs"),
    error: Optional[str] = Query(None, description="Error from Google OAuth"),
    error_description: Optional[str] = Query(None, description="Error description"),
    db: AsyncSession = Depends(get_db),
):
    """
    **Step 3: Handle Google OAuth Callback**

    This endpoint receives the forwarded OAuth callback from your application.

    **Flow:**
    1. Google redirects to YOUR redirect_uri with code and state
    2. Your server forwards the request to this endpoint (preserving all query params)
    3. Vomeet exchanges the code, stores tokens, and redirects to success_url or error_url

    **State Parameter Format (JSON):**
    ```json
    {
        "vomeet_calendar_auth_token": "token_from_step_1",
        "google_oauth_redirect_uri": "https://your-domain.com/oauth/callback",
        "success_url": "https://your-domain.com/calendar/success",
        "error_url": "https://your-domain.com/calendar/error"
    }
    ```

    **Success Redirect:**
    Redirects to `success_url` with query params:
    - `email`: Connected Google account email
    - `name`: User's name (if available)
    - `account_user_id`: Internal Vomeet account user ID

    **Error Redirect:**
    Redirects to `error_url` with query params:
    - `error`: Error code
    - `error_description`: Human-readable error message
    """

    def redirect_error(error_url: Optional[str], error_code: str, description: str):
        """Helper to redirect to error URL or return JSON error."""
        if error_url:
            params = urlencode({"error": error_code, "error_description": description})
            return RedirectResponse(url=f"{error_url}?{params}")
        raise HTTPException(status_code=400, detail=description)

    # Parse state
    state_data = {}
    error_url = None
    success_url = None
    redirect_uri = None

    if state:
        try:
            state_data = json.loads(state)
            error_url = state_data.get("error_url")
            success_url = state_data.get("success_url")
            redirect_uri = state_data.get("google_oauth_redirect_uri")
        except json.JSONDecodeError:
            logger.warning(f"Failed to parse state JSON: {state}")
            raise HTTPException(status_code=400, detail="Invalid state parameter format")

    # Handle Google OAuth errors
    if error:
        error_msg = error_description or error
        if error == "access_denied":
            error_msg = "Access denied. Please grant calendar permissions to continue."
        logger.warning(f"Google OAuth error: {error} - {error_description}")
        return redirect_error(error_url, error, error_msg)

    if not code:
        return redirect_error(error_url, "missing_code", "No authorization code received")

    if not state:
        return redirect_error(error_url, "missing_state", "Missing state parameter")

    # Verify auth token and get account_user_id
    auth_token = state_data.get("vomeet_calendar_auth_token")
    if not auth_token:
        return redirect_error(error_url, "missing_token", "Missing vomeet_calendar_auth_token in state")

    account_user_id = verify_calendar_auth_token(auth_token)
    if not account_user_id:
        return redirect_error(
            error_url, "invalid_token", "Invalid or expired auth token. Please restart the connection flow."
        )

    # Get account user and account
    account_user = await db.get(AccountUser, account_user_id)
    if not account_user:
        return redirect_error(error_url, "user_not_found", "Account user not found")

    account = await db.get(Account, account_user.account_id)
    if not account or not account.enabled:
        return redirect_error(error_url, "account_disabled", "Account not found or disabled")

    if not redirect_uri:
        return redirect_error(error_url, "missing_redirect_uri", "Missing google_oauth_redirect_uri in state")

    # Get Google credentials (account's own or Vomeet's default)
    try:
        client_id, client_secret = get_google_credentials(account)
    except HTTPException as e:
        return redirect_error(error_url, "not_configured", e.detail)

    # Exchange code for tokens
    async with httpx.AsyncClient() as client:
        token_response = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
        )

        if token_response.status_code != 200:
            logger.error(f"Token exchange failed: {token_response.text}")
            return redirect_error(error_url, "token_exchange_failed", "Failed to exchange authorization code")

        token_data = token_response.json()

        # Get user info from Google
        userinfo_response = await client.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {token_data['access_token']}"},
        )

        if userinfo_response.status_code != 200:
            logger.error(f"Failed to get userinfo: {userinfo_response.text}")
            return redirect_error(error_url, "userinfo_failed", "Failed to get Google user info")

        userinfo = userinfo_response.json()

    # Check if integration already exists
    stmt = select(AccountUserGoogleIntegration).where(AccountUserGoogleIntegration.account_user_id == account_user.id)
    result = await db.execute(stmt)
    integration = result.scalar_one_or_none()

    token_expires_at = None
    if "expires_in" in token_data:
        # Strip timezone for naive TIMESTAMP column (stored as UTC)
        token_expires_at = (datetime.now(timezone.utc) + timedelta(seconds=token_data["expires_in"])).replace(
            tzinfo=None
        )

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
        integration = AccountUserGoogleIntegration(
            account_user_id=account_user.id,
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

    # Update account user with email/name if not set
    if not account_user.email and userinfo.get("email"):
        account_user.email = userinfo.get("email")
    if not account_user.name and userinfo.get("name"):
        account_user.name = userinfo.get("name")

    await db.commit()
    await db.refresh(integration)

    logger.info(f"Google Calendar connected for account_user {account_user.id}: {integration.email}")

    # Create push notification channel if auto-join is enabled
    if integration.auto_join_enabled:
        await create_push_notification_channel(integration, account, db)

    # Redirect to success URL
    if success_url:
        params = urlencode(
            {
                "email": integration.email,
                "name": integration.name or "",
                "account_user_id": str(account_user.id),
            }
        )
        return RedirectResponse(url=f"{success_url}?{params}")

    # If no success_url, return JSON response
    return {
        "success": True,
        "email": integration.email,
        "name": integration.name,
        "account_user_id": account_user.id,
    }


# --- Integration Status Endpoints ---
@app.get(
    "/users/{external_user_id}/status",
    response_model=Optional[AccountUserGoogleIntegrationResponse],
    tags=["Integration"],
)
async def get_user_google_integration_status(
    external_user_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Get a user's Google integration status.

    Returns the integration details or null if not connected.
    """
    account = await get_account_from_api_key(request, db)

    stmt = select(AccountUser).where(
        AccountUser.account_id == account.id,
        AccountUser.external_user_id == external_user_id,
    )
    result = await db.execute(stmt)
    account_user = result.scalar_one_or_none()

    if not account_user:
        return None

    stmt = select(AccountUserGoogleIntegration).where(AccountUserGoogleIntegration.account_user_id == account_user.id)
    result = await db.execute(stmt)
    integration = result.scalar_one_or_none()

    if not integration:
        return None

    return AccountUserGoogleIntegrationResponse.model_validate(integration)


@app.get("/users/{external_user_id}/settings", response_model=GoogleIntegrationUpdate, tags=["Integration"])
async def get_user_google_integration_settings(
    external_user_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Get a user's Google integration settings.
    """
    account = await get_account_from_api_key(request, db)

    stmt = select(AccountUser).where(
        AccountUser.account_id == account.id,
        AccountUser.external_user_id == external_user_id,
    )
    result = await db.execute(stmt)
    account_user = result.scalar_one_or_none()

    if not account_user:
        raise HTTPException(status_code=404, detail="User not found")

    stmt = select(AccountUserGoogleIntegration).where(AccountUserGoogleIntegration.account_user_id == account_user.id)
    result = await db.execute(stmt)
    integration = result.scalar_one_or_none()

    if not integration:
        raise HTTPException(status_code=404, detail="Google integration not found. Please connect first.")

    return GoogleIntegrationUpdate(
        auto_join_enabled=integration.auto_join_enabled,
        bot_name=integration.bot_name,
        auto_join_mode=integration.auto_join_mode,
    )


@app.put(
    "/users/{external_user_id}/settings", response_model=AccountUserGoogleIntegrationResponse, tags=["Integration"]
)
async def update_user_google_integration_settings(
    external_user_id: str,
    update: GoogleIntegrationUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Update a user's Google integration settings.
    """
    account = await get_account_from_api_key(request, db)

    stmt = select(AccountUser).where(
        AccountUser.account_id == account.id,
        AccountUser.external_user_id == external_user_id,
    )
    result = await db.execute(stmt)
    account_user = result.scalar_one_or_none()

    if not account_user:
        raise HTTPException(status_code=404, detail="User not found")

    stmt = select(AccountUserGoogleIntegration).where(AccountUserGoogleIntegration.account_user_id == account_user.id)
    result = await db.execute(stmt)
    integration = result.scalar_one_or_none()

    if not integration:
        raise HTTPException(status_code=404, detail="Google integration not found. Please connect first.")

    # Track if auto_join_enabled changed
    old_auto_join_enabled = integration.auto_join_enabled

    if update.auto_join_enabled is not None:
        integration.auto_join_enabled = update.auto_join_enabled
    if update.bot_name is not None:
        integration.bot_name = update.bot_name
    if update.auto_join_mode is not None:
        if update.auto_join_mode not in ("all_events", "my_events_only"):
            raise HTTPException(status_code=400, detail="auto_join_mode must be 'all_events' or 'my_events_only'")
        integration.auto_join_mode = update.auto_join_mode

    await db.commit()
    await db.refresh(integration)

    # Manage push notification channel based on auto_join_enabled
    if update.auto_join_enabled is not None and update.auto_join_enabled != old_auto_join_enabled:
        if integration.auto_join_enabled:
            # Enable: create push notification channel
            logger.info(f"Auto-join enabled for account_user {account_user.id}, creating push notification channel")
            await create_push_notification_channel(integration, account, db)
        else:
            # Disable: stop push notification channel
            logger.info(f"Auto-join disabled for account_user {account_user.id}, stopping push notification channel")
            await stop_push_notification_channel(integration, integration.access_token)
            integration.channel_id = None
            integration.channel_token = None
            integration.resource_id = None
            integration.channel_expires_at = None
            await db.commit()

    return AccountUserGoogleIntegrationResponse.model_validate(integration)


@app.delete("/users/{external_user_id}/disconnect", tags=["Integration"])
async def disconnect_user_google_integration(
    external_user_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Disconnect a user's Google integration.
    """
    account = await get_account_from_api_key(request, db)

    stmt = select(AccountUser).where(
        AccountUser.account_id == account.id,
        AccountUser.external_user_id == external_user_id,
    )
    result = await db.execute(stmt)
    account_user = result.scalar_one_or_none()

    if not account_user:
        raise HTTPException(status_code=404, detail="User not found")

    stmt = select(AccountUserGoogleIntegration).where(AccountUserGoogleIntegration.account_user_id == account_user.id)
    result = await db.execute(stmt)
    integration = result.scalar_one_or_none()

    if not integration:
        raise HTTPException(status_code=404, detail="Google integration not found")

    # Stop push notification channel before deleting
    if integration.channel_id and integration.resource_id:
        await stop_push_notification_channel(integration, integration.access_token)

    await db.delete(integration)
    await db.commit()

    return {"status": "disconnected", "message": "Google integration removed successfully"}


# --- Calendar Endpoints ---
@app.get("/users/{external_user_id}/calendar/events", response_model=CalendarEventsResponse, tags=["Calendar"])
async def get_user_calendar_events(
    external_user_id: str,
    request: Request,
    time_min: Optional[datetime] = None,
    time_max: Optional[datetime] = None,
    max_results: int = 50,
    page_token: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """
    Get upcoming calendar events for a user.

    - **time_min**: Start time for events (defaults to now)
    - **time_max**: End time for events (defaults to 7 days from now)
    - **max_results**: Maximum number of events to return (default: 50, max: 250)
    - **page_token**: Token for pagination
    """
    account = await get_account_from_api_key(request, db)

    stmt = select(AccountUser).where(
        AccountUser.account_id == account.id,
        AccountUser.external_user_id == external_user_id,
    )
    result = await db.execute(stmt)
    account_user = result.scalar_one_or_none()

    if not account_user:
        raise HTTPException(status_code=404, detail="User not found")

    stmt = select(AccountUserGoogleIntegration).where(AccountUserGoogleIntegration.account_user_id == account_user.id)
    result = await db.execute(stmt)
    integration = result.scalar_one_or_none()

    if not integration:
        raise HTTPException(status_code=404, detail="Google integration not found. Please connect first.")

    access_token = await get_valid_account_user_token(integration, account, db)

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
            access_token = await refresh_account_user_token(integration, account, db)
            response = await client.get(
                f"{GOOGLE_CALENDAR_API}/calendars/primary/events",
                headers={"Authorization": f"Bearer {access_token}"},
                params=params,
            )

        if response.status_code != 200:
            logger.error(f"Failed to fetch calendar events: {response.text}")
            raise HTTPException(status_code=502, detail="Failed to fetch calendar events from Google")

        data = response.json()

    events = []
    for item in data.get("items", []):
        if item.get("status") == "cancelled":
            continue

        meet_link = None
        teams_link = None
        native_meeting_id = None
        meeting_platform = None

        conference_data = item.get("conferenceData", {})
        for entry_point in conference_data.get("entryPoints", []):
            if entry_point.get("entryPointType") == "video":
                uri = entry_point.get("uri", "")
                if "meet.google.com" in uri:
                    meet_link = uri
                    native_meeting_id = extract_meet_code(uri)
                    meeting_platform = "google_meet"
                    break
                elif "teams.microsoft.com" in uri or "teams.live.com" in uri:
                    teams_link = uri
                    meeting_platform = "teams"
                    break

        # Fallback: check location and description for Google Meet
        if not meet_link and not teams_link:
            meet_link_from_location = extract_meet_code(item.get("location", ""))
            meet_link_from_desc = extract_meet_code(item.get("description", ""))
            if meet_link_from_location:
                native_meeting_id = meet_link_from_location
                meet_link = f"https://meet.google.com/{meet_link_from_location}"
                meeting_platform = "google_meet"
            elif meet_link_from_desc:
                native_meeting_id = meet_link_from_desc
                meet_link = f"https://meet.google.com/{meet_link_from_desc}"
                meeting_platform = "google_meet"

        # Fallback: check location and description for MS Teams
        if not teams_link and not meet_link:
            teams_from_location = extract_teams_link(item.get("location", ""))
            teams_from_desc = extract_teams_link(item.get("description", ""))
            if teams_from_location:
                teams_link = teams_from_location
                meeting_platform = "teams"
            elif teams_from_desc:
                teams_link = teams_from_desc
                meeting_platform = "teams"

        start = item.get("start", {})
        end = item.get("end", {})
        start_time = start.get("dateTime") or start.get("date")
        end_time = end.get("dateTime") or end.get("date")

        if not start_time:
            continue

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

        # Detect if the current user created or organizes this event
        is_creator_self = item.get("creator", {}).get("self", False)
        is_organizer_self = item.get("organizer", {}).get("self", False)

        events.append(
            CalendarEvent(
                id=item["id"],
                summary=item.get("summary"),
                description=item.get("description"),
                start_time=start_dt,
                end_time=end_dt,
                google_meet_link=meet_link,
                teams_link=teams_link,
                native_meeting_id=native_meeting_id,
                meeting_platform=meeting_platform,
                location=item.get("location"),
                attendees=attendees,
                organizer_email=item.get("organizer", {}).get("email"),
                is_creator_self=is_creator_self,
                is_organizer_self=is_organizer_self,
                status=item.get("status", "confirmed"),
                html_link=item.get("htmlLink"),
            )
        )

    return CalendarEventsResponse(
        events=events,
        next_page_token=data.get("nextPageToken"),
        total_count=len(events),
    )


@app.get("/users/{external_user_id}/calendar/upcoming-meets", response_model=CalendarEventsResponse, tags=["Calendar"])
async def get_user_upcoming_google_meets(
    external_user_id: str,
    request: Request,
    hours: int = 24,
    db: AsyncSession = Depends(get_db),
):
    """
    Get upcoming Google Meet meetings for a user.

    - **hours**: Number of hours to look ahead (default: 24)
    """
    all_events = await get_user_calendar_events(
        external_user_id=external_user_id,
        request=request,
        time_min=datetime.now(timezone.utc),
        time_max=datetime.now(timezone.utc) + timedelta(hours=hours),
        max_results=100,
        db=db,
    )

    meet_events = [e for e in all_events.events if e.google_meet_link]

    return CalendarEventsResponse(
        events=meet_events,
        next_page_token=None,
        total_count=len(meet_events),
    )


@app.get(
    "/users/{external_user_id}/calendar/upcoming-meetings", response_model=CalendarEventsResponse, tags=["Calendar"]
)
async def get_user_upcoming_meetings(
    external_user_id: str,
    request: Request,
    hours: int = 24,
    platform: Optional[str] = Query(None, description="Filter by platform: google_meet, teams, or None for all"),
    db: AsyncSession = Depends(get_db),
):
    """
    Get upcoming meetings (Google Meet, MS Teams, etc.) for a user.

    - **hours**: Number of hours to look ahead (default: 24)
    - **platform**: Optional filter by meeting platform (google_meet, teams)
    """
    all_events = await get_user_calendar_events(
        external_user_id=external_user_id,
        request=request,
        time_min=datetime.now(timezone.utc),
        time_max=datetime.now(timezone.utc) + timedelta(hours=hours),
        max_results=100,
        db=db,
    )

    # Filter events that have any meeting link
    meeting_events = [e for e in all_events.events if e.google_meet_link or e.teams_link]

    # Optionally filter by platform
    if platform:
        meeting_events = [e for e in meeting_events if e.meeting_platform == platform]

    return CalendarEventsResponse(
        events=meeting_events,
        next_page_token=None,
        total_count=len(meeting_events),
    )


# --- Internal Callbacks from Bot Manager ---


# --- Push Notification Webhook ---


@app.post("/calendar/webhook", tags=["Push Notifications"])
async def calendar_push_notification(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Receive push notifications from Google Calendar API.

    Google sends notifications when calendar events change.
    We sync the calendar and send webhooks for changes.
    """
    # Extract Google's notification headers
    channel_id = request.headers.get("X-Goog-Channel-ID")
    channel_token = request.headers.get("X-Goog-Channel-Token")
    resource_state = request.headers.get("X-Goog-Resource-State")
    resource_id = request.headers.get("X-Goog-Resource-ID")
    message_number = request.headers.get("X-Goog-Message-Number")

    logger.info(
        f"Received push notification: channel_id={channel_id}, "
        f"state={resource_state}, resource_id={resource_id}, msg={message_number}"
    )

    # Handle sync message (sent when channel is first created)
    if resource_state == "sync":
        logger.info(f"Received sync message for channel {channel_id}")
        return {"status": "ok", "message": "sync acknowledged"}

    # Handle event change notification
    if resource_state in ["exists", "not_exists"]:
        # Find the integration associated with this channel
        stmt = select(AccountUserGoogleIntegration).where(AccountUserGoogleIntegration.channel_id == channel_id)
        result = await db.execute(stmt)
        integration = result.scalar_one_or_none()

        if not integration:
            logger.warning(f"No integration found for channel_id={channel_id}")
            return {"status": "ok", "message": "channel not found"}

        # Verify channel token to prevent spoofed notifications
        if integration.channel_token and integration.channel_token != channel_token:
            logger.warning(
                f"Channel token mismatch for channel {channel_id}: "
                f"expected {integration.channel_token}, got {channel_token}"
            )
            raise HTTPException(status_code=401, detail="Invalid channel token")

        logger.info(f"Calendar changed for account_user {integration.account_user_id}, syncing calendar")

        # Get user and account info
        stmt = (
            select(AccountUser, Account)
            .join(Account, AccountUser.account_id == Account.id)
            .where(AccountUser.id == integration.account_user_id)
        )
        result = await db.execute(stmt)
        user_account = result.one_or_none()

        if user_account:
            account_user, account = user_account

            # Sync calendar and send webhooks
            try:
                from calendar_sync import sync_calendar_for_user

                counts = await sync_calendar_for_user(integration, account_user, account, db)
                logger.info(f"Calendar sync completed for account_user {account_user.id}: {counts}")
            except Exception as e:
                logger.error(f"Failed to sync calendar for account_user {account_user.id}: {e}", exc_info=True)

    return {"status": "ok", "message": "notification received"}


async def create_push_notification_channel(
    integration: AccountUserGoogleIntegration,
    account: Account,
    db: AsyncSession,
) -> bool:
    """
    Create a push notification channel for a user's calendar.

    Returns True if successful, False otherwise.
    """
    try:
        # Generate unique channel ID and verification token
        channel_id = secrets.token_urlsafe(32)[:64]
        channel_token = secrets.token_urlsafe(48)[:256]  # Secure verification token

        # Calculate expiration (6 days from now)
        expiration = datetime.now(timezone.utc) + timedelta(days=CHANNEL_EXPIRATION_DAYS)
        expiration_ms = int(expiration.timestamp() * 1000)

        # Webhook URL that Google will call
        webhook_url = f"{WEBHOOK_BASE_URL}/google/calendar/webhook"

        # Create watch request with verification token
        watch_body = {
            "id": channel_id,
            "type": "web_hook",
            "address": webhook_url,
            "token": channel_token,  # Security: verify webhook authenticity
            "expiration": expiration_ms,
        }

        logger.info(f"Creating push notification channel for account_user {integration.account_user_id}")

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{GOOGLE_CALENDAR_API}/calendars/primary/events/watch",
                headers={"Authorization": f"Bearer {integration.access_token}"},
                json=watch_body,
                timeout=30.0,
            )

            if response.status_code != 200:
                logger.error(f"Failed to create watch channel: {response.status_code} - {response.text}")
                return False

            data = response.json()
            resource_id = data.get("resourceId")

            # Update integration with channel info
            integration.channel_id = channel_id
            integration.channel_token = channel_token
            integration.resource_id = resource_id
            integration.channel_expires_at = expiration
            await db.commit()

            logger.info(
                f"Successfully created push notification channel {channel_id} "
                f"for account_user {integration.account_user_id}, expires at {expiration}"
            )
            return True

    except Exception as e:
        logger.error(f"Error creating push notification channel: {e}")
        return False


async def stop_push_notification_channel(
    integration: AccountUserGoogleIntegration,
    access_token: str,
) -> bool:
    """
    Stop a push notification channel.

    Returns True if successful or channel doesn't exist, False on error.
    """
    if not integration.channel_id or not integration.resource_id:
        return True

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{GOOGLE_CALENDAR_API}/channels/stop",
                headers={"Authorization": f"Bearer {access_token}"},
                json={
                    "id": integration.channel_id,
                    "resourceId": integration.resource_id,
                },
                timeout=30.0,
            )

            # 200-299 is success, 404 means channel already expired/stopped
            if response.status_code < 300 or response.status_code == 404:
                logger.info(f"Stopped push notification channel {integration.channel_id}")
                return True
            else:
                logger.warning(
                    f"Failed to stop channel {integration.channel_id}: {response.status_code} - {response.text}"
                )
                return False

    except Exception as e:
        logger.error(f"Error stopping push notification channel: {e}")
        return False


async def renew_push_notification_channel(
    integration: AccountUserGoogleIntegration,
    account: Account,
    db: AsyncSession,
) -> bool:
    """
    Renew a push notification channel by stopping the old one and creating a new one.

    Returns True if successful, False otherwise.
    """
    logger.info(f"Renewing push notification channel for account_user {integration.account_user_id}")

    # Stop the old channel (if it exists)
    if integration.channel_id and integration.resource_id:
        await stop_push_notification_channel(integration, integration.access_token)

    # Create a new channel
    return await create_push_notification_channel(integration, account, db)


# --- Main Execution ---
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
