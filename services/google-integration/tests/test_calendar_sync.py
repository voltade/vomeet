"""
Tests for the calendar_sync module.
"""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock, AsyncMock

from shared_models.models import ScheduledMeetingStatus


class TestExtractMeetCode:
    """Tests for extract_meet_code function."""

    def test_extracts_meet_code_from_url(self):
        from calendar_sync import extract_meet_code

        result = extract_meet_code("https://meet.google.com/abc-defg-hij")
        assert result == "abc-defg-hij"

    def test_extracts_meet_code_from_text(self):
        from calendar_sync import extract_meet_code

        result = extract_meet_code("Join meeting: abc-defg-hij")
        assert result == "abc-defg-hij"

    def test_returns_none_for_no_match(self):
        from calendar_sync import extract_meet_code

        result = extract_meet_code("No meeting code here")
        assert result is None


class TestExtractTeamsLink:
    """Tests for extract_teams_link function."""

    def test_extracts_teams_link_from_text(self):
        from calendar_sync import extract_teams_link

        result = extract_teams_link("Join Teams Meeting\nhttps://teams.microsoft.com/l/meetup-join/xyz123")
        assert result is not None
        assert "teams.microsoft.com" in result

    def test_returns_none_for_no_teams_link(self):
        from calendar_sync import extract_teams_link

        result = extract_teams_link("No teams link here")
        assert result is None


class TestParseCalendarEvent:
    """Tests for parse_calendar_event function."""

    def test_extracts_google_meet_from_conference_data(self):
        from calendar_sync import parse_calendar_event

        event = {
            "id": "event123",
            "summary": "Team Standup",
            "start": {"dateTime": "2025-12-23T10:00:00Z"},
            "end": {"dateTime": "2025-12-23T11:00:00Z"},
            "conferenceData": {
                "entryPoints": [{"entryPointType": "video", "uri": "https://meet.google.com/abc-defg-hij"}]
            },
        }

        result = parse_calendar_event(event)

        assert result["platform"] == "google_meet"
        assert result["native_meeting_id"] == "abc-defg-hij"
        assert result["meeting_url"] == "https://meet.google.com/abc-defg-hij"
        assert result["title"] == "Team Standup"

    def test_extracts_teams_from_description(self):
        from calendar_sync import parse_calendar_event

        event = {
            "id": "event456",
            "summary": "Project Meeting",
            "start": {"dateTime": "2025-12-23T14:00:00Z"},
            "end": {"dateTime": "2025-12-23T15:00:00Z"},
            "description": "Join Teams Meeting\nhttps://teams.microsoft.com/l/meetup-join/xyz123",
        }

        result = parse_calendar_event(event)

        assert result["platform"] == "teams"
        assert "teams.microsoft.com" in result["meeting_url"]

    def test_returns_none_for_missing_id(self):
        from calendar_sync import parse_calendar_event

        event = {"summary": "No ID Event", "start": {"dateTime": "2025-12-23T10:00:00Z"}}

        result = parse_calendar_event(event)
        assert result is None

    def test_returns_none_for_no_meeting_link(self):
        from calendar_sync import parse_calendar_event

        event = {
            "id": "event789",
            "summary": "Coffee chat",
            "description": "Let's grab coffee",
            "start": {"dateTime": "2025-12-23T10:00:00Z"},
        }

        result = parse_calendar_event(event)
        # Event is parsed but has no platform/meeting_url
        assert result is not None
        assert result["platform"] is None
        assert result["meeting_url"] is None


