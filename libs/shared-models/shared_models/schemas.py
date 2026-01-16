from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field, EmailStr, field_validator, ValidationInfo, ConfigDict
from datetime import datetime
from enum import Enum
import re  # Import re for native ID validation
import logging  # Import logging for status validation warnings

# Setup logger for status validation warnings
logger = logging.getLogger(__name__)

# --- Language Codes from faster-whisper ---
# These are the accepted language codes from the faster-whisper library
# Source: faster_whisper.tokenizer._LANGUAGE_CODES
ACCEPTED_LANGUAGE_CODES = {
    "af",
    "am",
    "ar",
    "as",
    "az",
    "ba",
    "be",
    "bg",
    "bn",
    "bo",
    "br",
    "bs",
    "ca",
    "cs",
    "cy",
    "da",
    "de",
    "el",
    "en",
    "es",
    "et",
    "eu",
    "fa",
    "fi",
    "fo",
    "fr",
    "gl",
    "gu",
    "ha",
    "haw",
    "he",
    "hi",
    "hr",
    "ht",
    "hu",
    "hy",
    "id",
    "is",
    "it",
    "ja",
    "jw",
    "ka",
    "kk",
    "km",
    "kn",
    "ko",
    "la",
    "lb",
    "ln",
    "lo",
    "lt",
    "lv",
    "mg",
    "mi",
    "mk",
    "ml",
    "mn",
    "mr",
    "ms",
    "mt",
    "my",
    "ne",
    "nl",
    "nn",
    "no",
    "oc",
    "pa",
    "pl",
    "ps",
    "pt",
    "ro",
    "ru",
    "sa",
    "sd",
    "si",
    "sk",
    "sl",
    "sn",
    "so",
    "sq",
    "sr",
    "su",
    "sv",
    "sw",
    "ta",
    "te",
    "tg",
    "th",
    "tk",
    "tl",
    "tr",
    "tt",
    "uk",
    "ur",
    "uz",
    "vi",
    "yi",
    "yo",
    "zh",
    "yue",
}

# --- Allowed Tasks ---
# These are the tasks supported by WhisperLive
ALLOWED_TASKS = {"transcribe", "translate"}

# --- Meeting Status Definitions ---


class MeetingStatus(str, Enum):
    """
    Meeting status values with their sources and transitions.
    
    Status Flow:
    requested -> joining -> awaiting_admission -> active -> stopping -> completed
                                    |              |                 \
                                    v              v                  -> failed
                                 failed         failed
    
    Sources:
    - requested: POST bot API (user)
    - joining: bot callback
    - awaiting_admission: bot callback  
    - active: bot callback
    - stopping: user (stop bot API)
    - completed: user, bot callback
    - failed: bot callback, validation errors
    """

    REQUESTED = "requested"
    JOINING = "joining"
    AWAITING_ADMISSION = "awaiting_admission"
    ACTIVE = "active"
    STOPPING = "stopping"
    COMPLETED = "completed"
    FAILED = "failed"
    ERROR = "error"  # Legacy status, treat as FAILED


class MeetingCompletionReason(str, Enum):
    """
    Reasons for meeting completion.
    """

    STOPPED = "stopped"  # User stopped by API
    VALIDATION_ERROR = "validation_error"  # Post bot validation failed
    AWAITING_ADMISSION_TIMEOUT = "awaiting_admission_timeout"  # Timeout during awaiting admission
    AWAITING_ADMISSION_REJECTED = "awaiting_admission_rejected"  # Rejected during awaiting admission
    LEFT_ALONE = "left_alone"  # Timeout for being alone
    EVICTED = "evicted"  # Kicked out from meeting using meeting UI


class MeetingFailureStage(str, Enum):
    """
    Stages where meeting can fail.
    """

    REQUESTED = "requested"
    JOINING = "joining"
    AWAITING_ADMISSION = "awaiting_admission"
    ACTIVE = "active"


# --- Status Transition Helpers ---


def get_valid_status_transitions() -> Dict[MeetingStatus, List[MeetingStatus]]:
    """
    Returns valid status transitions for meetings.

    Returns:
        Dict mapping current status to list of valid next statuses
    """
    return {
        MeetingStatus.REQUESTED: [
            MeetingStatus.JOINING,
            MeetingStatus.FAILED,
            MeetingStatus.COMPLETED,
            MeetingStatus.STOPPING,
        ],
        MeetingStatus.JOINING: [
            MeetingStatus.AWAITING_ADMISSION,
            MeetingStatus.FAILED,
            MeetingStatus.COMPLETED,
            MeetingStatus.STOPPING,
        ],
        MeetingStatus.AWAITING_ADMISSION: [
            MeetingStatus.ACTIVE,
            MeetingStatus.FAILED,
            MeetingStatus.COMPLETED,
            MeetingStatus.STOPPING,
        ],
        MeetingStatus.ACTIVE: [
            MeetingStatus.STOPPING,
            MeetingStatus.COMPLETED,
            MeetingStatus.FAILED,
        ],
        MeetingStatus.STOPPING: [
            MeetingStatus.COMPLETED,
            MeetingStatus.FAILED,
        ],
        MeetingStatus.COMPLETED: [],  # Terminal state
        MeetingStatus.FAILED: [],  # Terminal state
        MeetingStatus.ERROR: [],  # Terminal state (legacy)
    }


