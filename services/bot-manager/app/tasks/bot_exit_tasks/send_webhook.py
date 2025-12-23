import logging
import httpx
import hmac
import hashlib
import json
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from shared_models.models import Meeting, Account

logger = logging.getLogger(__name__)


def compute_signature(payload: str, secret: str) -> str:
    """Compute HMAC-SHA256 signature for webhook payload."""
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


async def run(meeting: Meeting, db: AsyncSession):
    """
    Sends a webhook with the completed meeting details to the account's webhook URL.
    """
    logger.info(f"Executing send_webhook task for meeting {meeting.id}")

    try:
        # Get account from meeting
        if not meeting.account_id:
            logger.warning(f"Meeting {meeting.id} has no account_id, skipping webhook")
            return

        account = await db.get(Account, meeting.account_id)
        if not account:
            logger.error(f"Could not find account {meeting.account_id} for meeting {meeting.id}")
            return

        # Check if account has a webhook URL configured
        if not account.webhook_url:
            logger.info(f"No webhook URL configured for account {account.id} ({account.name}), skipping")
            return

        # Prepare the webhook payload
        payload = {
            "event": "bot.ended",
            "timestamp": datetime.utcnow().isoformat(),
            "meeting": {
                "id": meeting.id,
                "account_id": meeting.account_id,
                "platform": meeting.platform,
                "native_meeting_id": meeting.native_meeting_id,
                "constructed_meeting_url": meeting.constructed_meeting_url,
                "status": meeting.status,
                "bot_container_id": meeting.bot_container_id,
                "start_time": meeting.start_time.isoformat() if meeting.start_time else None,
                "end_time": meeting.end_time.isoformat() if meeting.end_time else None,
                "data": meeting.data or {},
                "created_at": meeting.created_at.isoformat() if meeting.created_at else None,
                "updated_at": meeting.updated_at.isoformat() if meeting.updated_at else None,
            },
        }

        # Send the webhook
        try:
            payload_json = json.dumps(payload, default=str)

            headers = {
                "Content-Type": "application/json",
                "X-Vomeet-Event": "bot.ended",
                "X-Vomeet-Timestamp": datetime.utcnow().isoformat(),
            }

            # Add HMAC signature if secret is configured
            if account.webhook_secret:
                signature = compute_signature(payload_json, account.webhook_secret)
                headers["X-Vomeet-Signature"] = f"sha256={signature}"

            async with httpx.AsyncClient() as client:
                logger.info(f"Sending webhook to {account.webhook_url} for meeting {meeting.id}")
                response = await client.post(
                    account.webhook_url,
                    content=payload_json,
                    headers=headers,
                    timeout=30.0,
                )

                if 200 <= response.status_code < 300:
                    logger.info(f"Successfully sent webhook for meeting {meeting.id} to {account.webhook_url}")
                else:
                    logger.warning(
                        f"Webhook for meeting {meeting.id} returned status {response.status_code}: {response.text[:200]}"
                    )

        except httpx.RequestError as e:
            logger.error(f"Failed to send webhook for meeting {meeting.id}: {e}")

    except Exception as e:
        logger.error(f"Unexpected error sending webhook for meeting {meeting.id}: {e}", exc_info=True)