class TestBuildWebhookPayload:
    """Tests for build_webhook_payload function."""

    def test_builds_payload_for_meeting_created(self):
        from calendar_sync import build_webhook_payload

        # Mock ScheduledMeeting object
        scheduled = MagicMock()
        scheduled.id = 1
        scheduled.calendar_event_id = "event123"
        scheduled.calendar_provider = "google"
        scheduled.title = "Team Standup"
        scheduled.description = "Daily standup"
        scheduled.platform = "google_meet"
        scheduled.native_meeting_id = "abc-defg-hij"
        scheduled.meeting_url = "https://meet.google.com/abc-defg-hij"
        scheduled.scheduled_start_time = datetime(2025, 12, 23, 10, 0, 0, tzinfo=timezone.utc)
        scheduled.scheduled_end_time = datetime(2025, 12, 23, 11, 0, 0, tzinfo=timezone.utc)
        scheduled.is_creator_self = True
        scheduled.is_organizer_self = True
        scheduled.status = "scheduled"
        scheduled.attendees = [{"email": "user@example.com"}]
        scheduled.bot_meetings = []  # No bots spawned yet

        # Mock AccountUser object
        account_user = MagicMock()
        account_user.id = 1
        account_user.external_user_id = "user123"
        account_user.account_id = 1

        payload = build_webhook_payload("meeting.created", scheduled, account_user)

        assert payload["event"] == "meeting.created"
        assert payload["calendar_event"]["calendar_event_id"] == "event123"
        assert payload["calendar_event"]["title"] == "Team Standup"
        assert payload["calendar_event"]["platform"] == "google_meet"
        assert payload["user"]["external_user_id"] == "user123"

    def test_includes_bot_id_when_active_meeting_exists(self):
        from calendar_sync import build_webhook_payload

        # Mock ScheduledMeeting with an active bot
        scheduled = MagicMock()
        scheduled.id = 1
        scheduled.calendar_event_id = "event123"
        scheduled.calendar_provider = "google"
        scheduled.title = "Team Standup"
        scheduled.description = None
        scheduled.platform = "google_meet"
        scheduled.native_meeting_id = "abc-defg-hij"
        scheduled.meeting_url = "https://meet.google.com/abc-defg-hij"
        scheduled.scheduled_start_time = datetime(2025, 12, 23, 10, 0, 0, tzinfo=timezone.utc)
        scheduled.scheduled_end_time = datetime(2025, 12, 23, 11, 0, 0, tzinfo=timezone.utc)
        scheduled.is_creator_self = True
        scheduled.is_organizer_self = True
        scheduled.status = "bot_active"
        scheduled.attendees = []

        # Mock an active Meeting (bot)
        active_meeting = MagicMock()
        active_meeting.id = 42
        active_meeting.status = "active"
        scheduled.bot_meetings = [active_meeting]

        account_user = MagicMock()
        account_user.id = 1
        account_user.external_user_id = "user123"
        account_user.account_id = 1

        payload = build_webhook_payload("meeting.updated", scheduled, account_user)

        assert payload["calendar_event"]["bot_id"] == 42

    def test_includes_changes_for_update_events(self):
        from calendar_sync import build_webhook_payload

        scheduled = MagicMock()
        scheduled.id = 1
        scheduled.calendar_event_id = "event123"
        scheduled.calendar_provider = "google"
        scheduled.title = "New Title"
        scheduled.description = None
        scheduled.platform = "google_meet"
        scheduled.native_meeting_id = "abc-defg-hij"
        scheduled.meeting_url = "https://meet.google.com/abc-defg-hij"
        scheduled.scheduled_start_time = datetime(2025, 12, 23, 10, 0, 0, tzinfo=timezone.utc)
        scheduled.scheduled_end_time = datetime(2025, 12, 23, 11, 0, 0, tzinfo=timezone.utc)
        scheduled.is_creator_self = True
        scheduled.is_organizer_self = True
        scheduled.status = "scheduled"
        scheduled.attendees = []
        scheduled.bot_meetings = []

        account_user = MagicMock()
        account_user.id = 1
        account_user.external_user_id = "user123"
        account_user.account_id = 1

        changes = {"title": {"old": "Old Title", "new": "New Title"}}

        payload = build_webhook_payload("meeting.updated", scheduled, account_user, changes)

        assert payload["changes"] == changes


