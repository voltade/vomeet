"""
Webhook utility functions for sending notifications.
"""

import hmac
import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any

import httpx

logger = logging.getLogger(__name__)


def compute_signature(payload: str, secret: str) -> str:
    """Compute HMAC-SHA256 signature for webhook payload."""
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def send_webhook(
    webhook_url: str,
    webhook_secret: Optional[str],
    event_type: str,
    payload: Dict[str, Any],
) -> bool:
    """
    Send a webhook notification.

    Args:
        webhook_url: The URL to send the webhook to
        webhook_secret: Optional HMAC secret for signing the payload
        event_type: The event type (e.g., 'meeting.created')
        payload: The payload to send

    Returns:
        True if the webhook was sent successfully, False otherwise
    """
    try:
        payload_json = json.dumps(payload, default=str)

        headers = {
            "Content-Type": "application/json",
            "X-Vomeet-Event": event_type,
            "X-Vomeet-Timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # Add HMAC signature if secret is configured
        if webhook_secret:
            signature = compute_signature(payload_json, webhook_secret)
            headers["X-Vomeet-Signature"] = f"sha256={signature}"

        with httpx.Client() as client:
            response = client.post(
                webhook_url,
                content=payload_json,
                headers=headers,
                timeout=30.0,
            )

            if 200 <= response.status_code < 300:
                logger.info(f"Successfully sent {event_type} webhook to {webhook_url}")
                return True
            else:
                logger.warning(f"{event_type} webhook to {webhook_url} returned status {response.status_code}")
                return False

    except httpx.RequestError as e:
        logger.error(f"Failed to send {event_type} webhook to {webhook_url}: {e}")
        return False
