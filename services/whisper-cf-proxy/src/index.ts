import { DurableObject } from "cloudflare:workers";
import { Buffer } from "node:buffer";

export interface Env {
  AI: Ai;
  AUDIO_BUCKET: R2Bucket;
  TRANSCRIPTION_QUEUE: Queue;
  WEBHOOK_QUEUE: Queue;
  WHISPER_SESSION: DurableObjectNamespace;
  VOMEET_WEBHOOK_URL?: string;  // Set via wrangler secret put VOMEET_WEBHOOK_URL
}

interface SessionConfig {
  uid: string;
  language?: string;
  task: string;
  meeting_id?: number;
  platform?: string;
  token?: string;
}

interface TranscriptionJob {
  type: "transcription";
  sessionId: string;
  audioKey: string;
  config: SessionConfig;
  chunkIndex: number;
  timestamp: number;
}

interface WebhookJob {
  type: "webhook";
  sessionId: string;
  meetingId?: number;
  token?: string;
  chunkIndex: number;
  timestamp: number;
  text: string;
  segments?: Array<{ start: number; end: number; text: string }>;
  language?: string;
  languageProbability?: number;
  duration?: number;
}

type QueueJob = TranscriptionJob | WebhookJob;

// Durable Object for managing WebSocket sessions
export class WhisperSession extends DurableObject {
  private audioBuffer: ArrayBuffer[] = [];
  private config: SessionConfig | null = null;
  private chunkIndex = 0;
  private lastFlushTime = Date.now();
  private readonly BUFFER_DURATION_MS = 10000; // 10 seconds
  private readonly SAMPLE_RATE = 16000;

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
  }

  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    
    if (url.pathname === "/ws") {
      const upgradeHeader = request.headers.get("Upgrade");
      if (upgradeHeader !== "websocket") {
        return new Response("Expected WebSocket", { status: 426 });
      }

      const pair = new WebSocketPair();
      const [client, server] = Object.values(pair);

      this.ctx.acceptWebSocket(server);

      return new Response(null, {
        status: 101,
        webSocket: client,
      });
    }

    return new Response("Not found", { status: 404 });
  }

  async webSocketMessage(ws: WebSocket, message: ArrayBuffer | string) {
    const env = this.env as Env;

    // Handle JSON config message
    if (typeof message === "string") {
      try {
        const data = JSON.parse(message);
        if (data.uid) {
          this.config = data as SessionConfig;
          // Send server ready response (matching WhisperLive protocol)
          ws.send(JSON.stringify({
            uid: data.uid,
            status: "SERVER_READY",
            message: "Cloudflare Whisper Proxy ready"
          }));
          console.log(`[WhisperSession] Config received for ${data.uid}`);
        }
      } catch (e) {
        console.error("[WhisperSession] Error parsing config:", e);
      }
      return;
    }

    // Handle binary audio data
    if (message instanceof ArrayBuffer) {
      this.audioBuffer.push(message);
      
      // Check if we should flush (every BUFFER_DURATION_MS)
      const now = Date.now();
      if (now - this.lastFlushTime >= this.BUFFER_DURATION_MS) {
        await this.flushAudioBuffer(env, ws);
      }
    }
  }

  async webSocketClose(ws: WebSocket, code: number, reason: string) {
    const env = this.env as Env;
    console.log(`[WhisperSession] WebSocket closed: ${code} - ${reason}`);
    
    // Flush remaining audio
    if (this.audioBuffer.length > 0) {
      await this.flushAudioBuffer(env, ws);
    }
  }

  private async flushAudioBuffer(env: Env, ws: WebSocket) {
    if (!this.config || this.audioBuffer.length === 0) return;

    const sessionId = this.config.uid;
    const chunkIndex = this.chunkIndex++;
    const timestamp = Date.now();
    
    // Concatenate audio chunks
    const totalLength = this.audioBuffer.reduce((acc, buf) => acc + buf.byteLength, 0);
    const combined = new Uint8Array(totalLength);
    let offset = 0;
    for (const buf of this.audioBuffer) {
      combined.set(new Uint8Array(buf), offset);
      offset += buf.byteLength;
    }

    // Store in R2
    const audioKey = `${sessionId}/${timestamp}-${chunkIndex}.raw`;
    await env.AUDIO_BUCKET.put(audioKey, combined.buffer, {
      customMetadata: {
        sessionId,
        chunkIndex: String(chunkIndex),
        sampleRate: String(this.SAMPLE_RATE),
        timestamp: String(timestamp),
        meetingId: String(this.config.meeting_id || ""),
        language: this.config.language || "",
      }
    });

    console.log(`[WhisperSession] Stored audio chunk: ${audioKey} (${combined.byteLength} bytes)`);

    // Queue transcription job
    const job: TranscriptionJob = {
      type: "transcription",
      sessionId,
      audioKey,
      config: this.config,
      chunkIndex,
      timestamp,
    };
    await env.TRANSCRIPTION_QUEUE.send(job);

    // Clear buffer
    this.audioBuffer = [];
    this.lastFlushTime = Date.now();

    // Send acknowledgment
    ws.send(JSON.stringify({
      uid: sessionId,
      status: "BUFFERED",
      chunk: chunkIndex,
    }));
  }
}

