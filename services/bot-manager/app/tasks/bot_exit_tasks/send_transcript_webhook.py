"""
Send transcript.ready webhook after meeting ends with full transcript data.

This task runs after the bot.ended webhook and aggregation tasks.
It waits 1 minute after bot exit to ensure transcript is fully processed,
then fetches the complete transcript and sends it to the account's webhook URL.
"""

import asyncio
import logging
import httpx
import hmac
import hashlib
import json
import os
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from shared_models.models import Meeting, Account

logger = logging.getLogger(__name__)

# Transcription collector service URL (use K8s service name in production)
TRANSCRIPTION_COLLECTOR_URL = os.getenv("TRANSCRIPTION_COLLECTOR_URL", "http://vomeet-transcription-collector:8000")

# Priority: lower runs first. Runs last after bot.ended (20) so transcript is fully processed.
PRIORITY = 30

# Delay in seconds before sending transcript webhook (allows aggregation to complete)
TRANSCRIPT_DELAY_SECONDS = 30


def compute_signature(payload: str, secret: str) -> str:
    """Compute HMAC-SHA256 signature for webhook payload."""
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


async def run(meeting: Meeting, db: AsyncSession):
    """
    Sends a transcript.ready webhook with the complete transcript data.

    This runs after aggregate_transcription.py and send_webhook.py (bot.ended).
    Waits 1 minute to ensure transcript aggregation is complete.
    """
    logger.info(f"Executing send_transcript_webhook task for meeting {meeting.id}")
    logger.info(f"Waiting {TRANSCRIPT_DELAY_SECONDS} seconds before sending transcript webhook...")

    await asyncio.sleep(TRANSCRIPT_DELAY_SECONDS)

    logger.info(f"Delay complete, proceeding to send transcript webhook for meeting {meeting.id}")

    try:
        # Get account from meeting
        if not meeting.account_id:
            logger.warning(f"Meeting {meeting.id} has no account_id, skipping transcript webhook")
            return

        account = await db.get(Account, meeting.account_id)
        if not account:
            logger.error(f"Could not find account {meeting.account_id} for meeting {meeting.id}")
            return

        # Check if account has a webhook URL configured
        if not account.webhook_url:
            logger.info(f"No webhook URL configured for account {account.id} ({account.name}), skipping")
            return

        # Fetch transcript from transcription-collector
        collector_url = f"{TRANSCRIPTION_COLLECTOR_URL}/internal/transcripts/{meeting.id}"

        transcript_segments = []
        try:
            async with httpx.AsyncClient() as client:
                logger.info(f"Fetching transcript for meeting {meeting.id} from collector")
                response = await client.get(collector_url, timeout=30.0)

                if response.status_code == 200:
                    transcript_segments = response.json()
                    logger.info(f"Fetched {len(transcript_segments)} transcript segments for meeting {meeting.id}")
                else:
                    logger.warning(f"Failed to fetch transcript for meeting {meeting.id}: {response.status_code}")
        except httpx.RequestError as e:
            logger.error(f"Failed to fetch transcript for meeting {meeting.id}: {e}")

        # Build full transcript text
        full_transcript = ""
        for segment in transcript_segments:
            speaker = segment.get("speaker", "Unknown")
            text = segment.get("text", "")
            if text:
                full_transcript += f"{speaker}: {text}\n"

        # Prepare the webhook payload
        payload = {
            "event": "transcript.ready",
            "timestamp": datetime.utcnow().isoformat(),
            "meeting": {
                "id": meeting.id,
                "account_id": meeting.account_id,
                "platform": meeting.platform,
                "native_meeting_id": meeting.native_meeting_id,
                "constructed_meeting_url": meeting.constructed_meeting_url,
                "status": meeting.status,
                "start_time": meeting.start_time.isoformat() if meeting.start_time else None,
                "end_time": meeting.end_time.isoformat() if meeting.end_time else None,
                "data": meeting.data or {},
            },
            "transcript": {
                "segment_count": len(transcript_segments),
                "segments": transcript_segments,
                "full_text": full_transcript.strip(),
                "participants": (meeting.data or {}).get("participants", []),
                "languages": (meeting.data or {}).get("languages", []),
            },
        }

        # Send the webhook
        try:
            payload_json = json.dumps(payload, default=str)

            headers = {
                "Content-Type": "application/json",
                "X-Vomeet-Event": "transcript.ready",
                "X-Vomeet-Timestamp": datetime.utcnow().isoformat(),
            }

            # Add HMAC signature if secret is configured
            if account.webhook_secret:
                signature = compute_signature(payload_json, account.webhook_secret)
                headers["X-Vomeet-Signature"] = f"sha256={signature}"

            async with httpx.AsyncClient() as client:
                logger.info(f"Sending transcript.ready webhook to {account.webhook_url} for meeting {meeting.id}")
                response = await client.post(
                    account.webhook_url,
                    content=payload_json,
                    headers=headers,
                    timeout=60.0,  # Longer timeout for potentially large payloads
                )

                if 200 <= response.status_code < 300:
                    logger.info(
                        f"Successfully sent transcript.ready webhook for meeting {meeting.id} "
                        f"({len(transcript_segments)} segments)"
                    )
                else:
                    logger.warning(
                        f"transcript.ready webhook for meeting {meeting.id} returned status "
                        f"{response.status_code}: {response.text[:200]}"
                    )

        except httpx.RequestError as e:
            logger.error(f"Failed to send transcript.ready webhook for meeting {meeting.id}: {e}")

    except Exception as e:
        logger.error(f"Unexpected error sending transcript webhook for meeting {meeting.id}: {e}", exc_info=True)
