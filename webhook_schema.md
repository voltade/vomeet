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
meeting.created
meeting.updated
meeting.rescheduled
meeting.cancelled
transcript.ready
transcript.segment
```

## Event Order

When a meeting ends, webhooks are sent in this order:

1. **`bot.ended`** - Sent immediately when bot exits the meeting
2. **`transcript.ready`** - Sent after transcript is finalized with full transcript data

For auto-joined meetings from calendar integration:

1. **`meeting.created`** - Sent when bot is spawned for a calendar event
2. **`meeting.rescheduled`** - Sent if the same calendar event is rescheduled to a new time

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

### meeting.created Event (Calendar Auto-Join)

Sent when a bot is automatically spawned for a calendar event:

```json
{
  "event": "meeting.created",
  "timestamp": "2025-12-23T09:58:00.000000",
  "meeting": {
    "id": 123,
    "bot_id": 123,
    "platform": "google_meet",
    "native_meeting_id": "abc-defg-hij",
    "meeting_url": "https://meet.google.com/abc-defg-hij",
    "status": "requested",
    "created_at": "2025-12-23T09:58:00.000000"
  },
  "calendar_event": {
    "event_id": "google_calendar_event_id_123",
    "title": "Team Standup",
    "scheduled_at": "2025-12-23T10:00:00+00:00",
    "is_creator_self": true,
    "is_organizer_self": true,
    "attendees": [
      {
        "email": "alice@example.com",
        "name": "Alice",
        "response_status": "accepted",
        "is_organizer": true,
        "is_self": true
      },
      {
        "email": "bob@example.com",
        "name": "Bob",
        "response_status": "accepted",
        "is_organizer": false,
        "is_self": false
      }
    ]
  },
  "user": {
    "external_user_id": "user-123",
    "account_user_id": 5,
    "account_id": 1
  }
}
```

### meeting.rescheduled Event

Sent when a calendar event is rescheduled and the bot joins at the new time:

```json
{
  "event": "meeting.rescheduled",
  "timestamp": "2025-12-23T10:58:00.000000",
  "meeting": {
    "id": 124,
    "bot_id": 124,
    "platform": "google_meet",
    "native_meeting_id": "abc-defg-hij",
    "meeting_url": "https://meet.google.com/abc-defg-hij",
    "status": "requested",
    "created_at": "2025-12-23T10:58:00.000000"
  },
  "calendar_event": {
    "event_id": "google_calendar_event_id_123",
    "title": "Team Standup",
    "scheduled_at": "2025-12-23T11:00:00+00:00",
    "previous_scheduled_at": "2025-12-23T10:00:00+00:00",
    "is_creator_self": true,
    "is_organizer_self": true,
    "attendees": [...]
  },
  "user": {
    "external_user_id": "user-123",
    "account_user_id": 5,
    "account_id": 1
  }
}
```

### meeting.updated Event (Calendar Sync)

Sent when a calendar event is updated (e.g., title, description, attendees changed):

```json
{
  "event": "meeting.updated",
  "timestamp": "2025-12-23T09:30:00.000000",
  "calendar_event": {
    "id": 456,
    "calendar_event_id": "google_calendar_event_id_123",
    "calendar_provider": "google",
    "title": "Team Standup - Updated",
    "description": "Weekly sync meeting",
    "platform": "google_meet",
    "native_meeting_id": "abc-defg-hij",
    "meeting_url": "https://meet.google.com/abc-defg-hij",
    "scheduled_start_time": "2025-12-23T10:00:00+00:00",
    "scheduled_end_time": "2025-12-23T10:30:00+00:00",
    "is_creator_self": true,
    "is_organizer_self": true,
    "status": "scheduled",
    "attendees": [...],
    "bot_id": 123
  },
  "changes": {
    "title": {
      "old": "Team Standup",
      "new": "Team Standup - Updated"
    }
  },
  "user": {
    "external_user_id": "user-123",
    "account_user_id": 5,
    "account_id": 1
  }
}
```

### meeting.cancelled Event (Calendar Sync)

Sent when a calendar event is cancelled or deleted:

```json
{
  "event": "meeting.cancelled",
  "timestamp": "2025-12-23T09:45:00.000000",
  "calendar_event": {
    "id": 456,
    "calendar_event_id": "google_calendar_event_id_123",
    "calendar_provider": "google",
    "title": "Team Standup",
    "description": "Weekly sync meeting",
    "platform": "google_meet",
    "native_meeting_id": "abc-defg-hij",
    "meeting_url": "https://meet.google.com/abc-defg-hij",
    "scheduled_start_time": "2025-12-23T10:00:00+00:00",
    "scheduled_end_time": "2025-12-23T10:30:00+00:00",
    "is_creator_self": true,
    "is_organizer_self": true,
    "status": "cancelled",
    "attendees": [...],
    "bot_id": 123
  },
  "user": {
    "external_user_id": "user-123",
    "account_user_id": 5,
    "account_id": 1
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
