import sqlalchemy
from sqlalchemy import (
    Column,
    String,
    Text,
    Integer,
    DateTime,
    Float,
    ForeignKey,
    Index,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func, text
from sqlalchemy.orm import declarative_base, relationship
from datetime import datetime  # Needed for Transcription model default
from shared_models.schemas import Platform  # Import Platform for the static method
from typing import Optional  # Added for the return type hint in constructed_meeting_url
from enum import Enum

# Define the base class for declarative models
Base = declarative_base()


class ScheduledMeetingStatus(str, Enum):
    """Status of a scheduled meeting from calendar sync"""

    SCHEDULED = "scheduled"  # Meeting is scheduled, bot not yet spawned
    BOT_REQUESTED = "bot_requested"  # Bot spawn has been requested
    BOT_ACTIVE = "bot_active"  # Bot is active in the meeting
    COMPLETED = "completed"  # Meeting completed
    CANCELLED = "cancelled"  # Meeting was cancelled in calendar


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)  # Added index=True
    email = Column(String(255), unique=True, index=True, nullable=False)
    name = Column(String(100))
    image_url = Column(Text)
    created_at = Column(DateTime, server_default=func.now())
    max_concurrent_bots = Column(Integer, nullable=False, server_default="1", default=1)  # Added field
    data = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=lambda: {})

    meetings = relationship("Meeting", back_populates="user")
    api_tokens = relationship("APIToken", back_populates="user")


