"""
Tests for the auto-join scheduler module.
"""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock, Mock


class TestGetUpcomingMeetsSync:
    """Tests for get_upcoming_meets_sync function."""

    @patch("scheduler.httpx.Client")
    def test_returns_events_with_google_meet_links(self, mock_client_class):
        """Should return only events with Google Meet links."""
        from scheduler import get_upcoming_meets_sync

        mock_client = MagicMock()
        mock_client_class.return_value.__enter__ = Mock(return_value=mock_client)
        mock_client_class.return_value.__exit__ = Mock(return_value=False)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "items": [
                {
                    "id": "event1",
                    "summary": "Team Standup",
                    "status": "confirmed",
                    "start": {"dateTime": "2025-12-23T10:00:00Z"},
                    "conferenceData": {
                        "entryPoints": [{"entryPointType": "video", "uri": "https://meet.google.com/abc-defg-hij"}]
                    },
                    "creator": {"self": True},
                    "organizer": {"self": True},
                },
                {
                    "id": "event2",
                    "summary": "No Meet Link Event",
                    "status": "confirmed",
                    "start": {"dateTime": "2025-12-23T11:00:00Z"},
                },
            ]
        }
        mock_client.get.return_value = mock_response

        events = get_upcoming_meets_sync("fake_token", minutes_ahead=60)

        assert len(events) == 1
        assert events[0]["event_id"] == "event1"
        assert events[0]["native_meeting_id"] == "abc-defg-hij"
        assert events[0]["is_creator_self"] is True
        assert events[0]["is_organizer_self"] is True

    @patch("scheduler.httpx.Client")
    def test_excludes_cancelled_events(self, mock_client_class):
        """Should exclude cancelled events."""
        from scheduler import get_upcoming_meets_sync

        mock_client = MagicMock()
        mock_client_class.return_value.__enter__ = Mock(return_value=mock_client)
        mock_client_class.return_value.__exit__ = Mock(return_value=False)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "items": [
                {
                    "id": "event1",
                    "summary": "Cancelled Meeting",
                    "status": "cancelled",
                    "start": {"dateTime": "2025-12-23T10:00:00Z"},
                    "conferenceData": {
                        "entryPoints": [{"entryPointType": "video", "uri": "https://meet.google.com/abc-defg-hij"}]
                    },
                },
            ]
        }
        mock_client.get.return_value = mock_response

        events = get_upcoming_meets_sync("fake_token", minutes_ahead=60)

        assert len(events) == 0

    @patch("scheduler.httpx.Client")
    def test_handles_api_error(self, mock_client_class):
        """Should return empty list on API error."""
        from scheduler import get_upcoming_meets_sync

        mock_client = MagicMock()
        mock_client_class.return_value.__enter__ = Mock(return_value=mock_client)
        mock_client_class.return_value.__exit__ = Mock(return_value=False)

        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.text = "Unauthorized"
        mock_client.get.return_value = mock_response

        events = get_upcoming_meets_sync("fake_token", minutes_ahead=60)

        assert events == []


class TestRefreshTokenSync:
    """Tests for refresh_token_sync function."""

    @patch("scheduler.httpx.Client")
    def test_returns_access_token_on_success(self, mock_client_class):
        """Should return access token on successful refresh."""
        from scheduler import refresh_token_sync

        mock_client = MagicMock()
        mock_client_class.return_value.__enter__ = Mock(return_value=mock_client)
        mock_client_class.return_value.__exit__ = Mock(return_value=False)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"access_token": "new_token_123"}
        mock_client.post.return_value = mock_response

        token = refresh_token_sync("refresh_token", "client_id", "client_secret")

        assert token == "new_token_123"

    @patch("scheduler.httpx.Client")
    def test_returns_none_on_failure(self, mock_client_class):
        """Should return None on refresh failure."""
        from scheduler import refresh_token_sync

        mock_client = MagicMock()
        mock_client_class.return_value.__enter__ = Mock(return_value=mock_client)
        mock_client_class.return_value.__exit__ = Mock(return_value=False)

        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.text = "Invalid refresh token"
        mock_client.post.return_value = mock_response

        token = refresh_token_sync("refresh_token", "client_id", "client_secret")

        assert token is None


