# Vomeet Whisper Proxy (Cloudflare Workers)

A Cloudflare Workers-based proxy that receives audio from vomeet-bot, stores it in R2, and transcribes using Cloudflare Workers AI (Whisper large-v3-turbo).

## Architecture

```
vomeet-bot  →  WebSocket  →  CF Worker (Durable Object)
                                    ↓
                            Store audio chunks in R2
                                    ↓
                            Queue transcription job
                                    ↓
                            CF Workers AI (whisper-large-v3-turbo)
                                    ↓
                            Send results to transcription-collector
```

## Features

- **WebSocket compatible** with vomeet-bot's WhisperLive protocol
- **Audio storage** in Cloudflare R2 (cheap, durable)
- **Async transcription** via Cloudflare Queues
- **Cost effective**: ~$0.00051/min for transcription

## Setup

### 1. Install dependencies

```bash
cd services/whisper-cf-proxy
npm install
```

### 2. Create R2 bucket

```bash
wrangler r2 bucket create vomeet-audio
```

### 3. Create Queue

```bash
wrangler queues create transcription-queue
```

### 4. Configure webhook URL

Set the secret for the webhook callback:
```bash
npx wrangler secret put VOMEET_WEBHOOK_URL
# Enter: https://vomeet.io/transcripts/webhook
```

### 5. Deploy

```bash
npm run deploy
```

## Usage

### Configure vomeet deployment

Point bot-manager to the Cloudflare Worker:

```bash
helm upgrade --install vomeet ./chart \
  --set "bot-manager.env.WHISPER_LIVE_URL=wss://vomeet-whisper-proxy.YOUR_SUBDOMAIN.workers.dev/ws"
```

### Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/ws` | WebSocket | Audio streaming (WhisperLive-compatible) |
| `/healthz` | GET | Health check |
| `/transcribe` | POST | Manual transcription trigger |

## Cost Estimate

| Component | Cost |
|-----------|------|
| Workers AI (Whisper) | $0.00051/audio minute |
| R2 Storage | $0.015/GB/month |
| R2 Operations | $0.36/million requests |
| Workers | Free tier: 100k req/day |

**Example**: 1 hour meeting = ~$0.03 transcription + negligible storage

## Audio Storage

Audio is stored in R2 with the following structure:
```
{session_id}/{timestamp}-{chunk_index}.raw
```

Metadata includes:
- `sessionId`: Bot session UUID
- `meetingId`: Meeting identifier
- `language`: Detected/specified language
- `sampleRate`: Audio sample rate (16000 Hz)
- `timestamp`: Unix timestamp

## Development

```bash
# Run locally
npm run dev

# View logs
npm run tail
```

## Limitations

- **Not real-time**: ~10 second buffer before transcription
- **Latency**: 2-5 seconds for transcription after buffer flush
- **No streaming output**: Results come in chunks, not word-by-word

For real-time transcription with <1s latency, use self-hosted WhisperLive with GPU.