class APIToken(Base):
    __tablename__ = "api_tokens"
    id = Column(Integer, primary_key=True, index=True)  # Added index=True
    token = Column(String(255), unique=True, index=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    created_at = Column(DateTime, server_default=func.now())

    user = relationship("User", back_populates="api_tokens")


class Meeting(Base):
    """
    Bot execution record. Tracks the lifecycle of a bot in a meeting.

    Meeting details (platform, meeting_url, scheduled times) are stored in ScheduledMeeting.
    This table only stores bot runtime data: status, container_id, actual times, errors.

    For both calendar-synced and ad-hoc meetings, a ScheduledMeeting record is created first,
    then a Meeting record is created when the bot is spawned.
    """

    __tablename__ = "meetings"
    id = Column(Integer, primary_key=True, index=True)

    # Link to the scheduled meeting (source of truth for meeting details)
    scheduled_meeting_id = Column(
        Integer, ForeignKey("scheduled_meetings.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # Account-based: account_id is the primary owner (B2B model)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=True, index=True)
    # Legacy field - kept for data migration from old meetings
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)

    # Legacy fields - meeting details now live in ScheduledMeeting, kept for data migration
    platform = Column(String(100), nullable=True)
    platform_specific_id = Column(String(255), index=True, nullable=True)
    scheduled_start_time = Column(DateTime, nullable=True, index=True)
    scheduled_end_time = Column(DateTime, nullable=True, index=True)

    # Bot runtime data
    status = Column(
        String(50), nullable=False, default="requested", index=True
    )  # Values: requested, joining, awaiting_admission, active, completed, failed
    bot_container_id = Column(String(255), nullable=True)
    start_time = Column(DateTime, nullable=True)  # Actual bot join time
    end_time = Column(DateTime, nullable=True)  # Actual bot leave time
    data = Column(JSONB, nullable=False, default=text("'{}'::jsonb"))
    created_at = Column(DateTime, server_default=func.now(), index=True)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    # Relationships
    scheduled_meeting = relationship(
        "ScheduledMeeting", foreign_keys=[scheduled_meeting_id], back_populates="bot_meetings"
    )
    account = relationship("Account")
    user = relationship("User", back_populates="meetings")
    transcriptions = relationship("Transcription", back_populates="meeting")
    sessions = relationship("MeetingSession", back_populates="meeting", cascade="all, delete-orphan")

    # Composite indexes for efficient lookup
    __table_args__ = (
        # Index for looking up by scheduled_meeting
        Index("ix_meeting_scheduled_meeting_status", "scheduled_meeting_id", "status"),
        # Legacy index for account-based queries
        Index(
            "ix_meeting_account_platform_native_id_created_at",
            "account_id",
            "platform",
            "platform_specific_id",
            "created_at",
        ),
        # Legacy index for user-based queries
        Index(
            "ix_meeting_user_platform_native_id_created_at",
            "user_id",
            "platform",
            "platform_specific_id",
            "created_at",
        ),
        Index("ix_meeting_data_gin", "data", postgresql_using="gin"),
    )

    # Convenience properties that delegate to scheduled_meeting
    @property
    def native_meeting_id(self) -> Optional[str]:
        """Get native meeting ID from scheduled_meeting, fallback to local field for backward compat."""
        if self.scheduled_meeting:
            return self.scheduled_meeting.native_meeting_id
        return self.platform_specific_id

    @native_meeting_id.setter
    def native_meeting_id(self, value):
        # For backward compatibility during migration
        self.platform_specific_id = value

    @property
    def effective_platform(self) -> Optional[str]:
        """Get platform from scheduled_meeting, fallback to local field."""
        if self.scheduled_meeting:
            return self.scheduled_meeting.platform
        return self.platform

    @property
    def meeting_url(self) -> Optional[str]:
        """Get meeting URL from scheduled_meeting."""
        if self.scheduled_meeting:
            return self.scheduled_meeting.meeting_url
        return None

    @property
    def constructed_meeting_url(self) -> Optional[str]:
        """Calculate the URL on demand using platform and native_meeting_id."""
        platform = self.effective_platform
        native_id = self.native_meeting_id
        if platform and native_id:
            return Platform.construct_meeting_url(platform, native_id)
        return None

    @property
    def effective_scheduled_start_time(self) -> Optional[datetime]:
        """Get scheduled start time from scheduled_meeting, fallback to local field."""
        if self.scheduled_meeting:
            return self.scheduled_meeting.scheduled_start_time
        return self.scheduled_start_time

    @property
    def effective_scheduled_end_time(self) -> Optional[datetime]:
        """Get scheduled end time from scheduled_meeting, fallback to local field."""
        if self.scheduled_meeting:
            return self.scheduled_meeting.scheduled_end_time
        return self.scheduled_end_time


class Transcription(Base):
    __tablename__ = "transcriptions"
    id = Column(Integer, primary_key=True, index=True)
    meeting_id = Column(
        Integer, ForeignKey("meetings.id"), nullable=False, index=True
    )  # Changed nullable to False, should always link
    # Removed redundant platform, meeting_url, token, client_uid, server_id as they belong to the Meeting
    start_time = Column(Float, nullable=False)
    end_time = Column(Float, nullable=False)
    text = Column(Text, nullable=False)
    speaker = Column(String(255), nullable=True)  # Speaker identifier
    language = Column(String(10), nullable=True)  # e.g., 'en', 'es'
    created_at = Column(DateTime, default=datetime.utcnow)

    meeting = relationship("Meeting", back_populates="transcriptions")

    session_uid = Column(String, nullable=True, index=True)  # Link to the specific bot session

    # Index for efficient querying by meeting_id and start_time
    __table_args__ = (Index("ix_transcription_meeting_start", "meeting_id", "start_time"),)


# New table to store session start times
class MeetingSession(Base):
    __tablename__ = "meeting_sessions"
    id = Column(Integer, primary_key=True, index=True)
    meeting_id = Column(Integer, ForeignKey("meetings.id"), nullable=False, index=True)
    session_uid = Column(String, nullable=False, index=True)  # Stores the 'uid' (based on connectionId)
    # Store timezone-aware timestamp to avoid ambiguity
    session_start_time = Column(sqlalchemy.DateTime(timezone=True), nullable=False, server_default=func.now())

    meeting = relationship("Meeting", back_populates="sessions")  # Define relationship

    __table_args__ = (
        UniqueConstraint("meeting_id", "session_uid", name="_meeting_session_uc"),
    )  # Ensure unique session per meeting


class AudioChunk(Base):
    """
    Stores transcribed audio chunks from Cloudflare Whisper Proxy.
    Each chunk represents ~10 seconds of audio stored in R2.
    Using audio_key as unique identifier ensures idempotent webhook delivery.
    """

    __tablename__ = "audio_chunks"
    id = Column(Integer, primary_key=True, index=True)
    meeting_id = Column(Integer, ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False, index=True)
    session_uid = Column(String, nullable=True, index=True)
    audio_key = Column(String(512), nullable=False, unique=True)  # R2 key: {session_uid}/{timestamp}-{chunk_index}.raw
    chunk_index = Column(Integer, nullable=False)  # Sequence number within session
    chunk_timestamp = Column(sqlalchemy.BigInteger, nullable=False)  # Unix timestamp ms when chunk was created
    duration = Column(Float, nullable=True)  # Duration of audio in seconds
    full_text = Column(Text, nullable=True)  # Full transcription text for chunk
    segments = Column(JSONB, nullable=True)  # Array of {start, end, text, temperature, avg_logprob, ...}
    language = Column(String(10), nullable=True)  # e.g., 'en', 'es'
    language_probability = Column(Float, nullable=True)
    speaker = Column(String(255), nullable=True)  # Speaker name detected from meeting UI
    created_at = Column(DateTime, server_default=func.now())

    meeting = relationship("Meeting")

    __table_args__ = (Index("ix_audio_chunks_meeting_chunk", "meeting_id", "chunk_index"),)


class GoogleIntegration(Base):
    """
    Stores Google OAuth tokens for users to enable calendar integration.
    Allows auto-joining meetings from user's Google Calendar.
    """

    __tablename__ = "google_integrations"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    google_user_id = Column(String(255), nullable=False)  # Google's unique user ID
    email = Column(String(255), nullable=False)  # User's Google email
    name = Column(String(255), nullable=True)  # User's display name
    picture = Column(Text, nullable=True)  # Profile picture URL
    access_token = Column(Text, nullable=False)  # OAuth access token (encrypted in production)
    refresh_token = Column(Text, nullable=True)  # OAuth refresh token (encrypted in production)
    token_expires_at = Column(DateTime, nullable=True)  # When access token expires
    scopes = Column(JSONB, nullable=True)  # List of granted scopes
    auto_join_enabled = Column(sqlalchemy.Boolean, nullable=False, default=False)  # Auto-join meetings
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    user = relationship("User")


class Webhook(Base):
    """
    Stores webhook configurations for users to receive event notifications.
    Supports multiple webhooks per user with event filtering.
    """

    __tablename__ = "webhooks"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    url = Column(Text, nullable=False)  # Webhook endpoint URL
    secret = Column(String(64), nullable=True)  # HMAC secret for signature verification
    events = Column(JSONB, nullable=False, default=lambda: ["*"])  # List of events to subscribe to, or ["*"] for all
    enabled = Column(sqlalchemy.Boolean, nullable=False, default=True)
    description = Column(String(255), nullable=True)  # Optional description
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    user = relationship("User")


class Account(Base):
    """
    Represents an external application/company using Vomeet API.
    Each account can have multiple end-users (AccountUser).
    This is for B2B integrations where an app manages its own users.
    """

    __tablename__ = "accounts"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)  # Company/app name
    api_key = Column(String(64), unique=True, index=True, nullable=False)  # App-level API key
    api_secret = Column(String(64), nullable=True)  # Optional API secret for additional security
    # Optional: Allow account to use their own Google OAuth credentials
    google_client_id = Column(String(255), nullable=True)
    google_client_secret = Column(Text, nullable=True)
    webhook_url = Column(Text, nullable=True)  # Account-level webhook for all events
    webhook_secret = Column(String(64), nullable=True)  # HMAC secret for webhook signatures
    max_concurrent_bots = Column(Integer, nullable=False, default=5)  # Account-wide limit
    data = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=lambda: {})
    enabled = Column(sqlalchemy.Boolean, nullable=False, default=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    account_users = relationship("AccountUser", back_populates="account", cascade="all, delete-orphan")


class AccountUser(Base):
    """
    Represents an end-user within an Account (external app's user).
    Identified by external_user_id which is the app's own user identifier.
    """

    __tablename__ = "account_users"
    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    external_user_id = Column(String(255), nullable=False)  # The app's own user ID
    email = Column(String(255), nullable=True)  # Optional email
    name = Column(String(255), nullable=True)  # Optional display name
    data = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=lambda: {})
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    account = relationship("Account", back_populates="account_users")
    google_integration = relationship("AccountUserGoogleIntegration", back_populates="account_user", uselist=False)

    __table_args__ = (
        UniqueConstraint("account_id", "external_user_id", name="_account_external_user_uc"),
        Index("ix_account_user_account_external", "account_id", "external_user_id"),
    )