def is_valid_status_transition(from_status: MeetingStatus, to_status: MeetingStatus) -> bool:
    """
    Check if a status transition is valid.

    Args:
        from_status: Current meeting status
        to_status: Desired new status

    Returns:
        True if transition is valid, False otherwise
    """
    valid_transitions = get_valid_status_transitions()
    return to_status in valid_transitions.get(from_status, [])


def get_status_source(from_status: MeetingStatus, to_status: MeetingStatus) -> str:
    """
    Get the source that should trigger this status transition.

    Args:
        from_status: Current meeting status
        to_status: Desired new status

    Returns:
        Source description ("user", "bot_callback", "validation_error")
    """
    # User-controlled transitions (via API)
    if to_status in (MeetingStatus.STOPPING, MeetingStatus.COMPLETED):
        return "user"  # Stop bot API initiated

    # Bot callback transitions
    bot_callback_transitions = [
        (MeetingStatus.REQUESTED, MeetingStatus.JOINING),
        (MeetingStatus.JOINING, MeetingStatus.AWAITING_ADMISSION),
        (MeetingStatus.AWAITING_ADMISSION, MeetingStatus.ACTIVE),
        (MeetingStatus.ACTIVE, MeetingStatus.COMPLETED),
        (MeetingStatus.STOPPING, MeetingStatus.COMPLETED),
        (MeetingStatus.REQUESTED, MeetingStatus.FAILED),
        (MeetingStatus.JOINING, MeetingStatus.FAILED),
        (MeetingStatus.AWAITING_ADMISSION, MeetingStatus.FAILED),
        (MeetingStatus.ACTIVE, MeetingStatus.FAILED),
        (MeetingStatus.STOPPING, MeetingStatus.FAILED),
    ]

    if (from_status, to_status) in bot_callback_transitions:
        return "bot_callback"

    # Validation error transitions
    if to_status == MeetingStatus.FAILED and from_status == MeetingStatus.REQUESTED:
        return "validation_error"

    return "unknown"


# --- Platform Definitions ---


class Platform(str, Enum):
    """
    Platform identifiers for meeting platforms.
    The value is the external API name, while the bot_name is what's used internally by the bot.
    """

    GOOGLE_MEET = "google_meet"
    ZOOM = "zoom"
    TEAMS = "teams"

    @property
    def bot_name(self) -> str:
        """
        Returns the platform name used by the bot containers.
        This maps external API platform names to internal bot platform names.
        """
        mapping = {
            Platform.GOOGLE_MEET: "google_meet",
            Platform.ZOOM: "zoom",
            Platform.TEAMS: "teams",
        }
        return mapping[self]

    @classmethod
    def get_bot_name(cls, platform_str: str) -> str:
        """
        Static method to get the bot platform name from a string.
        This is useful when you have a platform string but not a Platform instance.

        Args:
            platform_str: The platform identifier string (e.g., 'google_meet')

        Returns:
            The platform name used by the bot (e.g., 'google')
        """
        try:
            platform = Platform(platform_str)
            return platform.bot_name
        except ValueError:
            # If the platform string is invalid, return it unchanged or handle error
            return platform_str  # Or raise error/log warning

    @classmethod
    def get_api_value(cls, bot_platform_name: str) -> Optional[str]:
        """
        Gets the external API enum value from the internal bot platform name.
        Returns None if the bot name is unknown.
        """
        reverse_mapping = {
            "google_meet": Platform.GOOGLE_MEET.value,
            "zoom": Platform.ZOOM.value,
            "teams": Platform.TEAMS.value,
        }
        return reverse_mapping.get(bot_platform_name)

    @classmethod
    def construct_meeting_url(cls, platform_str: str, native_id: str, passcode: Optional[str] = None) -> Optional[str]:
        """
        Constructs the full meeting URL from platform, native ID, and optional passcode.
        Returns None if the platform is unknown or ID is invalid for the platform.
        """
        try:
            platform = Platform(platform_str)
            if platform == Platform.GOOGLE_MEET:
                # Basic validation for Google Meet code format (xxx-xxxx-xxx)
                if re.fullmatch(r"^[a-z]{3}-[a-z]{4}-[a-z]{3}$", native_id):
                    return f"https://meet.google.com/{native_id}"
                else:
                    return None  # Invalid ID format
            elif platform == Platform.TEAMS:
                # Teams meeting ID (numeric) and optional passcode
                # Only accept numeric meeting IDs, not full URLs
                if re.fullmatch(r"^\d{10,15}$", native_id):
                    url = f"https://teams.live.com/meet/{native_id}"
                    if passcode:
                        url += f"?p={passcode}"
                    return url
                else:
                    return None  # Invalid Teams ID format - must be numeric only
            else:
                return None  # Unknown platform
        except ValueError:
            return None  # Invalid platform string