class TestSpawnBotSync:
    """Tests for spawn_bot_sync function."""

    @patch("scheduler.httpx.Client")
    def test_returns_true_on_success(self, mock_client_class):
        """Should return True when bot is created successfully."""
        from scheduler import spawn_bot_sync

        mock_client = MagicMock()
        mock_client_class.return_value.__enter__ = Mock(return_value=mock_client)
        mock_client_class.return_value.__exit__ = Mock(return_value=False)

        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_client.post.return_value = mock_response

        result = spawn_bot_sync("api_key", "abc-defg-hij", "Notetaker", "Team Standup")

        assert result is True
        mock_client.post.assert_called_once()

    @patch("scheduler.httpx.Client")
    def test_returns_true_on_conflict(self, mock_client_class):
        """Should return True when bot already exists (409)."""
        from scheduler import spawn_bot_sync

        mock_client = MagicMock()
        mock_client_class.return_value.__enter__ = Mock(return_value=mock_client)
        mock_client_class.return_value.__exit__ = Mock(return_value=False)

        mock_response = MagicMock()
        mock_response.status_code = 409
        mock_client.post.return_value = mock_response

        result = spawn_bot_sync("api_key", "abc-defg-hij", "Notetaker", "Team Standup")

        assert result is True

    @patch("scheduler.httpx.Client")
    def test_returns_false_on_error(self, mock_client_class):
        """Should return False on other errors."""
        from scheduler import spawn_bot_sync

        mock_client = MagicMock()
        mock_client_class.return_value.__enter__ = Mock(return_value=mock_client)
        mock_client_class.return_value.__exit__ = Mock(return_value=False)

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"
        mock_client.post.return_value = mock_response

        result = spawn_bot_sync("api_key", "abc-defg-hij", "Notetaker", "Team Standup")

        assert result is False


