"""
Tests for bot-manager main module.
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch, Mock
from datetime import datetime, timezone, timedelta


class TestUpdateMeetingStatus:
    """Tests for the update_meeting_status function."""

    @pytest.fixture
    def mock_meeting(self):
        """Create a mock meeting object."""
        meeting = MagicMock()
        meeting.id = 1
        meeting.status = "requested"
        meeting.scheduled_meeting_id = 100
        meeting.data = {}
        return meeting

    @pytest.fixture
    def mock_db(self):
        """Create a mock async database session."""
        db = AsyncMock()
        return db

    @pytest.mark.asyncio
    async def test_valid_transition_from_requested_to_joining(self, mock_meeting, mock_db):
        """Should allow transition from requested to joining."""
        from app.main import update_meeting_status
        from shared_models.schemas import MeetingStatus

        mock_meeting.status = "requested"

        with patch("app.main._sync_scheduled_meeting_status", new_callable=AsyncMock):
            result = await update_meeting_status(mock_meeting, MeetingStatus.JOINING, mock_db)

        assert result is True
        assert mock_meeting.status == MeetingStatus.JOINING.value
        mock_db.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_valid_transition_from_active_to_completed(self, mock_meeting, mock_db):
        """Should allow transition from active to completed."""
        from app.main import update_meeting_status
        from shared_models.schemas import MeetingStatus, MeetingCompletionReason

        mock_meeting.status = "active"

        with patch("app.main._sync_scheduled_meeting_status", new_callable=AsyncMock):
            result = await update_meeting_status(
                mock_meeting,
                MeetingStatus.COMPLETED,
                mock_db,
                completion_reason=MeetingCompletionReason.STOPPED,
            )

        assert result is True
        assert mock_meeting.status == MeetingStatus.COMPLETED.value
        assert mock_meeting.end_time is not None
        assert mock_meeting.data.get("completion_reason") == "stopped"

    @pytest.mark.asyncio
    async def test_valid_transition_from_active_to_failed(self, mock_meeting, mock_db):
        """Should allow transition from active to failed."""
        from app.main import update_meeting_status
        from shared_models.schemas import MeetingStatus, MeetingFailureStage

        mock_meeting.status = "active"

        with patch("app.main._sync_scheduled_meeting_status", new_callable=AsyncMock):
            result = await update_meeting_status(
                mock_meeting,
                MeetingStatus.FAILED,
                mock_db,
                failure_stage=MeetingFailureStage.ACTIVE,
                error_details="Connection lost",
            )

        assert result is True
        assert mock_meeting.status == MeetingStatus.FAILED.value
        assert mock_meeting.end_time is not None
        assert mock_meeting.data.get("failure_stage") == "active"
        assert mock_meeting.data.get("error_details") == "Connection lost"

    @pytest.mark.asyncio
    async def test_invalid_transition_from_completed_to_active(self, mock_meeting, mock_db):
        """Should reject invalid transition from terminal state."""
        from app.main import update_meeting_status
        from shared_models.schemas import MeetingStatus

        mock_meeting.status = "completed"

        result = await update_meeting_status(mock_meeting, MeetingStatus.ACTIVE, mock_db)

        assert result is False
        mock_db.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_idempotent_same_status_transition(self, mock_meeting, mock_db):
        """Should allow same-status transition (idempotent)."""
        from app.main import update_meeting_status
        from shared_models.schemas import MeetingStatus

        mock_meeting.status = "active"

        result = await update_meeting_status(mock_meeting, MeetingStatus.ACTIVE, mock_db)

        assert result is True
        # Should not commit since no actual change
        mock_db.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_adds_status_transition_metadata(self, mock_meeting, mock_db):
        """Should add status transition metadata to data field."""
        from app.main import update_meeting_status
        from shared_models.schemas import MeetingStatus

        mock_meeting.status = "requested"
        mock_meeting.data = {}

        with patch("app.main._sync_scheduled_meeting_status", new_callable=AsyncMock):
            await update_meeting_status(
                mock_meeting,
                MeetingStatus.JOINING,
                mock_db,
                transition_reason="bot_started",
            )

        # Should have status_transition list in data
        assert "status_transition" in mock_meeting.data
        transitions = mock_meeting.data["status_transition"]
        assert len(transitions) == 1
        assert transitions[0]["from"] == "requested"
        assert transitions[0]["to"] == "joining"
        assert transitions[0]["reason"] == "bot_started"

    @pytest.mark.asyncio
    async def test_calls_sync_scheduled_meeting_status(self, mock_meeting, mock_db):
        """Should call _sync_scheduled_meeting_status for atomic update."""
        from app.main import update_meeting_status
        from shared_models.schemas import MeetingStatus

        mock_meeting.status = "active"

        with patch("app.main._sync_scheduled_meeting_status", new_callable=AsyncMock) as mock_sync:
            await update_meeting_status(mock_meeting, MeetingStatus.COMPLETED, mock_db)

        mock_sync.assert_called_once_with(mock_meeting, MeetingStatus.COMPLETED, mock_db)


class TestSyncScheduledMeetingStatus:
    """Tests for the _sync_scheduled_meeting_status function."""

    @pytest.fixture
    def mock_scheduled_meeting(self):
        """Create a mock scheduled meeting."""
        scheduled = MagicMock()
        scheduled.id = 100
        scheduled.status = "bot_active"
        return scheduled

    @pytest.mark.asyncio
    async def test_maps_active_to_bot_active(self):
        """Should map meeting ACTIVE to scheduled_meeting BOT_ACTIVE."""
        from app.main import _sync_scheduled_meeting_status
        from shared_models.schemas import MeetingStatus
        from shared_models.models import ScheduledMeetingStatus

        mock_meeting = MagicMock()
        mock_meeting.scheduled_meeting_id = 100

        mock_scheduled = MagicMock()
        mock_scheduled.id = 100
        mock_scheduled.status = "bot_requested"

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_scheduled
        mock_db.execute.return_value = mock_result

        await _sync_scheduled_meeting_status(mock_meeting, MeetingStatus.ACTIVE, mock_db)

        assert mock_scheduled.status == ScheduledMeetingStatus.BOT_ACTIVE.value

    @pytest.mark.asyncio
    async def test_maps_completed_to_completed(self):
        """Should map meeting COMPLETED to scheduled_meeting COMPLETED."""
        from app.main import _sync_scheduled_meeting_status
        from shared_models.schemas import MeetingStatus
        from shared_models.models import ScheduledMeetingStatus

        mock_meeting = MagicMock()
        mock_meeting.scheduled_meeting_id = 100

        mock_scheduled = MagicMock()
        mock_scheduled.id = 100
        mock_scheduled.status = "bot_active"

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_scheduled
        mock_db.execute.return_value = mock_result

        await _sync_scheduled_meeting_status(mock_meeting, MeetingStatus.COMPLETED, mock_db)

        assert mock_scheduled.status == ScheduledMeetingStatus.COMPLETED.value

    @pytest.mark.asyncio
    async def test_maps_failed_to_completed(self):
        """Should map meeting FAILED to scheduled_meeting COMPLETED (allows rescheduling)."""
        from app.main import _sync_scheduled_meeting_status
        from shared_models.schemas import MeetingStatus
        from shared_models.models import ScheduledMeetingStatus

        mock_meeting = MagicMock()
        mock_meeting.scheduled_meeting_id = 100

        mock_scheduled = MagicMock()
        mock_scheduled.id = 100
        mock_scheduled.status = "bot_active"

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_scheduled
        mock_db.execute.return_value = mock_result

        await _sync_scheduled_meeting_status(mock_meeting, MeetingStatus.FAILED, mock_db)

        assert mock_scheduled.status == ScheduledMeetingStatus.COMPLETED.value

    @pytest.mark.asyncio
    async def test_no_update_for_joining_status(self):
        """Should not update scheduled_meeting for JOINING status."""
        from app.main import _sync_scheduled_meeting_status
        from shared_models.schemas import MeetingStatus

        mock_meeting = MagicMock()
        mock_meeting.scheduled_meeting_id = 100

        mock_db = AsyncMock()

        await _sync_scheduled_meeting_status(mock_meeting, MeetingStatus.JOINING, mock_db)

        # Should not query the database for non-mapped statuses
        mock_db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_missing_scheduled_meeting_id(self):
        """Should handle meetings without scheduled_meeting_id (legacy ad-hoc)."""
        from app.main import _sync_scheduled_meeting_status
        from shared_models.schemas import MeetingStatus

        mock_meeting = MagicMock()
        mock_meeting.scheduled_meeting_id = None

        mock_db = AsyncMock()

        # Should not raise
        await _sync_scheduled_meeting_status(mock_meeting, MeetingStatus.ACTIVE, mock_db)

        mock_db.execute.assert_not_called()


class TestMintMeetingToken:
    """Tests for the mint_meeting_token function."""

    @patch.dict("os.environ", {"ADMIN_TOKEN": "test_secret_key"})
    def test_generates_valid_jwt_structure(self):
        """Should generate a token with header.payload.signature structure."""
        from app.main import mint_meeting_token

        token = mint_meeting_token(
            meeting_id=1,
            user_id=100,
            platform="google_meet",
            native_meeting_id="abc-defg-hij",
        )

        parts = token.split(".")
        assert len(parts) == 3  # header.payload.signature

    @patch.dict("os.environ", {"ADMIN_TOKEN": "test_secret_key"})
    def test_includes_required_claims(self):
        """Should include all required JWT claims."""
        import json
        import base64
        from app.main import mint_meeting_token

        token = mint_meeting_token(
            meeting_id=1,
            user_id=100,
            platform="google_meet",
            native_meeting_id="abc-defg-hij",
        )

        # Decode payload (add padding for base64)
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))

        assert payload["meeting_id"] == 1
        assert payload["user_id"] == 100
        assert payload["platform"] == "google_meet"
        assert payload["native_meeting_id"] == "abc-defg-hij"
        assert payload["scope"] == "transcribe:write"
        assert payload["iss"] == "bot-manager"
        assert payload["aud"] == "transcription-collector"

    @patch.dict("os.environ", {}, clear=True)
    def test_raises_without_admin_token(self):
        """Should raise ValueError when ADMIN_TOKEN not configured."""
        from app.main import mint_meeting_token

        with pytest.raises(ValueError, match="ADMIN_TOKEN not configured"):
            mint_meeting_token(
                meeting_id=1,
                user_id=100,
                platform="google_meet",
                native_meeting_id="abc-defg-hij",
            )


class TestStatusTransitions:
    """Tests for status transition validation."""

    def test_requested_can_transition_to_joining(self):
        """REQUESTED can transition to JOINING."""
        from shared_models.schemas import MeetingStatus, is_valid_status_transition

        assert is_valid_status_transition(MeetingStatus.REQUESTED, MeetingStatus.JOINING) is True

    def test_requested_can_transition_to_failed(self):
        """REQUESTED can transition to FAILED."""
        from shared_models.schemas import MeetingStatus, is_valid_status_transition

        assert is_valid_status_transition(MeetingStatus.REQUESTED, MeetingStatus.FAILED) is True

    def test_joining_can_transition_to_awaiting_admission(self):
        """JOINING can transition to AWAITING_ADMISSION."""
        from shared_models.schemas import MeetingStatus, is_valid_status_transition

        assert is_valid_status_transition(MeetingStatus.JOINING, MeetingStatus.AWAITING_ADMISSION) is True

    def test_awaiting_admission_can_transition_to_active(self):
        """AWAITING_ADMISSION can transition to ACTIVE."""
        from shared_models.schemas import MeetingStatus, is_valid_status_transition

        assert is_valid_status_transition(MeetingStatus.AWAITING_ADMISSION, MeetingStatus.ACTIVE) is True

    def test_active_can_transition_to_completed(self):
        """ACTIVE can transition to COMPLETED."""
        from shared_models.schemas import MeetingStatus, is_valid_status_transition

        assert is_valid_status_transition(MeetingStatus.ACTIVE, MeetingStatus.COMPLETED) is True

    def test_active_can_transition_to_failed(self):
        """ACTIVE can transition to FAILED."""
        from shared_models.schemas import MeetingStatus, is_valid_status_transition

        assert is_valid_status_transition(MeetingStatus.ACTIVE, MeetingStatus.FAILED) is True

    def test_completed_cannot_transition(self):
        """COMPLETED is terminal and cannot transition."""
        from shared_models.schemas import MeetingStatus, is_valid_status_transition

        assert is_valid_status_transition(MeetingStatus.COMPLETED, MeetingStatus.ACTIVE) is False
        assert is_valid_status_transition(MeetingStatus.COMPLETED, MeetingStatus.FAILED) is False

    def test_failed_cannot_transition(self):
        """FAILED is terminal and cannot transition."""
        from shared_models.schemas import MeetingStatus, is_valid_status_transition

        assert is_valid_status_transition(MeetingStatus.FAILED, MeetingStatus.ACTIVE) is False
        assert is_valid_status_transition(MeetingStatus.FAILED, MeetingStatus.COMPLETED) is False


class TestPublishMeetingStatusChange:
    """Tests for the publish_meeting_status_change function."""

    @pytest.mark.asyncio
    async def test_publishes_to_correct_channel(self):
        """Should publish to correct Redis channel."""
        from app.main import publish_meeting_status_change

        mock_redis = AsyncMock()

        with patch("app.main._update_scheduled_meeting_status_fallback", new_callable=AsyncMock):
            await publish_meeting_status_change(
                meeting_id=1,
                new_status="active",
                redis_client=mock_redis,
                platform="google_meet",
                native_meeting_id="abc-defg-hij",
                user_id=100,
            )

        mock_redis.publish.assert_called_once()
        call_args = mock_redis.publish.call_args
        assert call_args[0][0] == "bm:meeting:1:status"  # Channel name

    @pytest.mark.asyncio
    async def test_handles_missing_redis_client(self):
        """Should handle None redis client gracefully."""
        from app.main import publish_meeting_status_change

        # Should not raise
        await publish_meeting_status_change(
            meeting_id=1,
            new_status="active",
            redis_client=None,
            platform="google_meet",
            native_meeting_id="abc-defg-hij",
            user_id=100,
        )

    @pytest.mark.asyncio
    async def test_calls_fallback_status_update(self):
        """Should call _update_scheduled_meeting_status_fallback."""
        from app.main import publish_meeting_status_change

        mock_redis = AsyncMock()

        with patch("app.main._update_scheduled_meeting_status_fallback", new_callable=AsyncMock) as mock_fallback:
            with patch("asyncio.create_task") as mock_create_task:
                await publish_meeting_status_change(
                    meeting_id=1,
                    new_status="active",
                    redis_client=mock_redis,
                    platform="google_meet",
                    native_meeting_id="abc-defg-hij",
                    user_id=100,
                )

            mock_create_task.assert_called_once()


class TestUpdateScheduledMeetingStatusFallback:
    """Tests for the _update_scheduled_meeting_status_fallback function."""

    @pytest.mark.asyncio
    async def test_updates_scheduled_meeting_on_active(self):
        """Should update scheduled_meeting status when meeting becomes active."""
        from app.main import _update_scheduled_meeting_status_fallback
        from shared_models.models import ScheduledMeetingStatus

        mock_meeting = MagicMock()
        mock_meeting.id = 1
        mock_meeting.scheduled_meeting_id = 100

        mock_scheduled = MagicMock()
        mock_scheduled.id = 100
        mock_scheduled.status = "bot_requested"

        mock_db = AsyncMock()
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=None)

        # First execute returns meeting
        # Second execute returns scheduled meeting
        mock_db.execute.side_effect = [
            MagicMock(scalar_one_or_none=MagicMock(return_value=mock_meeting)),
            MagicMock(scalar_one_or_none=MagicMock(return_value=mock_scheduled)),
        ]

        with patch("app.main.async_session_local", return_value=mock_db):
            await _update_scheduled_meeting_status_fallback(1, "active")

        assert mock_scheduled.status == ScheduledMeetingStatus.BOT_ACTIVE.value
        mock_db.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_unmapped_statuses(self):
        """Should skip statuses without mapping (e.g., joining)."""
        from app.main import _update_scheduled_meeting_status_fallback

        mock_db = AsyncMock()
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=None)

        with patch("app.main.async_session_local", return_value=mock_db):
            await _update_scheduled_meeting_status_fallback(1, "joining")

        # Should not query database for unmapped status
        mock_db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_missing_meeting(self):
        """Should handle case when meeting not found."""
        from app.main import _update_scheduled_meeting_status_fallback

        mock_db = AsyncMock()
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=None)
        mock_db.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=None))

        with patch("app.main.async_session_local", return_value=mock_db):
            # Should not raise
            await _update_scheduled_meeting_status_fallback(1, "active")

        mock_db.commit.assert_not_called()
