# AI Coding Assistant Guide

This file provides context for AI coding assistants working on the Vomeet codebase.

## Project Overview

Vomeet is a meeting transcription platform with microservices architecture. Bots join video meetings (Google Meet, Teams), capture audio, and stream real-time transcriptions via WhisperLive.

## Repository Structure

```
services/
├── api-gateway/       # FastAPI - request routing, auth, WebSocket
├── bot-manager/       # FastAPI - bot lifecycle, K8s job orchestration
├── admin-api/         # FastAPI - user/org management, billing
├── google-integration/# FastAPI + RQ - calendar sync, OAuth, bot spawning
├── transcription-collector/  # FastAPI - transcription aggregation, webhooks
├── vomeet-bot/        # Node.js + Puppeteer - meeting bot agent
├── WhisperLive/       # Python - real-time Whisper transcription server
└── whisper-cf-proxy/  # Cloudflare Worker - Whisper API proxy

libs/
└── shared-models/     # SQLAlchemy models, Alembic migrations

chart/                 # Helm chart for K8s deployment
docs/                  # User-facing documentation
testing/               # Integration and load testing scripts
```

## Tech Stack

- **Python services**: FastAPI, SQLAlchemy, Alembic, RQ (Redis Queue)
- **Bot**: Node.js, Puppeteer, WebSocket
- **Database**: PostgreSQL (CloudNativePG operator)
- **Cache**: Dragonfly (Redis-compatible)
- **Deployment**: Kubernetes, Helm

## Key Conventions

### Python Services

- Use FastAPI with async endpoints
- SQLAlchemy models in `libs/shared-models/shared_models/`
- Pydantic for request/response schemas
- Tests use pytest with fixtures in `conftest.py`
- Environment variables for configuration (no hardcoded secrets)

### Database

- Local dev database: `postgresql://postgres:postgres@localhost:15438/vomeet`
- Migrations via Alembic in `libs/shared-models/alembic/versions/`
- `ScheduledMeeting` is the source of truth for all meeting requests
- `Meeting` tracks bot execution lifecycle

### Bot States

Terminal states: `completed`, `failed`
Active states: `requested`, `joining`, `awaiting_admission`, `active`, `stopping`

### Inter-Service Communication

- Services share database (no HTTP between services)
- RQ for async job queues (google-integration → bot-manager)
- WebSocket for real-time transcription streaming

## Common Tasks

### Database Migrations

Migrations are managed with Alembic. Migration files live in `libs/shared-models/alembic/versions/`.

```bash
source .venv/bin/activate
# Generate a new migration (auto-detects model changes)
DB_HOST=localhost DB_PORT=15438 python -m alembic revision --autogenerate -m "description_of_change"

# Create a blank migration (for manual SQL)
DB_HOST=localhost DB_PORT=15438 python -m alembic revision -m "description_of_change"

# Apply all pending migrations
DB_HOST=localhost DB_PORT=15438 python -m alembic upgrade head

# Check current migration status
DB_HOST=localhost DB_PORT=15438 python -m alembic current

# Rollback one migration
DB_HOST=localhost DB_PORT=15438 python -m alembic downgrade -1
```

### Running Tests

```bash
# From service directory
pytest tests/ -v

# Specific test file
pytest tests/test_bot_spawn.py -v
```

### Adding a New Endpoint

1. Add route in `app/routes/` or `app/api/`
2. Add Pydantic schemas if needed
3. Add tests in `tests/`
4. Update API docs if user-facing

## Important Files

- `libs/shared-models/shared_models/models.py` - All database models
- `services/bot-manager/app/orchestrator.py` - K8s job creation
- `services/google-integration/calendar_sync.py` - Calendar webhook handling
- `services/google-integration/bot_spawn.py` - Scheduled bot spawning
- `services/transcription-collector/filters.py` - Transcription filtering logic
- `chart/values-*.yaml` - Environment-specific Helm values

## Testing Approach

- Unit tests mock external dependencies (database, Redis, K8s API)
- Use `pytest-asyncio` for async test functions
- Fixtures provide test data and mocked services
- Integration tests in `testing/` directory

## Gotchas

- `ScheduledMeeting.calendar_provider='api'` indicates ad-hoc (non-calendar) meetings
- Bot status webhooks go to user-configured URLs, not internal services
- WhisperLive is a fork - check `services/WhisperLive/` for local modifications
- Helm chart uses separate values files per environment

## Documentation

- [docs/user_api_guide.md](docs/user_api_guide.md) - External API reference
- [docs/websocket.md](docs/websocket.md) - WebSocket protocol
- [docs/deployment.md](docs/deployment.md) - Deployment guide
- [webhook_schema.md](webhook_schema.md) - Webhook event schemas