class TestProcessAutoJoinForUser:
    """Tests for process_auto_join_for_user function."""

    @patch("scheduler.spawn_bot_sync")
    @patch("scheduler.get_upcoming_meets_sync")
    @patch("scheduler.refresh_token_sync")
    def test_joins_all_events_when_mode_is_all(self, mock_refresh, mock_get_events, mock_spawn):
        """Should join all events when auto_join_mode is 'all_events'."""
        from scheduler import process_auto_join_for_user

        mock_refresh.return_value = "access_token"
        now = datetime.now(timezone.utc)
        mock_get_events.return_value = [
            {
                "event_id": "event1",
                "summary": "Team Standup",
                "start_time": now + timedelta(minutes=1),
                "native_meeting_id": "abc-defg-hij",
                "meet_link": "https://meet.google.com/abc-defg-hij",
                "is_creator_self": False,
                "is_organizer_self": False,
            },
        ]
        mock_spawn.return_value = True

        process_auto_join_for_user(
            account_user_id=1,
            account_id=1,
            external_user_id="user123",
            integration_id=1,
            refresh_token="refresh",
            client_id="client",
            client_secret="secret",
            api_key="api_key",
            bot_name="Notetaker",
            auto_join_mode="all_events",
        )

        mock_spawn.assert_called_once_with(
            api_key="api_key",
            native_meeting_id="abc-defg-hij",
            bot_name="Notetaker",
            event_summary="Team Standup",
        )

    @patch("scheduler.spawn_bot_sync")
    @patch("scheduler.get_upcoming_meets_sync")
    @patch("scheduler.refresh_token_sync")
    def test_skips_non_owned_events_when_mode_is_my_events_only(self, mock_refresh, mock_get_events, mock_spawn):
        """Should skip events where user is not creator/organizer when mode is 'my_events_only'."""
        from scheduler import process_auto_join_for_user

        mock_refresh.return_value = "access_token"
        now = datetime.now(timezone.utc)
        mock_get_events.return_value = [
            {
                "event_id": "event1",
                "summary": "Someone Else's Meeting",
                "start_time": now + timedelta(minutes=1),
                "native_meeting_id": "abc-defg-hij",
                "meet_link": "https://meet.google.com/abc-defg-hij",
                "is_creator_self": False,
                "is_organizer_self": False,
            },
        ]

        process_auto_join_for_user(
            account_user_id=1,
            account_id=1,
            external_user_id="user123",
            integration_id=1,
            refresh_token="refresh",
            client_id="client",
            client_secret="secret",
            api_key="api_key",
            bot_name="Notetaker",
            auto_join_mode="my_events_only",
        )

        mock_spawn.assert_not_called()

    @patch("scheduler.spawn_bot_sync")
    @patch("scheduler.get_upcoming_meets_sync")
    @patch("scheduler.refresh_token_sync")
    def test_joins_owned_events_when_mode_is_my_events_only(self, mock_refresh, mock_get_events, mock_spawn):
        """Should join events where user is creator/organizer when mode is 'my_events_only'."""
        from scheduler import process_auto_join_for_user

        mock_refresh.return_value = "access_token"
        now = datetime.now(timezone.utc)
        mock_get_events.return_value = [
            {
                "event_id": "event1",
                "summary": "My Meeting",
                "start_time": now + timedelta(minutes=1),
                "native_meeting_id": "abc-defg-hij",
                "meet_link": "https://meet.google.com/abc-defg-hij",
                "is_creator_self": True,
                "is_organizer_self": False,
            },
        ]
        mock_spawn.return_value = True

        process_auto_join_for_user(
            account_user_id=1,
            account_id=1,
            external_user_id="user123",
            integration_id=1,
            refresh_token="refresh",
            client_id="client",
            client_secret="secret",
            api_key="api_key",
            bot_name="Notetaker",
            auto_join_mode="my_events_only",
        )

        mock_spawn.assert_called_once()

    @patch("scheduler.spawn_bot_sync")
    @patch("scheduler.get_upcoming_meets_sync")
    @patch("scheduler.refresh_token_sync")
    def test_skips_events_too_far_in_future(self, mock_refresh, mock_get_events, mock_spawn):
        """Should skip events that are more than AUTO_JOIN_MINUTES_BEFORE away."""
        from scheduler import process_auto_join_for_user

        mock_refresh.return_value = "access_token"
        now = datetime.now(timezone.utc)
        mock_get_events.return_value = [
            {
                "event_id": "event1",
                "summary": "Future Meeting",
                "start_time": now + timedelta(minutes=10),  # Too far in future
                "native_meeting_id": "abc-defg-hij",
                "meet_link": "https://meet.google.com/abc-defg-hij",
                "is_creator_self": True,
                "is_organizer_self": True,
            },
        ]

        process_auto_join_for_user(
            account_user_id=1,
            account_id=1,
            external_user_id="user123",
            integration_id=1,
            refresh_token="refresh",
            client_id="client",
            client_secret="secret",
            api_key="api_key",
            bot_name="Notetaker",
            auto_join_mode="all_events",
        )

        mock_spawn.assert_not_called()

    @patch("scheduler.get_upcoming_meets_sync")
    @patch("scheduler.refresh_token_sync")
    def test_handles_token_refresh_failure(self, mock_refresh, mock_get_events):
        """Should handle token refresh failure gracefully."""
        from scheduler import process_auto_join_for_user

        mock_refresh.return_value = None

        # Should not raise exception
        process_auto_join_for_user(
            account_user_id=1,
            account_id=1,
            external_user_id="user123",
            integration_id=1,
            refresh_token="refresh",
            client_id="client",
            client_secret="secret",
            api_key="api_key",
            bot_name="Notetaker",
            auto_join_mode="all_events",
        )

        mock_get_events.assert_not_called()

    @patch("scheduler.spawn_bot_sync")
    @patch("scheduler.get_upcoming_meets_sync")
    @patch("scheduler.refresh_token_sync")
    def test_uses_default_bot_name_when_none(self, mock_refresh, mock_get_events, mock_spawn):
        """Should use 'Notetaker' as default bot name when bot_name is None."""
        from scheduler import process_auto_join_for_user

        mock_refresh.return_value = "access_token"
        now = datetime.now(timezone.utc)
        mock_get_events.return_value = [
            {
                "event_id": "event1",
                "summary": "Team Standup",
                "start_time": now + timedelta(minutes=1),
                "native_meeting_id": "abc-defg-hij",
                "meet_link": "https://meet.google.com/abc-defg-hij",
                "is_creator_self": True,
                "is_organizer_self": True,
            },
        ]
        mock_spawn.return_value = True

        process_auto_join_for_user(
            account_user_id=1,
            account_id=1,
            external_user_id="user123",
            integration_id=1,
            refresh_token="refresh",
            client_id="client",
            client_secret="secret",
            api_key="api_key",
            bot_name=None,
            auto_join_mode="all_events",
        )

        mock_spawn.assert_called_once_with(
            api_key="api_key",
            native_meeting_id="abc-defg-hij",
            bot_name="Notetaker",
            event_summary="Team Standup",
        )


