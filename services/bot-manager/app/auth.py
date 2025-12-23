from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
import logging

from shared_models.models import Account
from shared_models.database import get_db

logger = logging.getLogger("bot_manager.auth")

API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)


async def get_account_from_api_key(
    api_key: str = Security(API_KEY_HEADER), db: AsyncSession = Depends(get_db)
) -> Account:
    """
    Dependency to verify X-API-Key as an Account API key (B2B flow).
    This is the authentication method for all API integrations.
    """
    if not api_key:
        logger.warning("API key missing from header")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Missing API key (X-API-Key header)",
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