// Queue consumer for transcription
export default {
  // HTTP handler for WebSocket upgrade
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);
    
    // Health check
    if (url.pathname === "/healthz") {
      return new Response("OK", { status: 200 });
    }

    // WebSocket connection - route to Durable Object
    if (url.pathname === "/ws") {
      const sessionId = url.searchParams.get("session") || crypto.randomUUID();
      const id = env.WHISPER_SESSION.idFromName(sessionId);
      const stub = env.WHISPER_SESSION.get(id);
      return stub.fetch(request);
    }

    return new Response("Vomeet Whisper Proxy\n\nEndpoints:\n- GET /ws - WebSocket connection\n- GET /healthz - Health check", { 
      status: 200,
      headers: { "Content-Type": "text/plain" }
    });
  },

  // Queue consumer - handles both transcription and webhook jobs
  async queue(batch: MessageBatch<QueueJob>, env: Env): Promise<void> {
    for (const message of batch.messages) {
      const job = message.body;
      
      try {
        if (job.type === "webhook") {
          await processWebhookJob(env, job);
        } else {
          await processTranscriptionJob(env, job);
        }
        message.ack();
      } catch (error) {
        console.error(`[Queue] Error processing ${job.type} job:`, error);
        message.retry();
      }
    }
  },
};

// Process transcription job - transcribe audio and queue webhook
async function processTranscriptionJob(env: Env, job: TranscriptionJob): Promise<void> {
  console.log(`[Queue] Processing transcription job: ${job.audioKey}`);
  
  const result = await transcribeAudio(env, job.audioKey);
  
  // Queue webhook delivery as separate job (if configured and has text)
  if (result.text && env.VOMEET_WEBHOOK_URL) {
    // Map segments to ensure required fields
    const mappedSegments = result.segments?.map(seg => ({
      start: seg.start ?? 0,
      end: seg.end ?? 0,
      text: seg.text ?? "",
    }));

    const webhookJob: WebhookJob = {
      type: "webhook",
      sessionId: job.sessionId,
      meetingId: job.config.meeting_id,
      token: job.config.token,
      chunkIndex: job.chunkIndex,
      timestamp: job.timestamp,
      text: result.text,
      segments: mappedSegments,
      language: result.transcription_info?.language,
      languageProbability: result.transcription_info?.language_probability,
      duration: result.transcription_info?.duration,
    };
    
    await env.WEBHOOK_QUEUE.send(webhookJob);
    console.log(`[Queue] Queued webhook job for chunk ${job.chunkIndex}`);
  } else if (result.text) {
    console.log(`[Queue] Transcription complete (no collector configured): ${result.text.substring(0, 100)}...`);
  }
}

