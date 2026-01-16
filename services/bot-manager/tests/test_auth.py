"""
Tests for bot-manager auth module.
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from fastapi import HTTPException


class TestGetAccountFromApiKey:
    """Tests for the get_account_from_api_key dependency."""

    @pytest.fixture
    def mock_db(self):
        """Create a mock async database session."""
        return AsyncMock()

    @pytest.mark.asyncio
    async def test_returns_account_for_valid_key(self, mock_db):
        """Should return account when valid API key is provided."""
        from app.auth import get_account_from_api_key

        mock_account = MagicMock()
        mock_account.id = 1
        mock_account.name = "Test Account"
        mock_account.enabled = True

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_account
        mock_db.execute.return_value = mock_result

        account = await get_account_from_api_key(api_key="valid_key", db=mock_db)

        assert account == mock_account
        assert account.id == 1
        mock_db.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_raises_403_for_missing_key(self, mock_db):
        """Should raise 403 when API key is missing."""
        from app.auth import get_account_from_api_key

        with pytest.raises(HTTPException) as exc_info:
            await get_account_from_api_key(api_key=None, db=mock_db)

        assert exc_info.value.status_code == 403
        assert "Missing API key" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_raises_403_for_empty_key(self, mock_db):
        """Should raise 403 when API key is empty string."""
        from app.auth import get_account_from_api_key

        with pytest.raises(HTTPException) as exc_info:
            await get_account_from_api_key(api_key="", db=mock_db)

        assert exc_info.value.status_code == 403
        assert "Missing API key" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_raises_403_for_invalid_key(self, mock_db):
        """Should raise 403 when API key is not found in database."""
        from app.auth import get_account_from_api_key

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        with pytest.raises(HTTPException) as exc_info:
            await get_account_from_api_key(api_key="invalid_key", db=mock_db)

        assert exc_info.value.status_code == 403
        assert "Invalid API key" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_raises_403_for_disabled_account(self, mock_db):
        """Should raise 403 when account is disabled."""
        from app.auth import get_account_from_api_key

        # Query filters by enabled=True, so disabled account returns None
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        with pytest.raises(HTTPException) as exc_info:
            await get_account_from_api_key(api_key="disabled_account_key", db=mock_db)

        assert exc_info.value.status_code == 403
        assert "Invalid API key" in exc_info.value.detail