# --- Schemas from Admin API ---


class UserBase(BaseModel):  # Base for common user fields
    email: EmailStr
    name: Optional[str] = None
    image_url: Optional[str] = None
    max_concurrent_bots: Optional[int] = Field(
        None, description="Maximum number of concurrent bots allowed for the user"
    )
    data: Optional[Dict[str, Any]] = Field(None, description="JSONB storage for arbitrary user data, like webhook URLs")


class UserCreate(UserBase):
    pass


class UserResponse(UserBase):
    id: int
    created_at: datetime
    max_concurrent_bots: int = Field(..., description="Maximum number of concurrent bots allowed for the user")

    model_config = ConfigDict(from_attributes=True)


class TokenBase(BaseModel):
    user_id: int


class TokenCreate(TokenBase):
    pass


class TokenResponse(TokenBase):
    id: int
    token: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class UserDetailResponse(UserResponse):
    api_tokens: List[TokenResponse] = []


# --- ADD UserUpdate Schema for PATCH ---
class UserUpdate(BaseModel):
    email: Optional[EmailStr] = None  # Make all fields optional for PATCH
    name: Optional[str] = None
    image_url: Optional[str] = None
    max_concurrent_bots: Optional[int] = Field(
        None, description="Maximum number of concurrent bots allowed for the user"
    )
    data: Optional[Dict[str, Any]] = Field(
        None,
        description="JSONB storage for arbitrary user data, like webhook URLs and subscription info",
    )


# --- END UserUpdate Schema ---


# --- Account Schemas (B2B API) ---


class AccountCreate(BaseModel):
    """Request to create a new account (external app)"""

    name: str = Field(..., min_length=1, max_length=255, description="Company/app name")
    google_client_id: Optional[str] = Field(None, description="Optional Google OAuth client ID")
    google_client_secret: Optional[str] = Field(None, description="Optional Google OAuth client secret")
    webhook_url: Optional[str] = Field(None, description="Webhook URL for event notifications")
    max_concurrent_bots: int = Field(default=5, ge=1, description="Maximum concurrent bots allowed")


class AccountUpdate(BaseModel):
    """Request to update an account"""

    name: Optional[str] = Field(None, min_length=1, max_length=255)
    google_client_id: Optional[str] = None
    google_client_secret: Optional[str] = None
    webhook_url: Optional[str] = None
    webhook_secret: Optional[str] = None
    max_concurrent_bots: Optional[int] = Field(None, ge=1)
    enabled: Optional[bool] = None


class AccountResponse(BaseModel):
    """Response containing account details"""

    id: int
    name: str
    api_key: str
    google_client_id: Optional[str] = None
    webhook_url: Optional[str] = None
    max_concurrent_bots: int
    enabled: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AccountUserCreate(BaseModel):
    """Request to create/get an account user"""

    external_user_id: str = Field(..., min_length=1, max_length=255, description="Your app's user ID")
    email: Optional[EmailStr] = Field(None, description="Optional user email")
    name: Optional[str] = Field(None, max_length=255, description="Optional user name")


class AccountUserResponse(BaseModel):
    """Response containing account user details"""

    id: int
    account_id: int
    external_user_id: str
    email: Optional[str] = None
    name: Optional[str] = None
    has_google_integration: bool = False
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AccountUserGoogleIntegrationResponse(BaseModel):
    """Response showing Google integration status for an account user"""

    id: int
    account_user_id: int
    google_user_id: str
    email: str
    name: Optional[str] = None
    picture: Optional[str] = None
    scopes: Optional[List[str]] = None
    auto_join_enabled: bool
    bot_name: Optional[str] = "Notetaker"
    auto_join_mode: str = "all_events"
    connected_at: datetime = Field(..., alias="created_at")
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class AccountCalendarAuthTokenRequest(BaseModel):
    """Request to get a calendar auth token for an account user"""

    external_user_id: str = Field(..., min_length=1, max_length=255, description="Your app's user ID")


class AccountCalendarAuthTokenResponse(BaseModel):
    """Response containing calendar auth token for OAuth flow"""

    calendar_auth_token: str = Field(
        ...,
        description="Token to include in OAuth state. Valid for 10 minutes.",
    )
    account_user_id: int = Field(..., description="Internal ID of the account user")
    expires_in: int = Field(default=600, description="Token validity in seconds")