class AccountUserGoogleIntegration(Base):
    """
    Stores Google OAuth tokens for AccountUsers (external app's users).
    Separate from GoogleIntegration which is for direct Vomeet users.
    """

    __tablename__ = "account_user_google_integrations"
    id = Column(Integer, primary_key=True, index=True)
    account_user_id = Column(
        Integer, ForeignKey("account_users.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    google_user_id = Column(String(255), nullable=False)  # Google's unique user ID
    email = Column(String(255), nullable=False)  # User's Google email
    name = Column(String(255), nullable=True)  # User's display name
    picture = Column(Text, nullable=True)  # Profile picture URL
    access_token = Column(Text, nullable=False)  # OAuth access token
    refresh_token = Column(Text, nullable=True)  # OAuth refresh token
    token_expires_at = Column(DateTime, nullable=True)  # When access token expires
    scopes = Column(JSONB, nullable=True)  # List of granted scopes
    auto_join_enabled = Column(sqlalchemy.Boolean, nullable=False, default=False)
    bot_name = Column(String(100), nullable=True, default="Notetaker")  # Name shown in meeting
    auto_join_mode = Column(String(50), nullable=False, default="all_events")  # "all_events" or "my_events_only"

    # Push notification channel fields
    channel_id = Column(String(64), nullable=True)  # UUID identifying the push notification channel
    channel_token = Column(String(256), nullable=True)  # Verification token for webhook security
    resource_id = Column(String(255), nullable=True)  # Opaque ID from Google for the watched resource
    channel_expires_at = Column(DateTime, nullable=True)  # When the push notification channel expires
    sync_token = Column(Text, nullable=True)  # Token for incremental sync

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    account_user = relationship("AccountUser", back_populates="google_integration")
    scheduled_meetings = relationship("ScheduledMeeting", back_populates="integration", cascade="all, delete-orphan")


class ScheduledMeeting(Base):
    """
    Single source of truth for meeting requests - both calendar-synced and ad-hoc.

    For calendar meetings:
    - Created by calendar sync job
    - Has calendar_event_id, integration_id
    - Webhooks sent on create/update/cancel

    For ad-hoc meetings (via API):
    - Created when user calls POST /bots
    - calendar_event_id = NULL, integration_id = NULL
    - calendar_provider = 'api'

    Lifecycle:
    1. Record created (from calendar or API)
    2. Bot spawn job creates Meeting record, links it here
    3. Bot status updates reflected in this record's status
    """

    __tablename__ = "scheduled_meetings"
    id = Column(Integer, primary_key=True, index=True)

    # User/Account context
    account_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    account_user_id = Column(
        Integer, ForeignKey("account_users.id", ondelete="CASCADE"), nullable=True, index=True
    )  # NULL for ad-hoc
    integration_id = Column(
        Integer,
        ForeignKey("account_user_google_integrations.id", ondelete="CASCADE"),
        nullable=True,
        index=True,  # NULL for ad-hoc
    )

    # Calendar event identifiers (NULL for ad-hoc meetings)
    calendar_event_id = Column(String(255), nullable=True)  # Google Calendar event ID, NULL for ad-hoc
    calendar_provider = Column(String(50), nullable=False, default="api")  # 'google', 'outlook', 'api' (ad-hoc)

    # Meeting details (required for all meeting types)
    title = Column(String(500), nullable=True)
    description = Column(Text, nullable=True)
    platform = Column(String(100), nullable=False)  # 'google_meet', 'teams', 'zoom', etc.
    native_meeting_id = Column(String(255), nullable=False)  # Platform-specific meeting ID
    meeting_url = Column(Text, nullable=True)  # Full meeting URL

    # Schedule
    scheduled_start_time = Column(DateTime(timezone=True), nullable=True, index=True)  # NULL for immediate ad-hoc
    scheduled_end_time = Column(DateTime(timezone=True), nullable=True)

    # Organizer info (for calendar meetings)
    is_creator_self = Column(sqlalchemy.Boolean, nullable=False, default=False)
    is_organizer_self = Column(sqlalchemy.Boolean, nullable=False, default=False)

    # Status tracking
    status = Column(String(50), nullable=False, default=ScheduledMeetingStatus.SCHEDULED.value, index=True)

    # Store attendees and other metadata
    attendees = Column(JSONB, nullable=True)  # List of {email, name, response_status}
    data = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=lambda: {})

    # Timestamps
    last_synced_at = Column(DateTime(timezone=True), server_default=func.now())
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    account = relationship("Account")
    account_user = relationship("AccountUser")
    integration = relationship("AccountUserGoogleIntegration", back_populates="scheduled_meetings")
    # bot_meetings: all Meeting records created from this scheduled meeting
    bot_meetings = relationship(
        "Meeting", foreign_keys="Meeting.scheduled_meeting_id", back_populates="scheduled_meeting"
    )

    __table_args__ = (
        # Unique constraint: one calendar event per integration (only for calendar meetings)
        # Note: This allows multiple ad-hoc meetings with same platform/native_meeting_id
        UniqueConstraint("integration_id", "calendar_event_id", name="_scheduled_meeting_event_uc"),
        # Index for finding meetings to spawn bots for
        Index("ix_scheduled_meeting_spawn", "status", "scheduled_start_time"),
        # Index for account lookup
        Index("ix_scheduled_meeting_account", "account_id", "scheduled_start_time"),
        # Index for duplicate detection (ad-hoc meetings)
        Index("ix_scheduled_meeting_platform_native", "account_id", "platform", "native_meeting_id", "status"),
    )

    @property
    def is_ad_hoc(self) -> bool:
        """Returns True if this is an ad-hoc meeting (not from calendar)."""
        return self.calendar_event_id is None
