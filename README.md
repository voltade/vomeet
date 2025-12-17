<p align="left">
  <img src="assets/logodark.svg" alt="Vomeet Logo" width="40"/>
</p>

# Vomeet — Real-Time Meeting Transcription API

<p align="left">
  <img height="24" src="assets/google-meet.svg" alt="Google Meet" style="margin-right: 8px; vertical-align: middle;"/>
  <strong>Google Meet</strong>
    
  <img height="24" src="assets/microsoft-teams.svg" alt="Microsoft Teams" style="margin-right: 8px; vertical-align: middle;"/>
  <strong>Microsoft Teams</strong>
</p>

[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

## 🎉  What's new in v0.6 (4 Oct 2025)

- **Microsoft Teams support** (alongside Google Meet)
- **WebSocket transcripts streaming** for efficient sub-second delivery
- Numerous reliability and joining improvements from real-world usage of our hosted service

**Vomeet** drops a bot into your online meeting and streams transcripts to your apps in real time.

- **Platforms:** Google Meet **and Microsoft Teams**
- **Transport:** REST **or WebSocket (sub-second)**
- **Run it your way:** Open source & self-hostable, or use the hosted API.

👉 **Hosted (start in 5 minutes):** https://vomeet.ai

👉 **Self-host guide:** [docs/deployment.md](docs/deployment.md)

---

> See full release notes: https://github.com/voltade/vomeet/releases

---

## Quickstart

- Hosted (fastest): Get your API key at [https://vomeet.ai/dashboard/api-keys](https://vomeet.ai/dashboard/api-keys)

Or self-host the entire stack:

Self-host with Docker Compose:

```bash
git clone https://github.com/voltade/vomeet.git
cd vomeet
make all            # CPU by default (Whisper tiny) — good for development
# For GPU:
# make all TARGET=gpu    # (Whisper medium) — recommended for production quality
```

* Full guide: [docs/deployment.md](docs/deployment.md)
* For self-hosted API key: follow `vomeet/nbs/0_basic_test.ipynb`
* Health: API Gateway at `http://localhost:18056/healthz`

## 1. Send bot to meeting:

`API_HOST` for hosted version is `https://api.cloud.vomeet.ai `
`API_HOST` for self-hosted version (default) is `http://localhost:18056`

### Request a bot for Microsoft Teams

```bash
curl -X POST https://<API_HOST>/bots \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <API_KEY>" \
  -d '{
    "platform": "teams",
    "native_meeting_id": "<NUMERIC_MEETING_ID>",
    "passcode": "<MEETING_PASSCODE>"
  }'
```

### Or request a bot for Google Meet

```bash
curl -X POST https://<API_HOST>/bots \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <API_KEY>" \
  -d '{
    "platform": "google_meet",
    "native_meeting_id": "<MEET_CODE_XXX-XXXX-XXX>"
  }'
```

## 2. Get transcripts:

### Get transcripts over REST

```bash
curl -H "X-API-Key: <API_KEY>" \
  "https://<API_HOST>/transcripts/<platform>/<native_meeting_id>"
```

For real-time streaming (sub‑second), see the [WebSocket guide](docs/websocket.md).
For full REST details, see the [User API Guide](docs/user_api_guide.md).

Note: Meeting IDs are user-provided (Google Meet code like `xxx-xxxx-xxx` or Teams numeric ID and passcode). Vomeet does not generate meeting IDs.

---

## Who Vomeet is for

* **Enterprises (self-host):** Data sovereignty and control on your infra
* **Teams using hosted API:** Fastest path from meeting to transcript
* **n8n/indie builders:** Low-code automations powered by real-time transcripts
  - Tutorial: https://vomeet.ai/blog/google-meet-transcription-n8n-workflow

---

## Roadmap

* Zoom support (public preview next)

For issues and roadmap progress, use GitHub issues: https://github.com/voltade/vomeet/issues.

## Build on Top. In Hours, Not Months

**Build powerful meeting assistants (like Otter.ai, Fireflies.ai, Fathom) for your startup, internal use, or custom integrations.**

The Vomeet API provides powerful abstractions and a clear separation of concerns, enabling you to build sophisticated applications on top with a safe and enjoyable coding experience.

<p align="center">
  <img src="assets/simplified_flow.png" alt="Vomeet Architecture Flow" width="100%"/>
</p>

- [api-gateway](./services/api-gateway): Routes API requests to appropriate services
- [mcp](./services/mcp): Provides MCP-capable agents with Vomeet as a toolkit
- [bot-manager](./services/bot-manager): Handles bot lifecycle management
- [vomeet-bot](./services/vomeet-bot): The bot that joins meetings and captures audio
- [WhisperLive](./services/WhisperLive): Real-time audio transcription service
- [transcription-collector](./services/transcription-collector): Processes and stores transcription segments
- [Database models](./libs/shared-models/shared_models/models.py): Data structures for storing meeting information

> 💫 If you're building with Vomeet, we'd love your support! [Star our repo](https://github.com/voltade/vomeet/stargazers) to help us reach 1500 stars.

### Features:

- **Real-time multilingual transcription** supporting **100 languages** with **Whisper**
- **Real-time translation** across all 100 supported languages

## Current Status

- **Public API**: Fully available with self-service API keys at [www.vomeet.ai](https://www.vomeet.ai/?utm_source=github&utm_medium=readme&utm_campaign=vomeet_repo)
- **Google Meet Bot:** Fully operational bot for joining Google Meet calls
- **Teams Bot:** Supported in v0.6
- **Real-time Transcription:** Low-latency, multilingual transcription service is live
- **Real-time Translation:** Instant translation between 100 supported languages
- **WebSocket Streaming:** Sub-second transcript delivery via WebSocket API

## Coming Next

- **Zoom Bot:** Integration for automated meeting attendance (July 2025)
- **Direct Streaming:** Ability to stream audio directly from web/mobile apps

## Self-Deployment

For **security-minded companies**, Vomeet offers complete **self-deployment** options.

To run Vomeet locally on your own infrastructure, the primary command you'll use after cloning the repository is `make all`. This command sets up the environment (CPU by default, or GPU if specified), builds all necessary Docker images, and starts the services.

**Deployment & Management Guides:**
- [Local Deployment and Testing Guide](docs/deployment.md)
- [Self-Hosted Management Guide](docs/self-hosted-management.md) - Managing users and API tokens

## Contributing

Contributors are welcome! Join our community and help shape Vomeet's future. Here's how to get involved:

1. **Understand Our Direction**: Review the roadmap and open issues.
2. **Discuss & Get Assigned**: Open or comment on GitHub issues to propose work and get alignment.
3. **Development Process**:

   * Browse available **tasks** in GitHub issues or the roadmap.
   * Coordinate in the issue thread to avoid duplication.
   * Submit **pull requests** for review.

- **Critical Tasks & Bounties**:
  - Selected **high-priority tasks** may be marked with **bounties**.
  - Bounties are sponsored by the **Vomeet core team**.
  - Check task descriptions in GitHub issues/roadmap for bounty details and requirements.

We look forward to your contributions!

## Contributing & License

We ❤️ contributions. Open issues/PRs on GitHub.
Licensed under **Apache-2.0** — see [LICENSE](LICENSE).

## Project Links

- 🌐 [Vomeet Website](https://vomeet.ai)
- 💼 [LinkedIn](https://www.linkedin.com/company/voltade/)

The Vomeet name and logo are trademarks of **Voltade Pte. Ltd.**.