// Process webhook job - send to collector with retry
async function processWebhookJob(env: Env, job: WebhookJob): Promise<void> {
  const webhookUrl = env.VOMEET_WEBHOOK_URL!;
  
  const payload = {
    session_id: job.sessionId,
    meeting_id: job.meetingId,
    chunk_index: job.chunkIndex,
    timestamp: job.timestamp,
    text: job.text,
    segments: job.segments,
    language: job.language,
    language_probability: job.languageProbability,
    duration: job.duration,
  };

  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };
  
  if (job.token) {
    headers["Authorization"] = `Bearer ${job.token}`;
  }

  const response = await fetch(webhookUrl, {
    method: "POST",
    headers,
    body: JSON.stringify(payload),
  });

  if (!response.ok) {
    const body = await response.text();
    throw new Error(`Collector responded with ${response.status}: ${body.substring(0, 200)}`);
  }
  
  console.log(`[Webhook] Successfully delivered chunk ${job.chunkIndex}`);
}

// Transcribe audio from R2 using Cloudflare AI
async function transcribeAudio(env: Env, audioKey: string): Promise<Ai_Cf_Openai_Whisper_Large_V3_Turbo_Output> {
  // Get audio from R2
  const object = await env.AUDIO_BUCKET.get(audioKey);
  if (!object) {
    throw new Error(`Audio not found: ${audioKey}`);
  }

  const audioBuffer = await object.arrayBuffer();
  const metadata = object.customMetadata || {};
  
  // Convert Float32 PCM to base64
  // The audio from vomeet-bot is Float32Array at 16kHz
  const float32Array = new Float32Array(audioBuffer);
  
  // Convert to WAV format for Whisper API
  const wavBuffer = float32ToWav(float32Array, parseInt(metadata.sampleRate || "16000"));
  const base64Audio = arrayBufferToBase64(wavBuffer);

  console.log(`[Transcribe] Processing ${audioKey}, ${float32Array.length} samples`);

  // Call Cloudflare AI
  const result = await env.AI.run("@cf/openai/whisper-large-v3-turbo", {
    audio: base64Audio,
    task: "transcribe",
    language: metadata.language || undefined,
    vad_filter: true,
  });

  console.log(`[Transcribe] Result: ${result.text?.substring(0, 100)}...`);
  
  return result;
}

// Helper: Convert Float32 PCM to WAV
function float32ToWav(samples: Float32Array, sampleRate: number): ArrayBuffer {
  const numChannels = 1;
  const bitsPerSample = 16;
  const bytesPerSample = bitsPerSample / 8;
  const blockAlign = numChannels * bytesPerSample;
  const byteRate = sampleRate * blockAlign;
  const dataSize = samples.length * bytesPerSample;
  const bufferSize = 44 + dataSize;
  
  const buffer = new ArrayBuffer(bufferSize);
  const view = new DataView(buffer);
  
  // WAV header
  writeString(view, 0, 'RIFF');
  view.setUint32(4, bufferSize - 8, true);
  writeString(view, 8, 'WAVE');
  writeString(view, 12, 'fmt ');
  view.setUint32(16, 16, true); // fmt chunk size
  view.setUint16(20, 1, true); // PCM format
  view.setUint16(22, numChannels, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, byteRate, true);
  view.setUint16(32, blockAlign, true);
  view.setUint16(34, bitsPerSample, true);
  writeString(view, 36, 'data');
  view.setUint32(40, dataSize, true);
  
  // Convert Float32 to Int16
  let offset = 44;
  for (let i = 0; i < samples.length; i++) {
    const sample = Math.max(-1, Math.min(1, samples[i]));
    const int16 = sample < 0 ? sample * 0x8000 : sample * 0x7FFF;
    view.setInt16(offset, int16, true);
    offset += 2;
  }
  
  return buffer;
}

function writeString(view: DataView, offset: number, str: string): void {
  for (let i = 0; i < str.length; i++) {
    view.setUint8(offset + i, str.charCodeAt(i));
  }
}

function arrayBufferToBase64(buffer: ArrayBuffer): string {
  return Buffer.from(buffer).toString("base64");
}
