import logging
import httpx
import hmac
import hashlib
import json
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from shared_models.models import Meeting, User, Webhook
from typing import Dict, Any, Optional, List

logger = logging.getLogger(__name__)


def compute_signature(payload: str, secret: str) -> str:
    """Compute HMAC-SHA256 signature for webhook payload."""
    return hmac.new(
        secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()


def get_event_type_from_status(status: str) -> str:
    """Map meeting status to webhook event type."""
    status_to_event = {
        "requested": "bot.requested",
        "joining": "bot.joining",
        "awaiting_admission": "bot.awaiting_admission",
        "active": "bot.active",
        "stopping": "bot.stopping",
        "completed": "bot.ended",
        "failed": "bot.failed",
        "error": "bot.failed",
    }
    return status_to_event.get(status, "meeting.status_change")


def should_send_webhook(webhook: Webhook, event_type: str) -> bool:
    """Check if webhook should receive this event type."""
    if not webhook.enabled:
        return False
    
    events = webhook.events or ["*"]
    
    # Wildcard matches all events
    if "*" in events:
        return True
    
    # Check for exact match
    if event_type in events:
        return True
    
    # Check for category match (e.g., "bot.*" matches "bot.active")
    event_category = event_type.split(".")[0] + ".*"
    if event_category in events:
        return True
    
    # Also match "meeting.status_change" for any bot status event
    if event_type.startswith("bot.") and "meeting.status_change" in events:
        return True
    
    return False


async def send_to_webhook(
    webhook: Webhook,
    payload: Dict[str, Any],
    event_type: str,
) -> bool:
    """Send payload to a single webhook endpoint."""
    try:
        payload_json = json.dumps(payload, default=str)
        
        headers = {
            "Content-Type": "application/json",
            "X-Vomeet-Event": event_type,
            "X-Vomeet-Timestamp": datetime.utcnow().isoformat(),
        }
        
        # Add HMAC signature if secret is configured
        if webhook.secret:
            signature = compute_signature(payload_json, webhook.secret)
            headers["X-Vomeet-Signature"] = f"sha256={signature}"
        
        async with httpx.AsyncClient() as client:
            response = await client.post(
                webhook.url,
                content=payload_json,
                headers=headers,
                timeout=30.0,
            )
            
            if 200 <= response.status_code < 300:
                logger.info(
                    f"Successfully sent webhook to {webhook.url} (event: {event_type})"
                )
                return True
            else:
                logger.warning(
                    f"Webhook to {webhook.url} returned status {response.status_code}: {response.text[:200]}"
                )
                return False
                
    except httpx.RequestError as e:
        logger.error(f"Failed to send webhook to {webhook.url}: {e}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error sending webhook to {webhook.url}: {e}", exc_info=True)
        return False


async def run(
    meeting: Meeting,
    db: AsyncSession,
    status_change_info: Optional[Dict[str, Any]] = None,
):
    """
    Sends webhooks for meeting status changes to all configured webhook endpoints.

    Args:
        meeting: Meeting object with current status
        db: Database session
        status_change_info: Optional dict containing status change details like:
            - old_status: Previous status
            - new_status: Current status
            - reason: Reason for change
            - timestamp: When change occurred
    """
    logger.info(
        f"Executing send_status_webhook task for meeting {meeting.id} with status {meeting.status}"
    )

    try:
        user = meeting.user
        if not user:
            logger.error(f"Could not find user on meeting object {meeting.id}")
            return

        # Get the event type based on current status
        event_type = get_event_type_from_status(meeting.status)

        # Prepare the webhook payload
        payload = {
            "event": event_type,
            "timestamp": datetime.utcnow().isoformat(),
            "data": {
                "old_status": status_change_info.get("old_status") if status_change_info else None,
                "new_status": meeting.status,
                "reason": status_change_info.get("reason") if status_change_info else None,
                "transition_source": status_change_info.get("transition_source") if status_change_info else None,
            },
            "meeting": {
                "id": meeting.id,
                "user_id": meeting.user_id,
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

        # Query all webhooks for this user
        result = await db.execute(
            select(Webhook).where(Webhook.user_id == user.id)
        )
        webhooks: List[Webhook] = result.scalars().all()

        # Also check legacy webhook_url in user.data for backwards compatibility
        legacy_webhook_url = (
            user.data.get("webhook_url")
            if user.data and isinstance(user.data, dict)
            else None
        )

        if not webhooks and not legacy_webhook_url:
            logger.info(
                f"No webhooks configured for user {user.email} (meeting {meeting.id})"
            )
            return

        # Send to all matching webhooks
        sent_count = 0
        
        for webhook in webhooks:
            if should_send_webhook(webhook, event_type):
                success = await send_to_webhook(webhook, payload, event_type)
                if success:
                    sent_count += 1

        # Send to legacy webhook URL (always sends for backwards compatibility)
        if legacy_webhook_url:
            try:
                async with httpx.AsyncClient() as client:
                    # Legacy format for backwards compatibility
                    legacy_payload = {
                        "event_type": "meeting.status_change",
                        "meeting": payload["meeting"],
                    }
                    if status_change_info:
                        legacy_payload["status_change"] = {
                            "from": status_change_info.get("old_status"),
                            "to": status_change_info.get("new_status", meeting.status),
                            "reason": status_change_info.get("reason"),
                            "timestamp": status_change_info.get("timestamp"),
                            "transition_source": status_change_info.get("transition_source"),
                        }
                    
                    response = await client.post(
                        legacy_webhook_url,
                        json=legacy_payload,
                        timeout=30.0,
                        headers={"Content-Type": "application/json"},
                    )
                    
                    if 200 <= response.status_code < 300:
                        logger.info(
                            f"Successfully sent legacy webhook for meeting {meeting.id} to {legacy_webhook_url}"
                        )
                        sent_count += 1
                    else:
                        logger.warning(
                            f"Legacy webhook returned status {response.status_code}: {response.text[:200]}"
                        )
            except Exception as e:
                logger.error(f"Failed to send legacy webhook: {e}")

        logger.info(
            f"Sent {sent_count} webhook(s) for meeting {meeting.id} (event: {event_type})"
        )

    except Exception as e:
        logger.error(
            f"Unexpected error in webhook task for meeting {meeting.id}: {e}",
            exc_info=True,
        )
