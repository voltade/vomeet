# Google Calendar Push Notifications

This document explains how the Google Calendar push notification system works in the vomeet integration service.

## Overview

The system uses **Google Calendar Push Notifications** to receive real-time updates when calendar events change. This provides:
- Minimal API calls (respects rate limits)
- Low server load (event-driven architecture)
- Near-instant detection of calendar changes (< 1 second latency)

## Architecture

### Components

1. **Push Notification Webhook** (`/google/calendar/webhook`)
   - Receives notifications from Google Calendar API
   - Verifies channel tokens for security
   - Triggers immediate auto-join checks when calendar changes
   
2. **Channel Management**
   - Automatically creates push notification channels when users enable auto-join
   - Renews channels before they expire (every 6 days)
   - Stops channels when users disable auto-join or disconnect

3. **Fallback Polling** (15-minute intervals)
   - Backup for missed notifications
   - Channel renewal checks
   - Pre-meeting verification

## How It Works

### 1. Channel Creation

When a user enables auto-join:

```python
# Automatically triggered in PUT /users/{external_user_id}/settings
await create_push_notification_channel(integration, account, db)
```

This creates a webhook subscription with Google:
- Unique `channel_id` generated for tracking
- Security `channel_token` for webhook verification
- Webhook URL: `{WEBHOOK_BASE_URL}/google/calendar/webhook`
- Expires in 6 days (Google's max is 7 days)

### 2. Receiving Notifications

When calendar events change, Google sends POST requests to our webhook:

```http
POST /google/calendar/webhook
X-Goog-Channel-Token: {channel_token}
X-Goog-Channel-ID: {channel_id}
X-Goog-Resource-State: exists|sync|not_exists
X-Goog-Resource-ID: {resource_id}
X-Goog-Message-Number: {sequence_number}
```

**Resource States:**
- `sync` - Initial verification message when channel is created
- `exists` - Calendar event was created, updated, or still exists
- `not_exists` - Calendar event was deleted

### 3. Triggering Auto-Join

When we receive an `exists` notification:
1. Verify the `channel_token` matches (security check)
3. Enqueue an immediate auto-join check for that user
4. Check for upcoming meetings in the next 15 minutes
5. Check for upcoming meetings in the next 15 minutes
4. Spawn bots for meetings starting within `AUTO_JOIN_MINUTES_BEFORE`

### 4. Channel Renewal

Channels expire after 6 days. The scheduler checks for expiring channels:

```python
# In check_and_enqueue_auto_joins()
renewal_threshold = now + timedelta(hours=12)  # Renew if expiring in next 12 hours

if channel_expires_at < renewal_threshold:
    queue.enqueue("scheduler.renew_channel_for_user", ...)
```

Renewal process:
1. Stop the old channel
2. Create a new channel with new ID
3. Update database with new channel info

## Configuration

### Environment Variables

```bash
# Required: Your public-facing URL where Google can reach the webhook
WEBHOOK_BASE_URL=https://vomeet.io

AUTO_JOIN_CHECK_INTERVAL=900  # Fallback polling every 15 minutes
AUTO_JOIN_CHECK_INTERVAL=900  # Fallback polling every 15 minutes (default)
AUTO_JOIN_MINUTES_BEFORE=2    # Spawn bot 2 minutes before meeting

# Enable the scheduler
ENABLE_AUTO_JOIN_SCHEDULER=true
```

### Database Schema
Columns in `account_user_google_integrations`:

```sql
channel_id VARCHAR(64)           -- UUID for the push notification channel
channel_token VARCHAR(256)       -- Security token for webhook verification
resource_id VARCHAR(255)          -- Opaque ID from Google
channel_expires_at TIMESTAMP      -- When channel expires (renewed automatically)
sync_token TEXT                   -- For incremental sync (future usely)
sync_token TEXT                   -- For incremental sync (future optimization)
```

## API Endpoints

### Webhook Endpoint

```http
POST /google/calendar/webhook
```

**Called by Google Calendar API** when events change. Returns `200 OK` to acknowledge.

### Management (Automatic)

Channels are managed automatically:
- **Created**: When user enables auto-join
- **Renewed**: Every 6 days (before 7-day expiration)
- **Stopped**: When user disables auto-join or disconnects
Performance Characteristics

For 1000 users with auto-join enabled:

**API Calls per Day:**
- Channel management: ~167 renewals (1000 channels / 6 days)
- Push notifications: Variable (only when calendars change)
- Fallback polling: 96,000 calls (1000 users × 96 checks at 15min intervals)
- **Total: ~96,167 API calls/day**

**System Benefits:**
- **Latency**: < 1 second for calendar changes
- **Server Load**: Low (event-driven)
- **Rate Limits**: Stays well within Google's limits
- **Scalability**: Constant base load regardless of user countin intervals) = 96,000 calls/day
- **Total: ~96,167 calls/day (93% reduction)**

## Google Calendar API Documentation

- [Push Notifications Guide](https://developers.google.com/calendar/api/guides/push)
- [Watch Method](https://developers.google.com/calendar/api/v3/reference/events/watch)
- [Stop Method](https://developers.google.com/calendar/api/v3/reference/channels/stop)

## Troubleshooting

### Channel Creation Fails

Check:
1. `WEBHOOK_BASE_URL` is set and publicly accessible
2. SSL certificate is valid (not self-signed)
3. Webhook endpoint returns 200 OK on sync messages
4. Google Cloud project has Calendar API enabled

### Not Receiving Notifications

1. Check channel hasn't expired: `SELECT channel_expires_at FROM account_user_google_integrations`
2. Verify webhook is accessible: `curl https://{WEBHOOK_BASE_URL}/google/calendar/webhook`
3. Check logs for sync message receipt
4. Ensure no firewall blocking Google IPs

### Duplicate Bots Spawned

The system has deduplication:
- Redis key: `auto_join:spawned:{account_id}:{event_id}:{date}`
- TTL: 20 minutes
- Handles rescheduled meetings by checking event start time changes

## Migration from Polling

No action required! The system works in hybrid mode:
- Push notifications handle real-time updates
- Polling continues as fallback (reduced to 15min intervals)
- Gradually creates channels as users enable auto-join

To force channel creation for existing users:
Security

### Channel Token Verification

Each push notification channel includes a secure verification token:
- Generated using `secrets.token_urlsafe(48)` (256 chars max)
- Stored in the database with the channel
- Included in Google's webhook notifications via `X-Goog-Channel-Token` header
- Verified on every incoming notification to prevent spoofing

If the token doesn't match, the webhook returns `401 Unauthorized`.

### Additional Security

- HTTPS required for webhook endpoint
- Valid SSL certificate (not self-signed)
- Token doesn't contain sensitive data
- Automatic token rotation during channel renewal

## Future Enhancements

1. **Sync Tokens**: Use incremental sync for more efficient polling fallback
2. **Multi-Calendar Support**: Watch multiple calendars per user
3. **Advanced Filtering**: Reduce unnecessary notifications with smarter filtering