class TestSyncCalendarForUser:
    """Tests for sync_calendar_for_user function."""

    @pytest.mark.asyncio
    @patch("calendar_sync.send_webhook")
    @patch("calendar_sync.fetch_calendar_events")
    @patch("calendar_sync.refresh_access_token")
    async def test_creates_new_scheduled_meeting(self, mock_refresh, mock_fetch, mock_webhook):
        """Should create ScheduledMeeting for new calendar event."""
        from calendar_sync import sync_calendar_for_user

        # Mock access token refresh
        mock_refresh.return_value = "valid_access_token"

        # Mock calendar events response
        mock_fetch.return_value = [
            {
                "calendar_event_id": "event123",
                "title": "Team Standup",
                "description": "Daily standup meeting",
                "platform": "google_meet",
                "native_meeting_id": "abc-defg-hij",
                "meeting_url": "https://meet.google.com/abc-defg-hij",
                "scheduled_start_time": datetime(2026, 1, 20, 10, 0, 0, tzinfo=timezone.utc),
                "scheduled_end_time": datetime(2026, 1, 20, 11, 0, 0, tzinfo=timezone.utc),
                "is_creator_self": True,
                "is_organizer_self": True,
                "is_cancelled": False,
                "attendees": [{"email": "user@example.com"}],
            }
        ]

        # Mock DB session with async context manager
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(
            return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]))))
        )
        mock_db.add = MagicMock()
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock()

        # Mock integration
        mock_integration = MagicMock()
        mock_integration.id = 1
        mock_integration.auto_join_mode = "all_events"

        # Mock account user
        mock_account_user = MagicMock()
        mock_account_user.id = 1
        mock_account_user.external_user_id = "user123"
        mock_account_user.account_id = 1

        # Mock account
        mock_account = MagicMock()
        mock_account.id = 1
        mock_account.webhook_url = "https://example.com/webhook"
        mock_account.webhook_secret = "secret123"

        result = await sync_calendar_for_user(mock_integration, mock_account_user, mock_account, mock_db)

        # Should have created one meeting
        assert result["created"] == 1
        assert mock_db.add.called

    @pytest.mark.asyncio
    @patch("calendar_sync.send_webhook")
    @patch("calendar_sync.fetch_calendar_events")
    @patch("calendar_sync.refresh_access_token")
    async def test_sends_webhook_on_create(self, mock_refresh, mock_fetch, mock_webhook):
        """Should send meeting.created webhook for new events."""
        from calendar_sync import sync_calendar_for_user

        # Mock access token refresh
        mock_refresh.return_value = "valid_access_token"

        # Mock calendar events response
        mock_fetch.return_value = [
            {
                "calendar_event_id": "event456",
                "title": "Project Review",
                "description": None,
                "platform": "google_meet",
                "native_meeting_id": "xyz-abcd-efg",
                "meeting_url": "https://meet.google.com/xyz-abcd-efg",
                "scheduled_start_time": datetime(2026, 1, 21, 14, 0, 0, tzinfo=timezone.utc),
                "scheduled_end_time": datetime(2026, 1, 21, 15, 0, 0, tzinfo=timezone.utc),
                "is_creator_self": False,
                "is_organizer_self": False,
                "is_cancelled": False,
                "attendees": [],
            }
        ]

        # Mock DB session
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(
            return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]))))
        )
        mock_db.add = MagicMock()
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock()

        # Mock integration
        mock_integration = MagicMock()
        mock_integration.id = 1
        mock_integration.auto_join_mode = "all_events"

        # Mock account user
        mock_account_user = MagicMock()
        mock_account_user.id = 1
        mock_account_user.external_user_id = "user456"
        mock_account_user.account_id = 1

        # Mock account with webhook configured
        mock_account = MagicMock()
        mock_account.id = 1
        mock_account.webhook_url = "https://example.com/webhook"
        mock_account.webhook_secret = "secret123"

        await sync_calendar_for_user(mock_integration, mock_account_user, mock_account, mock_db)

        # Verify webhook was called with meeting.created event
        mock_webhook.assert_called_once()
        call_args = mock_webhook.call_args
        assert call_args[0][0] == "https://example.com/webhook"  # webhook_url
        assert call_args[0][2] == "meeting.created"  # event_type

    @pytest.mark.asyncio
    @patch("calendar_sync.send_webhook")
    @patch("calendar_sync.fetch_calendar_events")
    @patch("calendar_sync.refresh_access_token")
    async def test_handles_cancelled_event(self, mock_refresh, mock_fetch, mock_webhook):
        """Should mark event as cancelled and send webhook."""
        from calendar_sync import sync_calendar_for_user

        mock_refresh.return_value = "valid_access_token"

        # Return a cancelled event
        mock_fetch.return_value = [
            {
                "calendar_event_id": "event123",
                "title": "Cancelled Meeting",
                "description": None,
                "platform": "google_meet",
                "native_meeting_id": "abc-defg-hij",
                "meeting_url": "https://meet.google.com/abc-defg-hij",
                "scheduled_start_time": datetime(2026, 1, 20, 10, 0, 0, tzinfo=timezone.utc),
                "scheduled_end_time": datetime(2026, 1, 20, 11, 0, 0, tzinfo=timezone.utc),
                "is_creator_self": True,
                "is_organizer_self": True,
                "is_cancelled": True,  # Cancelled!
                "attendees": [],
            }
        ]

        # Mock existing scheduled meeting
        existing_meeting = MagicMock()
        existing_meeting.calendar_event_id = "event123"
        existing_meeting.status = ScheduledMeetingStatus.SCHEDULED.value

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(
            return_value=MagicMock(
                scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[existing_meeting])))
            )
        )
        mock_db.commit = AsyncMock()

        mock_integration = MagicMock()
        mock_integration.id = 1
        mock_integration.auto_join_mode = "all_events"

        mock_account_user = MagicMock()
        mock_account_user.id = 1
        mock_account_user.external_user_id = "user123"
        mock_account_user.account_id = 1

        mock_account = MagicMock()
        mock_account.id = 1
        mock_account.webhook_url = "https://example.com/webhook"
        mock_account.webhook_secret = "secret123"

        result = await sync_calendar_for_user(mock_integration, mock_account_user, mock_account, mock_db)

        assert result["cancelled"] == 1
        assert existing_meeting.status == ScheduledMeetingStatus.CANCELLED.value
        mock_webhook.assert_called_once()
        assert mock_webhook.call_args[0][2] == "meeting.cancelled"

    @pytest.mark.asyncio
    @patch("calendar_sync.send_webhook")
    @patch("calendar_sync.fetch_calendar_events")
    @patch("calendar_sync.refresh_access_token")
    async def test_handles_rescheduled_event_resets_status(self, mock_refresh, mock_fetch, mock_webhook):
        """Should reset BOT_REQUESTED status when meeting is rescheduled."""
        from calendar_sync import sync_calendar_for_user

        mock_refresh.return_value = "valid_access_token"

        new_start_time = datetime(2026, 1, 25, 14, 0, 0, tzinfo=timezone.utc)  # Changed time

        mock_fetch.return_value = [
            {
                "calendar_event_id": "event123",
                "title": "Rescheduled Meeting",
                "description": None,
                "platform": "google_meet",
                "native_meeting_id": "abc-defg-hij",
                "meeting_url": "https://meet.google.com/abc-defg-hij",
                "scheduled_start_time": new_start_time,
                "scheduled_end_time": datetime(2026, 1, 25, 15, 0, 0, tzinfo=timezone.utc),
                "is_creator_self": True,
                "is_organizer_self": True,
                "is_cancelled": False,
                "attendees": [],
            }
        ]

        # Existing meeting was BOT_REQUESTED (bot was about to spawn)
        existing_meeting = MagicMock()
        existing_meeting.id = 1
        existing_meeting.calendar_event_id = "event123"
        existing_meeting.status = ScheduledMeetingStatus.BOT_REQUESTED.value
        existing_meeting.scheduled_start_time = datetime(2026, 1, 20, 10, 0, 0, tzinfo=timezone.utc)  # Old time
        existing_meeting.title = "Original Title"
        existing_meeting.description = None
        existing_meeting.attendees = []
        existing_meeting.bot_meetings = []

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(
            return_value=MagicMock(
                scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[existing_meeting])))
            )
        )
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock()

        mock_integration = MagicMock()
        mock_integration.id = 1
        mock_integration.auto_join_mode = "all_events"

        mock_account_user = MagicMock()
        mock_account_user.id = 1
        mock_account_user.external_user_id = "user123"
        mock_account_user.account_id = 1

        mock_account = MagicMock()
        mock_account.id = 1
        mock_account.webhook_url = "https://example.com/webhook"
        mock_account.webhook_secret = None

        result = await sync_calendar_for_user(mock_integration, mock_account_user, mock_account, mock_db)

        # Status should be reset to SCHEDULED
        assert existing_meeting.status == ScheduledMeetingStatus.SCHEDULED.value
        assert result["rescheduled"] == 1
        mock_webhook.assert_called_once()
        assert mock_webhook.call_args[0][2] == "meeting.rescheduled"

    @pytest.mark.asyncio
    @patch("calendar_sync.send_webhook")
    @patch("calendar_sync.fetch_calendar_events")
    @patch("calendar_sync.refresh_access_token")
    async def test_reactivates_completed_meeting_on_reschedule(self, mock_refresh, mock_fetch, mock_webhook):
        """Should reactivate COMPLETED meeting when rescheduled (e.g., after bot failure)."""
        from calendar_sync import sync_calendar_for_user

        mock_refresh.return_value = "valid_access_token"

        new_start_time = datetime(2026, 1, 25, 14, 0, 0, tzinfo=timezone.utc)

        mock_fetch.return_value = [
            {
                "calendar_event_id": "event123",
                "title": "Retry Meeting",
                "description": None,
                "platform": "google_meet",
                "native_meeting_id": "abc-defg-hij",
                "meeting_url": "https://meet.google.com/abc-defg-hij",
                "scheduled_start_time": new_start_time,
                "scheduled_end_time": datetime(2026, 1, 25, 15, 0, 0, tzinfo=timezone.utc),
                "is_creator_self": True,
                "is_organizer_self": True,
                "is_cancelled": False,
                "attendees": [],
            }
        ]

        # Existing meeting was COMPLETED (bot finished or failed)
        existing_meeting = MagicMock()
        existing_meeting.id = 1
        existing_meeting.calendar_event_id = "event123"
        existing_meeting.status = ScheduledMeetingStatus.COMPLETED.value  # Was completed
        existing_meeting.scheduled_start_time = datetime(2026, 1, 20, 10, 0, 0, tzinfo=timezone.utc)
        existing_meeting.title = "Original Title"
        existing_meeting.description = None
        existing_meeting.attendees = []
        existing_meeting.bot_meetings = []

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(
            return_value=MagicMock(
                scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[existing_meeting])))
            )
        )
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock()

        mock_integration = MagicMock()
        mock_integration.id = 1
        mock_integration.auto_join_mode = "all_events"

        mock_account_user = MagicMock()
        mock_account_user.id = 1
        mock_account_user.external_user_id = "user123"
        mock_account_user.account_id = 1

        mock_account = MagicMock()
        mock_account.id = 1
        mock_account.webhook_url = "https://example.com/webhook"
        mock_account.webhook_secret = None

        result = await sync_calendar_for_user(mock_integration, mock_account_user, mock_account, mock_db)

        # Status should be reset to SCHEDULED (reactivated!)
        assert existing_meeting.status == ScheduledMeetingStatus.SCHEDULED.value
        assert result["rescheduled"] == 1

    @pytest.mark.asyncio
    @patch("calendar_sync.send_webhook")
    @patch("calendar_sync.fetch_calendar_events")
    @patch("calendar_sync.refresh_access_token")
    async def test_skips_events_without_meeting_link(self, mock_refresh, mock_fetch, mock_webhook):
        """Should skip events that don't have a meeting URL."""
        from calendar_sync import sync_calendar_for_user

        mock_refresh.return_value = "valid_access_token"

        # Event without meeting link
        mock_fetch.return_value = [
            {
                "calendar_event_id": "event123",
                "title": "Coffee Chat",
                "description": "Let's grab coffee",
                "platform": None,  # No platform
                "native_meeting_id": None,
                "meeting_url": None,  # No meeting URL
                "scheduled_start_time": datetime(2026, 1, 20, 10, 0, 0, tzinfo=timezone.utc),
                "scheduled_end_time": datetime(2026, 1, 20, 11, 0, 0, tzinfo=timezone.utc),
                "is_creator_self": True,
                "is_organizer_self": True,
                "is_cancelled": False,
                "attendees": [],
            }
        ]

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(
            return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]))))
        )
        mock_db.add = MagicMock()
        mock_db.commit = AsyncMock()

        mock_integration = MagicMock()
        mock_integration.id = 1
        mock_integration.auto_join_mode = "all_events"

        mock_account_user = MagicMock()
        mock_account_user.id = 1
        mock_account_user.external_user_id = "user123"
        mock_account_user.account_id = 1

        mock_account = MagicMock()
        mock_account.id = 1
        mock_account.webhook_url = "https://example.com/webhook"
        mock_account.webhook_secret = None

        result = await sync_calendar_for_user(mock_integration, mock_account_user, mock_account, mock_db)

        # Should not create any meetings
        assert result["created"] == 0
        assert not mock_db.add.called
        assert not mock_webhook.called

    @pytest.mark.asyncio
    @patch("calendar_sync.send_webhook")
    @patch("calendar_sync.fetch_calendar_events")
    @patch("calendar_sync.refresh_access_token")
    async def test_respects_my_events_only_mode(self, mock_refresh, mock_fetch, mock_webhook):
        """Should skip events user didn't create when auto_join_mode is my_events_only."""
        from calendar_sync import sync_calendar_for_user

        mock_refresh.return_value = "valid_access_token"

        mock_fetch.return_value = [
            {
                "calendar_event_id": "event123",
                "title": "Someone Else's Meeting",
                "description": None,
                "platform": "google_meet",
                "native_meeting_id": "abc-defg-hij",
                "meeting_url": "https://meet.google.com/abc-defg-hij",
                "scheduled_start_time": datetime(2026, 1, 20, 10, 0, 0, tzinfo=timezone.utc),
                "scheduled_end_time": datetime(2026, 1, 20, 11, 0, 0, tzinfo=timezone.utc),
                "is_creator_self": False,  # Not created by user
                "is_organizer_self": False,  # Not organized by user
                "is_cancelled": False,
                "attendees": [],
            }
        ]

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(
            return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]))))
        )
        mock_db.add = MagicMock()

        mock_integration = MagicMock()
        mock_integration.id = 1
        mock_integration.auto_join_mode = "my_events_only"  # Only join own events

        mock_account_user = MagicMock()
        mock_account_user.id = 1
        mock_account_user.external_user_id = "user123"
        mock_account_user.account_id = 1

        mock_account = MagicMock()
        mock_account.id = 1
        mock_account.webhook_url = None

        result = await sync_calendar_for_user(mock_integration, mock_account_user, mock_account, mock_db)

        # Should not create meeting for someone else's event
        assert result["created"] == 0
        assert not mock_db.add.called

    @pytest.mark.asyncio
    @patch("calendar_sync.send_webhook")
    @patch("calendar_sync.fetch_calendar_events")
    @patch("calendar_sync.refresh_access_token")
    async def test_handles_deleted_events(self, mock_refresh, mock_fetch, mock_webhook):
        """Should cancel meetings for events deleted from calendar."""
        from calendar_sync import sync_calendar_for_user

        mock_refresh.return_value = "valid_access_token"

        # Return empty list (event was deleted)
        mock_fetch.return_value = []

        # Existing scheduled meeting that's no longer in calendar
        existing_meeting = MagicMock()
        existing_meeting.calendar_event_id = "deleted_event"
        existing_meeting.status = ScheduledMeetingStatus.SCHEDULED.value
        existing_meeting.bot_meetings = []

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(
            return_value=MagicMock(
                scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[existing_meeting])))
            )
        )
        mock_db.commit = AsyncMock()

        mock_integration = MagicMock()
        mock_integration.id = 1
        mock_integration.auto_join_mode = "all_events"

        mock_account_user = MagicMock()
        mock_account_user.id = 1
        mock_account_user.external_user_id = "user123"
        mock_account_user.account_id = 1

        mock_account = MagicMock()
        mock_account.id = 1
        mock_account.webhook_url = "https://example.com/webhook"
        mock_account.webhook_secret = None

        result = await sync_calendar_for_user(mock_integration, mock_account_user, mock_account, mock_db)

        # Should mark as cancelled
        assert result["cancelled"] == 1
        assert existing_meeting.status == ScheduledMeetingStatus.CANCELLED.value
        mock_webhook.assert_called_once()
        assert mock_webhook.call_args[0][2] == "meeting.cancelled"

    @pytest.mark.asyncio
    @patch("calendar_sync.send_webhook")
    @patch("calendar_sync.fetch_calendar_events")
    @patch("calendar_sync.refresh_access_token")
    async def test_handles_title_update(self, mock_refresh, mock_fetch, mock_webhook):
        """Should detect and send webhook for title changes."""
        from calendar_sync import sync_calendar_for_user

        mock_refresh.return_value = "valid_access_token"

        mock_fetch.return_value = [
            {
                "calendar_event_id": "event123",
                "title": "New Title",  # Changed
                "description": "Same description",
                "platform": "google_meet",
                "native_meeting_id": "abc-defg-hij",
                "meeting_url": "https://meet.google.com/abc-defg-hij",
                "scheduled_start_time": datetime(2026, 1, 20, 10, 0, 0, tzinfo=timezone.utc),  # Same time
                "scheduled_end_time": datetime(2026, 1, 20, 11, 0, 0, tzinfo=timezone.utc),
                "is_creator_self": True,
                "is_organizer_self": True,
                "is_cancelled": False,
                "attendees": [],
            }
        ]

        existing_meeting = MagicMock()
        existing_meeting.id = 1
        existing_meeting.calendar_event_id = "event123"
        existing_meeting.status = ScheduledMeetingStatus.SCHEDULED.value
        existing_meeting.scheduled_start_time = datetime(2026, 1, 20, 10, 0, 0, tzinfo=timezone.utc)  # Same
        existing_meeting.title = "Old Title"  # Different
        existing_meeting.description = "Same description"
        existing_meeting.attendees = []
        existing_meeting.bot_meetings = []

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(
            return_value=MagicMock(
                scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[existing_meeting])))
            )
        )
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock()

        mock_integration = MagicMock()
        mock_integration.id = 1
        mock_integration.auto_join_mode = "all_events"

        mock_account_user = MagicMock()
        mock_account_user.id = 1
        mock_account_user.external_user_id = "user123"
        mock_account_user.account_id = 1

        mock_account = MagicMock()
        mock_account.id = 1
        mock_account.webhook_url = "https://example.com/webhook"
        mock_account.webhook_secret = None

        result = await sync_calendar_for_user(mock_integration, mock_account_user, mock_account, mock_db)

        # Should count as updated (not rescheduled since time didn't change)
        assert result["updated"] == 1
        assert result["rescheduled"] == 0
        mock_webhook.assert_called_once()
        assert mock_webhook.call_args[0][2] == "meeting.updated"

    @pytest.mark.asyncio
    @patch("calendar_sync.fetch_calendar_events")
    @patch("calendar_sync.refresh_access_token")
    async def test_returns_error_on_token_failure(self, mock_refresh, mock_fetch):
        """Should return error when token refresh fails."""
        from calendar_sync import sync_calendar_for_user

        mock_refresh.return_value = None  # Token refresh failed

        mock_db = AsyncMock()
        mock_integration = MagicMock()
        mock_account_user = MagicMock()
        mock_account_user.id = 1
        mock_account = MagicMock()

        result = await sync_calendar_for_user(mock_integration, mock_account_user, mock_account, mock_db)

        assert result["error"] == "token_refresh_failed"
        assert not mock_fetch.called