# --- END Account Schemas ---


# --- Meeting Schemas ---


class MeetingBase(BaseModel):
    platform: Platform = Field(..., description="Platform identifier (e.g., 'google_meet', 'teams')")
    native_meeting_id: str = Field(
        ...,
        description="The native meeting identifier (e.g., 'abc-defg-hij' for Google Meet, '1234567890' for Teams)",
    )
    # meeting_url field removed

    @field_validator("platform", mode="before")
    @classmethod
    def validate_platform_str(cls, v):
        """Validate that the platform string is one of the supported platforms"""
        try:
            Platform(v)
            return v
        except ValueError:
            supported = ", ".join([p.value for p in Platform])
            raise ValueError(f"Invalid platform '{v}'. Must be one of: {supported}")

    # Removed get_bot_platform method, use Platform.get_bot_name(self.platform.value) if needed


class MeetingCreate(BaseModel):
    platform: Platform
    native_meeting_id: str = Field(
        ...,
        description="The platform-specific ID for the meeting (e.g., Google Meet code, Teams ID)",
    )
    bot_name: Optional[str] = Field(None, description="Optional name for the bot in the meeting")
    language: Optional[str] = Field(None, description="Optional language code for transcription (e.g., 'en', 'es')")
    task: Optional[str] = Field(
        None,
        description="Optional task for the transcription model (e.g., 'transcribe', 'translate')",
    )
    passcode: Optional[str] = Field(None, description="Optional passcode for the meeting (Teams only)")
    scheduled_start_time: Optional[datetime] = Field(
        None,
        description="Optional scheduled start time of the meeting in ISO 8601 format (UTC)",
    )
    scheduled_end_time: Optional[datetime] = Field(
        None,
        description="Optional scheduled end time of the meeting in ISO 8601 format (UTC)",
    )

    @field_validator("platform")
    @classmethod
    def platform_must_be_valid(cls, v):
        """Validate that the platform is one of the supported platforms"""
        try:
            Platform(v)
            return v
        except ValueError:
            supported = ", ".join([p.value for p in Platform])
            raise ValueError(f"Invalid platform '{v}'. Must be one of: {supported}")

    @field_validator("passcode")
    @classmethod
    def validate_passcode(cls, v, info: ValidationInfo):
        """Validate passcode usage based on platform"""
        if v is not None and v != "":
            platform = info.data.get("platform")
            if platform == Platform.GOOGLE_MEET:
                raise ValueError("Passcode is not supported for Google Meet meetings")
            elif platform == Platform.TEAMS:
                # Teams passcode validation (alphanumeric, reasonable length)
                if not re.match(r"^[A-Za-z0-9]{8,20}$", v):
                    raise ValueError("Teams passcode must be 8-20 alphanumeric characters")
        return v

    @field_validator("language")
    @classmethod
    def validate_language(cls, v):
        """Validate that the language code is one of the accepted language codes."""
        if v is not None and v != "" and v not in ACCEPTED_LANGUAGE_CODES:
            raise ValueError(f"Invalid language code '{v}'. Must be one of: {sorted(ACCEPTED_LANGUAGE_CODES)}")
        return v

    @field_validator("task")
    @classmethod
    def validate_task(cls, v):
        """Validate that the task is one of the allowed tasks."""
        if v is not None and v != "" and v not in ALLOWED_TASKS:
            raise ValueError(f"Invalid task '{v}'. Must be one of: {sorted(ALLOWED_TASKS)}")
        return v

    @field_validator("native_meeting_id")
    @classmethod
    def validate_native_meeting_id(cls, v, info: ValidationInfo):
        """Validate that the native meeting ID matches the expected format for the platform."""
        if not v or not v.strip():
            raise ValueError("native_meeting_id cannot be empty")

        platform = info.data.get("platform")
        if not platform:
            return v  # Let platform validator handle this case

        platform = Platform(platform)
        native_id = v.strip()

        if platform == Platform.GOOGLE_MEET:
            # Google Meet format: abc-defg-hij
            if not re.fullmatch(r"^[a-z]{3}-[a-z]{4}-[a-z]{3}$", native_id):
                raise ValueError("Google Meet ID must be in format 'abc-defg-hij' (lowercase letters only)")

        elif platform == Platform.TEAMS:
            # Teams format: numeric ID only (10-15 digits)
            if not re.fullmatch(r"^\d{10,15}$", native_id):
                raise ValueError("Teams meeting ID must be 10-15 digits only (not a full URL)")

            # Explicitly reject full URLs
            if native_id.startswith(("http://", "https://", "teams.microsoft.com", "teams.live.com")):
                raise ValueError("Teams meeting ID must be the numeric ID only (e.g., '9399697580372'), not a full URL")

        return v