class TestCheckAndEnqueueAutoJoins:
    """Tests for check_and_enqueue_auto_joins function."""

    @patch("scheduler.get_queue")
    @patch("scheduler.get_sync_db_url")
    def test_enqueues_jobs_for_enabled_users(self, mock_get_db_url, mock_get_queue):
        """Should enqueue jobs for all users with auto_join_enabled."""
        import psycopg2
        from scheduler import check_and_enqueue_auto_joins

        mock_get_db_url.return_value = "postgresql://user:pass@localhost:5432/testdb"

        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [
            {
                "integration_id": 1,
                "account_user_id": 10,
                "refresh_token": "refresh_token_1",
                "bot_name": "My Bot",
                "auto_join_mode": "all_events",
                "external_user_id": "user123",
                "account_id": 1,
                "api_key": "api_key_1",
                "google_client_id": "client_id",
                "google_client_secret": "client_secret",
            },
            {
                "integration_id": 2,
                "account_user_id": 20,
                "refresh_token": "refresh_token_2",
                "bot_name": None,
                "auto_join_mode": "my_events_only",
                "external_user_id": "user456",
                "account_id": 1,
                "api_key": "api_key_1",
                "google_client_id": "client_id",
                "google_client_secret": "client_secret",
            },
        ]
        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__ = Mock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = Mock(return_value=False)

        mock_queue = MagicMock()
        mock_get_queue.return_value = mock_queue

        with patch.object(psycopg2, "connect", return_value=mock_conn):
            check_and_enqueue_auto_joins()

        assert mock_queue.enqueue.call_count == 2

    @patch("scheduler.get_queue")
    @patch("scheduler.get_sync_db_url")
    def test_handles_no_enabled_users(self, mock_get_db_url, mock_get_queue):
        """Should handle case with no enabled users gracefully."""
        import psycopg2
        from scheduler import check_and_enqueue_auto_joins

        mock_get_db_url.return_value = "postgresql://user:pass@localhost:5432/testdb"

        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__ = Mock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = Mock(return_value=False)

        mock_queue = MagicMock()
        mock_get_queue.return_value = mock_queue

        with patch.object(psycopg2, "connect", return_value=mock_conn):
            check_and_enqueue_auto_joins()

        mock_queue.enqueue.assert_not_called()


class TestSetupScheduler:
    """Tests for setup_scheduler function."""

    @patch("scheduler.get_scheduler")
    def test_schedules_periodic_job(self, mock_get_scheduler):
        """Should schedule the auto-join check job."""
        from scheduler import setup_scheduler

        mock_scheduler = MagicMock()
        mock_scheduler.get_jobs.return_value = []
        mock_get_scheduler.return_value = mock_scheduler

        scheduler = setup_scheduler()

        mock_scheduler.schedule.assert_called_once()
        assert scheduler == mock_scheduler

    @patch("scheduler.get_scheduler")
    def test_cancels_existing_jobs(self, mock_get_scheduler):
        """Should cancel existing auto-join jobs before scheduling new one."""
        from scheduler import setup_scheduler

        mock_existing_job = MagicMock()
        mock_existing_job.func_name = "scheduler.check_and_enqueue_auto_joins"
        mock_existing_job.id = "old_job_id"

        mock_scheduler = MagicMock()
        mock_scheduler.get_jobs.return_value = [mock_existing_job]
        mock_get_scheduler.return_value = mock_scheduler

        setup_scheduler()

        mock_scheduler.cancel.assert_called_once_with(mock_existing_job)
