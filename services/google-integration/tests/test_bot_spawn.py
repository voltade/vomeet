"""
Tests for the bot_spawn module.
"""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock


class TestSpawnBotsForUpcomingMeetings:
    """Tests for spawn_bots_for_upcoming_meetings function."""

    @patch("bot_spawn.get_rq_queue")
    @patch("bot_spawn.get_db_connection")
    def test_finds_meetings_within_buffer(self, mock_db, mock_queue):
        """Should find scheduled meetings within spawn buffer time."""
        from bot_spawn import spawn_bots_for_upcoming_meetings

        # Setup mock DB
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_db.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        # No meetings found
        mock_cursor.fetchall.return_value = []

        result = spawn_bots_for_upcoming_meetings()

        assert result["spawned"] == 0
        mock_cursor.execute.assert_called()  # Should have queried

    @patch("bot_spawn.get_rq_queue")
    @patch("bot_spawn.get_db_connection")
    def test_creates_meeting_and_enqueues_job(self, mock_db, mock_queue):
        """Should create Meeting record and enqueue RQ job."""
        from bot_spawn import spawn_bots_for_upcoming_meetings

        # Setup mock DB
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_db.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        # Mock a scheduled meeting
        now = datetime.now(timezone.utc)
        mock_cursor.fetchall.return_value = [
            {
                "id": 1,
                "account_id": 100,
                "title": "Test Meeting",
                "platform": "google_meet",
                "native_meeting_id": "abc-defg-hij",
                "meeting_url": "https://meet.google.com/abc-defg-hij",
                "scheduled_start_time": now + timedelta(minutes=10),
                "scheduled_end_time": now + timedelta(hours=1),
                "data": None,
                "bot_name": "Notetaker",
                "max_concurrent_bots": 5,
            }
        ]
        # fetchone for concurrency check
        mock_cursor.fetchone.side_effect = [
            (0,),  # active count
            (999,),  # new meeting ID
        ]

        # Mock RQ queue
        mock_q = MagicMock()
        mock_queue.return_value = mock_q
        mock_job = MagicMock()
        mock_job.id = "job-123"
        mock_q.enqueue.return_value = mock_job

        result = spawn_bots_for_upcoming_meetings()

        assert result["spawned"] == 1
        mock_q.enqueue.assert_called_once()

    @patch("bot_spawn.get_rq_queue")
    @patch("bot_spawn.get_db_connection")
    def test_respects_concurrency_limit(self, mock_db, mock_queue):
        """Should skip spawning when account is at concurrency limit."""
        from bot_spawn import spawn_bots_for_upcoming_meetings

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_db.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        now = datetime.now(timezone.utc)
        mock_cursor.fetchall.return_value = [
            {
                "id": 1,
                "account_id": 100,
                "title": "Test Meeting",
                "platform": "google_meet",
                "native_meeting_id": "abc-defg-hij",
                "meeting_url": "https://meet.google.com/abc-defg-hij",
                "scheduled_start_time": now + timedelta(minutes=10),
                "scheduled_end_time": now + timedelta(hours=1),
                "data": None,
                "bot_name": "Notetaker",
                "max_concurrent_bots": 2,  # Limit of 2
            }
        ]
        # Return 2 active bots (at limit)
        mock_cursor.fetchone.return_value = (2,)

        mock_q = MagicMock()
        mock_queue.return_value = mock_q

        result = spawn_bots_for_upcoming_meetings()

        # Should not spawn because at limit
        assert result["spawned"] == 0
        mock_q.enqueue.assert_not_called()

    @patch("bot_spawn.get_rq_queue")
    @patch("bot_spawn.get_db_connection")
    def test_rollback_on_enqueue_failure(self, mock_db, mock_queue):
        """Should rollback transaction if RQ enqueue fails."""
        from bot_spawn import spawn_bots_for_upcoming_meetings

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_db.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        now = datetime.now(timezone.utc)
        mock_cursor.fetchall.return_value = [
            {
                "id": 1,
                "account_id": 100,
                "title": "Test Meeting",
                "platform": "google_meet",
                "native_meeting_id": "abc-defg-hij",
                "meeting_url": "https://meet.google.com/abc-defg-hij",
                "scheduled_start_time": now + timedelta(minutes=10),
                "scheduled_end_time": now + timedelta(hours=1),
                "data": None,
                "bot_name": "Notetaker",
                "max_concurrent_bots": 5,
            }
        ]
        mock_cursor.fetchone.side_effect = [
            (0,),  # active count
            (999,),  # new meeting ID
        ]

        # Make enqueue fail
        mock_q = MagicMock()
        mock_queue.return_value = mock_q
        mock_q.enqueue.side_effect = Exception("Redis connection failed")

        result = spawn_bots_for_upcoming_meetings()

        # Should rollback (not commit)
        assert result["spawned"] == 0
        mock_conn.rollback.assert_called()
        mock_conn.commit.assert_not_called()

    @patch("bot_spawn.get_rq_queue")
    @patch("bot_spawn.get_db_connection")
    def test_skips_meetings_without_url(self, mock_db, mock_queue):
        """Should skip meetings that have no meeting URL."""
        from bot_spawn import spawn_bots_for_upcoming_meetings

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_db.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        now = datetime.now(timezone.utc)
        mock_cursor.fetchall.return_value = [
            {
                "id": 1,
                "account_id": 100,
                "title": "Test Meeting",
                "platform": "teams",  # Teams URLs need meeting_url, can't construct
                "native_meeting_id": "some-id",
                "meeting_url": None,  # No URL!
                "scheduled_start_time": now + timedelta(minutes=10),
                "scheduled_end_time": now + timedelta(hours=1),
                "data": None,
                "bot_name": "Notetaker",
                "max_concurrent_bots": 5,
            }
        ]
        mock_cursor.fetchone.return_value = (0,)

        mock_q = MagicMock()
        mock_queue.return_value = mock_q

        result = spawn_bots_for_upcoming_meetings()

        # Should not spawn - no URL
        assert result["spawned"] == 0
        mock_q.enqueue.assert_not_called()

    @patch("bot_spawn.get_rq_queue")
    @patch("bot_spawn.get_db_connection")
    def test_uses_bot_name_from_data(self, mock_db, mock_queue):
        """Should use bot_name from meeting data if available."""
        from bot_spawn import spawn_bots_for_upcoming_meetings

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_db.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        now = datetime.now(timezone.utc)
        mock_cursor.fetchall.return_value = [
            {
                "id": 1,
                "account_id": 100,
                "title": "Test Meeting",
                "platform": "google_meet",
                "native_meeting_id": "abc-defg-hij",
                "meeting_url": "https://meet.google.com/abc-defg-hij",
                "scheduled_start_time": now + timedelta(minutes=10),
                "scheduled_end_time": now + timedelta(hours=1),
                "data": {"bot_name": "CustomBot"},  # Custom name in data
                "bot_name": "IntegrationBot",  # Name from integration
                "max_concurrent_bots": 5,
            }
        ]
        mock_cursor.fetchone.side_effect = [(0,), (999,)]

        mock_q = MagicMock()
        mock_queue.return_value = mock_q
        mock_job = MagicMock()
        mock_job.id = "job-123"
        mock_q.enqueue.return_value = mock_job

        spawn_bots_for_upcoming_meetings()

        # Should use bot_name from data
        call_kwargs = mock_q.enqueue.call_args[1]
        assert call_kwargs["bot_name"] == "CustomBot"


class TestConstructMeetingUrl:
    """Tests for construct_meeting_url function."""

    def test_google_meet_url(self):
        from bot_spawn import construct_meeting_url

        url = construct_meeting_url("google_meet", "abc-defg-hij")
        assert url == "https://meet.google.com/abc-defg-hij"

    def test_zoom_url_without_passcode(self):
        from bot_spawn import construct_meeting_url

        url = construct_meeting_url("zoom", "12345678901")
        assert url == "https://zoom.us/j/12345678901"

    def test_zoom_url_with_passcode(self):
        from bot_spawn import construct_meeting_url

        url = construct_meeting_url("zoom", "12345678901", "secret123")
        assert url == "https://zoom.us/j/12345678901?pwd=secret123"

    def test_teams_returns_none(self):
        from bot_spawn import construct_meeting_url

        # Teams URLs are stored directly, not constructed
        url = construct_meeting_url("teams", "some-id")
        assert url is None

    def test_unknown_platform_returns_none(self):
        from bot_spawn import construct_meeting_url

        url = construct_meeting_url("unknown", "some-id")
        assert url is None