class MeetingResponse(
    BaseModel
):  # Not inheriting from MeetingBase anymore to avoid duplicate fields if DB model is used directly
    id: int = Field(..., description="Internal database ID for the meeting")
    user_id: Optional[int] = Field(None, description="Deprecated user_id for backward compatibility")
    platform: Platform  # Use the enum type
    native_meeting_id: Optional[str] = Field(
        None, description="The native meeting identifier provided during creation"
    )  # Renamed from platform_specific_id for clarity
    constructed_meeting_url: Optional[str] = Field(
        None, description="The meeting URL constructed internally, if possible"
    )  # Added for info
    status: MeetingStatus = Field(..., description="Current meeting status")
    bot_container_id: Optional[str]
    start_time: Optional[datetime]
    end_time: Optional[datetime]
    scheduled_start_time: Optional[datetime] = Field(
        None,
        description="Scheduled start time of the meeting (UTC)",
    )
    scheduled_end_time: Optional[datetime] = Field(
        None,
        description="Scheduled end time of the meeting (UTC)",
    )
    data: Optional[Dict] = Field(
        default_factory=dict,
        description="JSON data containing meeting metadata like name, participants, languages, notes, and status reasons",
    )
    created_at: datetime
    updated_at: datetime

    @field_validator("status", mode="before")
    @classmethod
    def normalize_status(cls, v):
        """Normalize invalid status values to valid enum values"""
        if isinstance(v, str):
            # Try to use the value as-is first
            try:
                return MeetingStatus(v)
            except ValueError:
                # For unknown status values, default to 'completed' as a safe fallback
                logger.warning("Unknown meeting status '%s' → completed", v)
                return MeetingStatus.COMPLETED

        return v

    @field_validator("data")
    @classmethod
    def validate_status_data(cls, v, info: ValidationInfo):
        """Validate that status-related data is consistent with meeting status."""
        if v is None:
            return v

        status = info.data.get("status")
        if not status:
            return v

        # Validate completion reasons
        if status == MeetingStatus.COMPLETED:
            reason = v.get("completion_reason")
            if reason and reason not in [r.value for r in MeetingCompletionReason]:
                raise ValueError(
                    f"Invalid completion_reason '{reason}'. Must be one of: {[r.value for r in MeetingCompletionReason]}"
                )

        # Validate failure stage
        elif status == MeetingStatus.FAILED:
            stage = v.get("failure_stage")
            if stage and stage not in [s.value for s in MeetingFailureStage]:
                raise ValueError(
                    f"Invalid failure_stage '{stage}'. Must be one of: {[s.value for s in MeetingFailureStage]}"
                )

        return v

    model_config = ConfigDict(from_attributes=True, use_enum_values=True)


# --- Meeting Update Schema ---
class MeetingDataUpdate(BaseModel):
    """Schema for updating meeting data fields - restricted to user-editable fields only"""

    name: Optional[str] = Field(None, description="Meeting name/title")
    participants: Optional[List[str]] = Field(None, description="List of participant names")
    languages: Optional[List[str]] = Field(None, description="List of language codes detected/used in the meeting")
    notes: Optional[str] = Field(None, description="Meeting notes or description")

    @field_validator("languages")
    @classmethod
    def validate_languages(cls, v):
        """Validate that all language codes in the list are accepted faster-whisper codes."""
        if v is not None:
            invalid_languages = [lang for lang in v if lang not in ACCEPTED_LANGUAGE_CODES]
            if invalid_languages:
                raise ValueError(
                    f"Invalid language codes: {invalid_languages}. Must be one of: {sorted(ACCEPTED_LANGUAGE_CODES)}"
                )
        return v


class MeetingUpdate(BaseModel):
    """Schema for updating meeting data via PATCH requests"""

    data: MeetingDataUpdate = Field(..., description="Meeting metadata to update")


# --- Bot Configuration Update Schema ---
class MeetingConfigUpdate(BaseModel):
    """Schema for updating bot configuration (language and task)"""

    language: Optional[str] = Field(None, description="New language code (e.g., 'en', 'es')")
    task: Optional[str] = Field(None, description="New task ('transcribe' or 'translate')")

    @field_validator("language")
    @classmethod
    def validate_language(cls, v):
        """Validate that the language code is one of the accepted faster-whisper codes."""
        if v is not None and v != "" and v not in ACCEPTED_LANGUAGE_CODES:
            raise ValueError(f"Invalid language code '{v}'. Must be one of: {sorted(ACCEPTED_LANGUAGE_CODES)}")
        return v

    @field_validator("task")
    @classmethod
    def validate_task(cls, v):
        """Validate that the task is one of the allowed tasks."""
        if v is not None and v != "" and v not in ALLOWED_TASKS:
            raise ValueError(f"Invalid task '{v}'. Must be one of: {sorted(ALLOWED_TASKS)}")
        return v


