# Vomeet Agents & Services

This document describes the autonomous agents and services that make up the Vomeet platform.

## Overview

Vomeet consists of several microservices and bot agents that work together to provide meeting transcription and management capabilities.

## Service Agents

### 1. API Gateway
**Location:** `services/api-gateway/`

The main entry point for external API requests. Handles:
- Request routing and load balancing
- Authentication and authorization
- Rate limiting
- WebSocket connections for real-time transcription

**Key Responsibilities:**
- Route requests to appropriate backend services
- Manage WebSocket connections for streaming transcription
- Handle CORS and security policies

### 2. Bot Manager
**Location:** `services/bot-manager/`

Orchestrates the lifecycle of meeting bots.

**Key Responsibilities:**
- Create and spawn bot instances for meetings
- Monitor bot health and status
- Handle bot scaling and resource allocation
- Manage bot job scheduling in Kubernetes

**Bot States:**
- `requested` - Bot request created, waiting to start
- `joining` - Bot container started, attempting to join meeting
- `awaiting_admission` - Bot in waiting room, waiting to be admitted
- `active` - Bot successfully joined and actively transcribing
- `stopping` - Stop request received, bot is shutting down
- `completed` - Bot successfully completed the meeting (terminal)
- `failed` - Bot failed at some stage (terminal)

### 3. Transcription Collector
**Location:** `services/transcription-collector/`

Aggregates and deduplicates transcription segments from WhisperLive servers.

**Key Responsibilities:**
- Receive real-time transcription streams from bots via WebSocket
- Deduplicate segments using Redis
- Filter non-informative content (filler words, noise)
- Store meaningful transcriptions in PostgreSQL
- Trigger webhooks for transcription events

**Features:**
- Modular filtering system (pattern matching, word counting)
- Configurable filters via `filter_config.py`
- Real-time streaming via WebSocket
- Speaker diarization support

### 4. Admin API
**Location:** `services/admin-api/`

Administrative interface for platform management.

**Key Responsibilities:**
- User management and authentication
- Organization/team management
- Bot configuration and settings
- Analytics and reporting endpoints
- Billing and usage tracking

### 5. Google Integration
**Location:** `services/google-integration/`

Integrates with Google Calendar and Meet.

**Key Responsibilities:**
- Google Calendar sync and webhook handling
- Automatic bot deployment for scheduled meetings
- OAuth2 authentication flow
- Calendar event monitoring
- Meeting link extraction and validation

**Components:**
- Main API service for OAuth and calendar operations
- Scheduler for periodic calendar polling
- Worker for background job processing

**Auto-Join Behavior:**
- Spawns bot **15 minutes** before scheduled meeting start time
- Configurable via `google-integration.autoJoin.minutesBefore`

## Bot Agents

### Vomeet Bot
**Location:** `services/vomeet-bot/`

The autonomous agent that joins meetings and captures audio/transcription. Uses a platform-agnostic architecture with shared meeting lifecycle management.

**Capabilities:**
- Join Google Meet and Microsoft Teams meetings
- Capture high-quality audio via browser automation
- Real-time speech-to-text transcription via WhisperLive
- Speaker identification
- Graceful admission handling (waiting room support)
- Automatic exit on meeting end or removal

**Architecture:**
- Node.js-based with Puppeteer for browser control
- Shared `runMeetingFlow()` orchestrates cross-platform lifecycle
- Platform-specific strategies for join, admission, recording, leave
- Browser-side helpers for audio capture and WebSocket streaming

**Deployment:**
- Runs as Kubernetes Jobs (ephemeral)
- Each bot is an isolated container instance
- Auto-scales based on meeting demand

**Integration:**
- Uses WhisperLive for real-time transcription
- Connects to transcription-collector via WebSocket
- Reports status to bot-manager

**Exit/Completion Reasons:**
- `stopped` - User stopped via API
- `validation_error` - Post-bot validation failed
- `awaiting_admission_timeout` - Timeout waiting to be admitted
- `awaiting_admission_rejected` - Rejected by meeting host
- `left_alone` - Timeout for being alone in meeting
- `evicted` - Kicked out from meeting by host

**Exit/Completion Reasons:**
- `stopped` - User stopped via API
- `validation_error` - Post-bot validation failed
- `awaiting_admission_timeout` - Timeout waiting to be admitted
- `awaiting_admission_rejected` - Rejected by meeting host
- `left_alone` - Timeout for being alone in meeting
- `evicted` - Kicked out from meeting by host

## Supporting Services

### Database (CloudNativePG)
PostgreSQL cluster for persistent data storage:
- User accounts and authentication
- Meeting metadata and recordings
- Transcription text and timestamps
- Bot status and logs
- Organization settings

