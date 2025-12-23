import logging
from fastapi import Depends, HTTPException, Security, status
from fastapi.security.api_key import APIKeyHeader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# Relative import for API_KEY_NAME from the service's config.py
from config import API_KEY_NAME

# Imports from shared libraries
from shared_models.database import get_db
from shared_models.models import Account

logger = logging.getLogger(__name__)

api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)


async def get_account_from_api_key(
    api_key: str = Security(api_key_header), db: AsyncSession = Depends(get_db)
) -> Account:
    """
    Dependency to verify X-API-Key as an Account API key (B2B flow).
    This is the authentication method for all API integrations.
    """
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Missing API key",
        )

    result = await db.execute(select(Account).where(Account.api_key == api_key, Account.enabled.is_(True)))
    account = result.scalar_one_or_none()

    if account:
        logger.info(f"Account API key validated for account {account.id} ({account.name})")
        return account

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Invalid API key",
    )