# --- Transcription Schemas ---


class TranscriptionSegment(BaseModel):
    # id: Optional[int] # No longer relevant to expose outside DB
    start_time: float = Field(..., alias="start")  # Add alias
    end_time: float = Field(..., alias="end")  # Add alias
    text: str
    language: Optional[str]
    created_at: Optional[datetime]
    speaker: Optional[str] = None
    absolute_start_time: Optional[datetime] = Field(None, description="Absolute start timestamp of the segment (UTC)")
    absolute_end_time: Optional[datetime] = Field(None, description="Absolute end timestamp of the segment (UTC)")

    @field_validator("language")
    @classmethod
    def validate_language(cls, v):
        """Validate that the language code is one of the accepted faster-whisper codes."""
        if v is not None and v != "" and v not in ACCEPTED_LANGUAGE_CODES:
            raise ValueError(f"Invalid language code '{v}'. Must be one of: {sorted(ACCEPTED_LANGUAGE_CODES)}")
        return v

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


# --- WebSocket Schema (NEW - Represents data from WhisperLive) ---


class WhisperLiveData(BaseModel):
    """Schema for the data message sent by WhisperLive to the collector."""

    uid: str  # Unique identifier from the original client connection
    platform: Platform
    meeting_url: Optional[str] = None
    token: str  # User API token
    meeting_id: str  # Native Meeting ID (string, e.g., 'abc-xyz-pqr')
    segments: List[TranscriptionSegment]

    @field_validator("platform", mode="before")
    @classmethod
    def validate_whisperlive_platform_str(cls, v):
        """Validate that the platform string is one of the supported platforms"""
        try:
            Platform(v)
            return v
        except ValueError:
            supported = ", ".join([p.value for p in Platform])
            raise ValueError(f"Invalid platform '{v}'. Must be one of: {supported}")


# --- Other Schemas ---
class TranscriptionResponse(BaseModel):  # Doesn't inherit MeetingResponse to avoid redundancy if joining data
    """Response for getting a meeting's transcript."""

    # Meeting details (consider duplicating fields from MeetingResponse or nesting)
    id: int = Field(..., description="Internal database ID for the meeting")
    platform: Platform
    native_meeting_id: Optional[str]
    constructed_meeting_url: Optional[str]
    status: str
    start_time: Optional[datetime]
    end_time: Optional[datetime]
    # ---
    segments: List[TranscriptionSegment] = Field(..., description="List of transcript segments")

    model_config = ConfigDict(from_attributes=True, use_enum_values=True)


# --- Utility Schemas ---


class HealthResponse(BaseModel):
    status: str
    redis: str
    database: str
    stream: Optional[str] = None
    timestamp: datetime


class ErrorResponse(BaseModel):
    detail: str  # Standard FastAPI error response uses 'detail'


class MeetingListResponse(BaseModel):
    meetings: List[MeetingResponse]


# --- ADD Bot Status Schemas ---
class BotStatus(BaseModel):
    container_id: Optional[str] = None
    container_name: Optional[str] = None
    platform: Optional[str] = None
    native_meeting_id: Optional[str] = None
    status: Optional[str] = None
    normalized_status: Optional[str] = None
    created_at: Optional[str] = None
    labels: Optional[Dict[str, str]] = None
    meeting_id_from_name: Optional[str] = None  # Example auxiliary info

    @field_validator("normalized_status")
    @classmethod
    def validate_normalized_status(cls, v):
        if v is None:
            return v
        allowed = {"Requested", "Starting", "Up", "Stopping", "Exited", "Failed"}
        if v not in allowed:
            raise ValueError(f"normalized_status must be one of {sorted(allowed)}")
        return v


class BotStatusResponse(BaseModel):
    running_bots: List[BotStatus]


# --- END Bot Status Schemas ---


# --- Analytics Schemas ---
class UserTableResponse(BaseModel):
    """User data for analytics table - excludes sensitive fields"""

    id: int
    email: str
    name: Optional[str]
    image_url: Optional[str]
    created_at: datetime
    max_concurrent_bots: int
    # Excludes: data, api_tokens

    model_config = ConfigDict(from_attributes=True)


class MeetingTableResponse(BaseModel):
    """Meeting data for analytics table - excludes sensitive fields"""

    id: int
    user_id: int
    platform: Platform
    native_meeting_id: Optional[str]
    status: MeetingStatus
    start_time: Optional[datetime]
    end_time: Optional[datetime]
    created_at: datetime
    updated_at: datetime
    # Excludes: data, transcriptions, sessions

    @field_validator("status", mode="before")
    @classmethod
    def normalize_status(cls, v):
        """Normalize invalid status values to valid enum values"""
        if isinstance(v, str):
            # Try to use the value as-is first
            try:
                return MeetingStatus(v)
            except ValueError:
                # For unknown status values, default to 'completed' as a safe fallback
                logger.warning("Unknown meeting status '%s' → completed", v)
                return MeetingStatus.COMPLETED

        return v

    model_config = ConfigDict(from_attributes=True, use_enum_values=True)


