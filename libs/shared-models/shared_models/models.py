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

# Define the base class for declarative models
Base = declarative_base()


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
    __tablename__ = "meetings"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    platform = Column(String(100), nullable=False)  # e.g., 'google_meet', 'zoom'
    # Database column name is platform_specific_id but we use native_meeting_id in the code
    platform_specific_id = Column(String(255), index=True, nullable=True)
    status = Column(
        String(50), nullable=False, default="requested", index=True
    )  # Values: requested, joining, awaiting_admission, active, completed, failed
    bot_container_id = Column(String(255), nullable=True)
    start_time = Column(DateTime, nullable=True)
    end_time = Column(DateTime, nullable=True)
    data = Column(JSONB, nullable=False, default=text("'{}'::jsonb"))
    created_at = Column(DateTime, server_default=func.now(), index=True)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    user = relationship("User", back_populates="meetings")
    transcriptions = relationship("Transcription", back_populates="meeting")
    sessions = relationship("MeetingSession", back_populates="meeting", cascade="all, delete-orphan")

    # Add composite index for efficient lookup by user, platform, and native ID, including created_at for sorting
    __table_args__ = (
        Index(
            "ix_meeting_user_platform_native_id_created_at",
            "user_id",
            "platform",
            "platform_specific_id",
            "created_at",  # Include created_at because the query orders by it
        ),
        Index("ix_meeting_data_gin", "data", postgresql_using="gin"),
        # Optional: Unique constraint (uncomment if needed, ensure native_meeting_id cannot be NULL if unique)
        # UniqueConstraint('user_id', 'platform', 'platform_specific_id', name='_user_platform_native_id_uc'),
    )

    # Add property getters/setters for compatibility
    @property
    def native_meeting_id(self):
        return self.platform_specific_id

    @native_meeting_id.setter
    def native_meeting_id(self, value):
        self.platform_specific_id = value

    @property
    def constructed_meeting_url(self) -> Optional[str]:  # Added return type hint
        # Calculate the URL on demand using the static method from schemas.py
        if self.platform and self.platform_specific_id:
            return Platform.construct_meeting_url(self.platform, self.platform_specific_id)
        return None


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
