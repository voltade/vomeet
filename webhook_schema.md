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

## Event Order

When a meeting ends, webhooks are sent in this order:

1. **`bot.ended`** - Sent immediately when bot exits the meeting
2. **`transcript.ready`** - Sent after transcript is finalized with full transcript data

## Payload Structure

### Bot Events (bot.*)

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

### transcript.ready Event

Sent after the meeting ends with the complete transcript:

```json
{
  "event": "transcript.ready",
  "timestamp": "2025-12-23T10:45:00.000000",
  "meeting": {
    "id": 123,
    "account_id": 1,
    "platform": "google_meet",
    "native_meeting_id": "abc-defg-hij",
    "constructed_meeting_url": "https://meet.google.com/abc-defg-hij",
    "status": "completed",
    "start_time": "2025-12-23T10:00:00.000000",
    "end_time": "2025-12-23T10:44:00.000000",
    "data": {
      "participants": ["Alice", "Bob"],
      "languages": ["en"]
    }
  },
  "transcript": {
    "segment_count": 42,
    "segments": [
      {
        "speaker": "Alice",
        "text": "Hello everyone, thanks for joining.",
        "timestamp": "2025-12-23T10:00:05.000000",
        "language": "en"
      },
      {
        "speaker": "Bob", 
        "text": "Hi Alice, glad to be here.",
        "timestamp": "2025-12-23T10:00:12.000000",
        "language": "en"
      }
    ],
    "full_text": "Alice: Hello everyone, thanks for joining.\nBob: Hi Alice, glad to be here.",
    "participants": ["Alice", "Bob"],
    "languages": ["en"]
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