class MeetingSessionResponse(BaseModel):
    """Meeting session data for telematics"""

    id: int
    meeting_id: int
    session_uid: str
    session_start_time: datetime

    model_config = ConfigDict(from_attributes=True)


class TranscriptionStats(BaseModel):
    """Transcription statistics for a meeting"""

    total_transcriptions: int
    total_duration: float
    unique_speakers: int
    languages_detected: List[str]


class MeetingPerformanceMetrics(BaseModel):
    """Performance metrics for a meeting"""

    join_time: Optional[float]  # seconds to join
    admission_time: Optional[float]  # seconds to get admitted
    total_duration: Optional[float]  # meeting duration in seconds
    bot_uptime: Optional[float]  # bot uptime in seconds


class MeetingTelematicsResponse(BaseModel):
    """Comprehensive telematics data for a specific meeting"""

    meeting: MeetingResponse
    sessions: List[MeetingSessionResponse]
    transcription_stats: Optional[TranscriptionStats]
    performance_metrics: Optional[MeetingPerformanceMetrics]


class UserMeetingStats(BaseModel):
    """User meeting statistics"""

    total_meetings: int
    completed_meetings: int
    failed_meetings: int
    active_meetings: int
    total_duration: Optional[float]  # total meeting duration in seconds
    average_duration: Optional[float]  # average meeting duration in seconds


class UserUsagePatterns(BaseModel):
    """User usage patterns"""

    most_used_platform: Optional[str]
    meetings_per_day: float
    peak_usage_hours: List[int]  # hours of day (0-23)
    last_activity: Optional[datetime]


class UserAnalyticsResponse(BaseModel):
    """Comprehensive user analytics data including full user record"""

    user: UserDetailResponse  # This includes the data field
    meeting_stats: UserMeetingStats
    usage_patterns: UserUsagePatterns
    api_tokens: Optional[List[TokenResponse]]  # Optional for security


# --- END Analytics Schemas ---


# --- Google Calendar Integration Schemas ---


class CalendarAuthTokenResponse(BaseModel):
    """Response containing calendar auth token for OAuth flow"""

    calendar_auth_token: str = Field(..., description="Token to include in OAuth state. Valid for 10 minutes.")
    expires_in: int = Field(default=600, description="Token validity in seconds")


class GoogleAuthUrlResponse(BaseModel):
    """Response containing Google OAuth authorization URL"""

    auth_url: str = Field(..., description="URL to redirect user to for Google OAuth consent")


class GoogleCallbackRequest(BaseModel):
    """Request body for Google OAuth callback"""

    code: str = Field(..., description="Authorization code from Google OAuth callback")
    state: Optional[str] = Field(None, description="State parameter for CSRF protection")


class GoogleIntegrationResponse(BaseModel):
    """Response showing Google integration status"""

    id: int
    user_id: int
    google_user_id: str
    email: str
    name: Optional[str]
    picture: Optional[str]
    scopes: Optional[List[str]]
    auto_join_enabled: bool
    connected_at: datetime = Field(..., alias="created_at")
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class GoogleIntegrationUpdate(BaseModel):
    """Request to update Google integration settings"""

    auto_join_enabled: Optional[bool] = Field(None, description="Enable/disable auto-join for meetings")
    bot_name: Optional[str] = Field(None, max_length=100, description="Name shown when bot joins meeting")
    auto_join_mode: Optional[str] = Field(
        None, description="Which events to auto-join: 'all_events' or 'my_events_only'"
    )


class CalendarEventAttendee(BaseModel):
    """Attendee information from a calendar event"""

    email: str
    display_name: Optional[str] = None
    response_status: Optional[str] = None  # "accepted", "declined", "tentative", "needsAction"
    is_organizer: bool = False
    is_self: bool = False


class CalendarEvent(BaseModel):
    """Calendar event with meeting information (Google Meet, MS Teams, etc.)"""

    id: str = Field(..., description="Google Calendar event ID")
    summary: Optional[str] = Field(None, description="Event title")
    description: Optional[str] = Field(None, description="Event description")
    start_time: datetime = Field(..., description="Event start time")
    end_time: datetime = Field(..., description="Event end time")
    google_meet_link: Optional[str] = Field(None, description="Google Meet URL if present")
    teams_link: Optional[str] = Field(None, description="Microsoft Teams URL if present")
    native_meeting_id: Optional[str] = Field(None, description="Extracted meeting code/ID")
    meeting_platform: Optional[str] = Field(None, description="Detected platform: google_meet, teams, zoom, etc.")
    location: Optional[str] = Field(None, description="Event location")
    attendees: List[CalendarEventAttendee] = Field(default_factory=list)
    organizer_email: Optional[str] = None
    is_creator_self: bool = Field(False, description="True if the authenticated user created this event")
    is_organizer_self: bool = Field(False, description="True if the authenticated user is the organizer")
    status: str = Field("confirmed", description="Event status: confirmed, tentative, cancelled")
    html_link: Optional[str] = Field(None, description="Link to view event in Google Calendar")


