# AI Agent Observability — Elastic + OpenTelemetry

A live Gemini AI chat agent fully instrumented with the OpenTelemetry SDK.  
Every interaction ships **traces, logs, and metrics** via OTLP/HTTP to Elastic — exactly as a production service would.

![OTel](https://img.shields.io/badge/OpenTelemetry-OTLP%2FHTTP-7b61ff?logo=opentelemetry)
![Elastic](https://img.shields.io/badge/Elastic-APM-00bfb3?logo=elastic)
![Gemini](https://img.shields.io/badge/Google-Gemini%202.5%20Flash-blue?logo=google)
![Sampling](https://img.shields.io/badge/Sampling-100%25-green)

---

## What this demos

| Signal | Where it lands | What you see |
|--------|----------------|--------------|
| 📡 **Traces** | `traces-generic.otel-default` | Full waterfall: `invoke_agent [SERVER]` → `chat gemini-2.5-flash [CLIENT]` |
| 📋 **Logs** | `logs-generic.otel-default` | Structured logs with `trace_id` auto-correlated to every span |
| 📊 **Metrics** | `metrics-generic.otel-default` | `gen_ai.client.token.usage`, `gen_ai.client.operation.duration`, real CPU/memory via psutil |

**Service Map:** Kibana auto-discovers the `gemini-demo-agent → google_gemini` dependency from `peer.service` on the CLIENT span.

**Full request/response:** Stored as OTel span events (`gen_ai.content.prompt` / `gen_ai.content.completion`) — no truncation, fully searchable via ES|QL.

---

## Architecture

```
🌐 Browser (Chat UI)
      │ HTTP POST /api/chat
      ▼
⚡ Python Server  ──── Gemini API call ───▶  🤖 Google Gemini
 (OTel SDK · psutil)                         (gemini-2.5-flash)
      │
      │ OTLP/HTTP
      ▼
☁️  Elastic Cloud
  ├── /v1/traces   → APM · Service Map · Waterfall
  ├── /v1/metrics  → Token usage · Latency · Host metrics
  └── /v1/logs     → Correlated logs · Discover · Alerts
```

---

## Quick start

### 1. Prerequisites

```bash
pip install google-genai opentelemetry-sdk opentelemetry-exporter-otlp-proto-http psutil
```

### 2. Configure

```bash
cd web_agent
cp .env.example .env
# Edit .env — add your GEMINI_API_KEY, ES_ENDPOINT, ES_API_KEY
```

Get your Elastic credentials from [cloud.elastic.co](https://cloud.elastic.co):
- **ES_ENDPOINT** — the Elasticsearch endpoint (`https://<id>.es.<region>.elastic.cloud`)
- **ES_API_KEY** — an API key with `monitor` + indices write privileges

### 3. Run

```bash
export $(cat web_agent/.env | xargs)
python3 web_agent/server.py
```

Open **http://localhost:5601** — the chat UI is live.

### 4. Docker (optional)

```bash
cd web_agent
docker compose up --build
```

Includes an OTel Collector sidecar that ships real Docker container metrics and logs.

---

## OTel signals detail

### Traces (100% sampling — AlwaysOn)

Every chat turn produces one trace with two spans:

```
invoke_agent [SERVER, ~4s]          ← APM Transaction
  └── chat gemini-2.5-flash [CLIENT, ~3.6s]  ← APM Span
        ├── event: gen_ai.content.prompt      ← full user message
        └── event: gen_ai.content.completion  ← full LLM response
```

Key span attributes follow the [OTel gen_ai semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/):

| Attribute | Example |
|-----------|---------|
| `gen_ai.system` | `google_gemini` |
| `gen_ai.request.model` | `gemini-2.5-flash` |
| `gen_ai.usage.input_tokens` | `82` |
| `gen_ai.usage.output_tokens` | `190` |
| `peer.service` | `google_gemini` ← drives Service Map |

### Metrics (every 30 s)

| Metric | Type | Description |
|--------|------|-------------|
| `gen_ai.client.token.usage` | Counter | Tokens in/out per model |
| `gen_ai.client.operation.duration` | Histogram | End-to-end latency in seconds |
| `gen_ai.client.errors` | Counter | Errors by type |
| `system.cpu.utilization` | Observable Gauge | Real host CPU via psutil |
| `system.memory.usage` | Observable UpDownCounter | RSS bytes |
| `system.network.io` | Observable Counter | Network bytes in/out |

### Logs

Two log records per turn, both carrying `trace_id` + `span_id` for correlation:

- **Turn started** — `conversation_id`, `model`, `user_message_len`
- **Turn completed** — `in_tokens`, `out_tokens`, `latency_ms`, `cost_usd`, `is_error`

---

## Kibana navigation

| View | Path |
|------|------|
| Transactions | APM → Services → `gemini-demo-agent` → Transactions |
| Trace waterfall | Click any `invoke_agent` transaction |
| Service Map | APM → Services → `gemini-demo-agent` → Service Map |
| Logs | APM → Services → `gemini-demo-agent` → Logs |
| ES\|QL query | Discover → `traces-generic.otel-default` |

---

## ES|QL — query all conversations

```esql
FROM traces-generic.otel-default
| WHERE attributes.gen_ai.operation.name == "invoke_agent"
| KEEP @timestamp, attributes.gen_ai.conversation.id,
       attributes.gen_ai.usage.input_tokens,
       attributes.gen_ai.usage.output_tokens
| SORT @timestamp DESC
| LIMIT 50
```

---

## Files

```
web_agent/
├── server.py          # Full OTel web server (the main file)
├── Dockerfile         # Single-container build
├── docker-compose.yml # Agent + OTel Collector sidecar
├── otel-collector.yml # Collector config (Docker stats + filelog + OTLP)
└── .env.example       # Template — copy to .env
presentation/
└── index.html         # 13-slide standalone HTML deck (no deps)
docs/
└── demo_script.md     # Step-by-step 10-min demo walkthrough
```

---

## License

Apache 2.0 — see [LICENSE](LICENSE).