### Cache (Dragonfly)
Redis-compatible cache for:
- Session management
- WebSocket connection tracking
- Rate limiting counters
- Real-time bot status
- Temporary data storage

### Whisper Proxy
**Location:** `services/whisper-cf-proxy/`

Optional proxy for Whisper transcription API to optimize performance and cost.

### WhisperLive
**Location:** `services/WhisperLive/`

Real-time Whisper transcription server (forked from collabora/WhisperLive).

**Key Features:**
- WebSocket-based streaming transcription
- Supports faster-whisper and TensorRT backends
- Real-time audio processing
- Integrates with bots via WebSocket

## Agent Communication

```
┌─────────────────────────────────────────────────────────────┐
│                         External                            │
│                  (Users, Calendar, Meetings)                │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
              ┌──────────────────────┐
              │    API Gateway       │
              │   (Entry Point)      │
              └──────────┬───────────┘
                         │
         ┌───────────────┼───────────────┐
         │               │               │
         ▼               ▼               ▼
┌─────────────┐  ┌──────────────┐  ┌─────────────┐
│  Bot        │  │   Google     │  │   Admin     │
│  Manager    │  │ Integration  │  │    API      │
└──────┬──────┘  └──────┬───────┘  └─────────────┘
       │                │
       │                │ (spawn trigger)
       ▼                ▼
┌─────────────────────────────┐
│      Vomeet Bot Agents      │
│    (Kubernetes Jobs)        │
└──────────┬──────────────────┘
           │
           │ (transcription stream)
           ▼
┌─────────────────────────────┐
│  Transcription Collector    │
│  (Processing & Storage)     │
└─────────────────────────────┘
           │
           ▼
┌─────────────────────────────┐
│   Database & Cache          │
│   (CNPG + Dragonfly)        │
└─────────────────────────────┘
```

## Agent Deployment

### Staging Environment
- **Cluster:** Separate Kubernetes cluster
- **Namespace:** `vomeet`
- **Domain:** `vomeet.voltade.sg`
- **Image Tag:** `staging`
- **Replicas:** 1 per service (cost-optimized)

### Production Environment
- **Cluster:** Separate Kubernetes cluster
- **Namespace:** `vomeet`
- **Domain:** `vomeet.voltade.com`
- **Image Tag:** `latest` or `stable`
- **Replicas:** 2-3 per service (high availability)

## Development

### Local Testing
See individual service READMEs for local development setup.

### Testing Bot Agents
```bash
# Test bot locally
cd testing/
python bot.py --meeting-url "https://meet.google.com/xxx-xxxx-xxx"

# Load testing
python load.py --concurrent-bots 10
```

### Monitoring Agent Health
```bash
# Check service status
kubectl get pods -n vomeet

# View bot manager logs
kubectl logs -f deployment/vomeet-bot-manager -n vomeet

# Check active bots
kubectl get jobs -n vomeet -l app=vomeet-bot

# View bot logs
kubectl logs job/vomeet-bot-<meeting-id> -n vomeet
```

## Troubleshooting

### Bot Won't Join Meeting
1. Check bot-manager logs for spawn errors
2. Verify meeting URL is valid and accessible
3. Check bot job status: `kubectl get jobs -n vomeet`
4. Review bot logs for authentication issues

### Transcription Not Appearing
1. Verify bot is active and connected
2. Check transcription-collector logs
3. Confirm WebSocket connection is established
4. Check database connectivity

### Google Calendar Not Syncing
1. Verify OAuth tokens are valid
2. Check google-integration scheduler logs
3. Confirm webhook endpoint is accessible
4. Review calendar permissions

## Security Considerations

### Service Accounts
- Each service has minimal required permissions
- Long-lived tokens for CI/CD deployment
- Kubernetes RBAC for resource access

### Bot Isolation
- Each bot runs in isolated container
- Network policies restrict bot communication
- Bots have no access to other meetings' data

### Data Protection
- All transcriptions encrypted at rest
- TLS for all service communication
- Regular security audits of bot behavior

## Performance & Scaling

### Auto-scaling
- Bot Manager scales based on meeting demand
- API Gateway scales with traffic load
- Database uses CNPG operator for HA

### Resource Limits
- Each bot: ~500MB RAM, 0.5 CPU
- Services: Configured per environment
- Database: Separate storage tiers

## Future Enhancements

- [ ] Multi-language transcription support
- [ ] Real-time translation agents
- [ ] Meeting summary generation (AI)
- [ ] Sentiment analysis agents
- [ ] Action item extraction
- [ ] Meeting insights and analytics

## Related Documentation

- [Deployment Guide](docs/deployment.md)
- [Staging Setup](docs/staging-setup.md)
- [API Documentation](docs/user_api_guide.md)
- [WebSocket Protocol](docs/websocket.md)
- [Contributing](CONTRIBUTING.md)