class CalendarEventsResponse(BaseModel):
    """Response containing calendar events with Google Meet links"""

    events: List[CalendarEvent] = Field(default_factory=list)
    next_page_token: Optional[str] = None
    total_count: int = 0


class CalendarAutoJoinRequest(BaseModel):
    """Request to auto-join a specific calendar event"""

    event_id: str = Field(..., description="Google Calendar event ID to join")
    bot_name: Optional[str] = Field(None, description="Custom name for the bot")
    language: Optional[str] = Field(None, description="Transcription language code")


# --- END Google Calendar Integration Schemas ---


# --- Webhook Schemas ---


class WebhookEventType(str, Enum):
    """Supported webhook event types"""

    # Bot lifecycle events
    BOT_REQUESTED = "bot.requested"
    BOT_JOINING = "bot.joining"
    BOT_AWAITING_ADMISSION = "bot.awaiting_admission"
    BOT_ACTIVE = "bot.active"
    BOT_STOPPING = "bot.stopping"
    BOT_ENDED = "bot.ended"
    BOT_FAILED = "bot.failed"

    # Meeting/Calendar events
    MEETING_CREATED = "meeting.created"
    MEETING_UPDATED = "meeting.updated"
    MEETING_RESCHEDULED = "meeting.rescheduled"
    MEETING_CANCELLED = "meeting.cancelled"
    MEETING_STATUS_CHANGE = "meeting.status_change"

    # Transcript events
    TRANSCRIPT_READY = "transcript.ready"
    TRANSCRIPT_SEGMENT = "transcript.segment"

    # Wildcard for all events
    ALL = "*"


class WebhookCreate(BaseModel):
    """Request to create a new webhook"""

    url: str = Field(..., description="Webhook endpoint URL (must be HTTPS in production)")
    events: List[str] = Field(default=["*"], description="List of event types to subscribe to. Use '*' for all events.")
    description: Optional[str] = Field(None, max_length=255, description="Optional description for the webhook")

    @field_validator("url")
    @classmethod
    def validate_url(cls, v):
        if not v.startswith(("http://", "https://")):
            raise ValueError("URL must start with http:// or https://")
        return v

    @field_validator("events")
    @classmethod
    def validate_events(cls, v):
        valid_events = {e.value for e in WebhookEventType}
        for event in v:
            if event not in valid_events:
                raise ValueError(f"Invalid event type '{event}'. Valid types: {sorted(valid_events)}")
        return v


class WebhookUpdate(BaseModel):
    """Request to update a webhook"""

    url: Optional[str] = Field(None, description="New webhook URL")
    events: Optional[List[str]] = Field(None, description="New list of event types")
    enabled: Optional[bool] = Field(None, description="Enable or disable the webhook")
    description: Optional[str] = Field(None, max_length=255, description="New description")

    @field_validator("url")
    @classmethod
    def validate_url(cls, v):
        if v is not None and not v.startswith(("http://", "https://")):
            raise ValueError("URL must start with http:// or https://")
        return v

    @field_validator("events")
    @classmethod
    def validate_events(cls, v):
        if v is not None:
            valid_events = {e.value for e in WebhookEventType}
            for event in v:
                if event not in valid_events:
                    raise ValueError(f"Invalid event type '{event}'. Valid types: {sorted(valid_events)}")
        return v


class WebhookResponse(BaseModel):
    """Response containing webhook configuration"""

    id: int
    user_id: int
    url: str
    events: List[str]
    enabled: bool
    description: Optional[str]
    secret: Optional[str] = Field(None, description="Webhook secret (only shown on creation)")
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class WebhookListResponse(BaseModel):
    """Response containing list of webhooks"""

    webhooks: List[WebhookResponse]


class WebhookTestResponse(BaseModel):
    """Response from testing a webhook"""

    success: bool
    status_code: Optional[int] = None
    response_time_ms: Optional[float] = None
    error: Optional[str] = None


class WebhookDelivery(BaseModel):
    """Webhook delivery payload sent to user's endpoint"""

    event: str = Field(..., description="Event type that triggered the webhook")
    timestamp: datetime = Field(..., description="When the event occurred")
    data: Dict[str, Any] = Field(..., description="Event-specific data")

    # Meeting data (included for most events)
    meeting: Optional[Dict[str, Any]] = Field(None, description="Meeting details if applicable")


# --- END Webhook Schemas ---
