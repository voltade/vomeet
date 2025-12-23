# Webhook Payload Schema

## Headers

| Header | Description |
|--------|-------------|
| `Content-Type` | `application/json` |
| `X-Vomeet-Event` | Event type (e.g., `bot.active`, `bot.ended`) |
| `X-Vomeet-Timestamp` | ISO 8601 timestamp |
| `X-Vomeet-Signature` | `sha256={hmac}` (if webhook_secret is configured) |

## Event Types

```
bot.requested
bot.joining
bot.awaiting_admission
bot.active
bot.stopping
bot.ended
bot.failed
transcript.ready
transcript.segment
```

## Payload Structure

```json
{
  "event": "bot.active",
  "timestamp": "2025-12-23T10:30:00.000000",
  "data": {
    "old_status": "joining",
    "new_status": "active",
    "reason": null,
    "transition_source": null
  },
  "meeting": {
    "id": 123,
    "account_id": 1,
    "platform": "google_meet",
    "native_meeting_id": "abc-defg-hij",
    "constructed_meeting_url": "https://meet.google.com/abc-defg-hij",
    "status": "active",
    "bot_container_id": "bot-xxx-yyy",
    "start_time": "2025-12-23T10:29:00.000000",
    "end_time": null,
    "data": {},
    "created_at": "2025-12-23T10:28:00.000000",
    "updated_at": "2025-12-23T10:30:00.000000"
  }
}
```

## Signature Verification

If you set `webhook_secret` on your account, verify the signature:

```python
import hmac
import hashlib

def verify_signature(payload: str, secret: str, signature_header: str) -> bool:
    expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    received = signature_header.replace("sha256=", "")
    return hmac.compare_digest(expected, received)
```
