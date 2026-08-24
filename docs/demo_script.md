# AI Agent Observability — Demo Script
### Talk Track & Presenter Guide

> **Audience:** Engineering leaders, AI/Platform teams evaluating LLM observability  
> **Time:** ~20 minutes · adjust by skipping acts  
> **Stack:** Gemini 2.5 Flash · OpenTelemetry OTLP · Elastic Cloud 9.6 Serverless

---

## 🧩 The Story

> *"Your team just shipped an AI agent in production. It calls a language model, searches your knowledge base, and runs calculations. Costs are climbing. Latency is unpredictable. One of your models silently started hallucinating. The on-call engineer opens Kibana and has no idea where to look. How do you get visibility into what an AI agent is actually doing?"*

This demo answers that with a **live** Gemini AI agent — fully instrumented with OpenTelemetry — shipping real traces, logs, and metrics to Elastic via OTLP as you use it.

---

## ✅ Pre-Demo Checklist (5 min before call)

- [ ] Agent running: `python3 web_agent/server.py` → http://localhost:5601
- [ ] Send 5–10 chat messages to warm up the data (tool calls need to appear)
- [ ] Kibana open at: `APM → Services → gemini-demo-agent`
- [ ] Tabs pre-opened: APM Overview · Service Map · Dashboard · SLOs · Alerts
- [ ] Time range: **Last 1 hour** (live data)
- [ ] Presentation open: `open presentation/index.html`

---

## 🎬 Act 1 — Show the Live Agent (2 min)

**Screen:** http://localhost:5601

> *"This is a live Gemini AI chat agent. Every message I send generates real OpenTelemetry telemetry — traces, logs, and metrics — shipping to Elastic right now via OTLP. Watch the sidebar."*

**Send:** `"What is the ESTC stock price and calculate 2 to the power of 20"`

Point to the UI as it responds:
- **Tool chips** appear before the response: `🔧 get_stock_price (54ms)` · `🔧 calculate (12ms)`
- **Trace ID** appears in the pills row below the response
- **Sidebar** updates: latency, tokens in/out, cost, tool calls count, A/B variant badge

> *"The agent called two tools — a stock price lookup and a math calculator — before calling the LLM. Each of those tool calls is a child span in the APM trace. You can see the A/B variant: this session is routed to model A (gemini-2.5-flash) — 30% of sessions go to the lighter 2.5-flash-lite for cost comparison."*

**Click 👍** on the response.

> *"The thumbs up just fired a feedback event — logged as an OTel record correlated to this exact trace ID. We can query which responses users liked and compare them to latency, model, and token count."*

---

## 🎬 Act 2 — APM Trace Waterfall (4 min)

**Screen:** Kibana → APM → Services → `gemini-demo-agent` → Transactions

> *"Every chat turn is one APM transaction: `invoke_agent`. Click any row."*

**Click a transaction → Trace waterfall**

> *"Here's what makes this different from basic APM. We have three levels of spans:*
> 1. *`invoke_agent` — the root SERVER span, the full agent turn*
> 2. *`tool:get_stock_price` and `tool:calculate` — CLIENT spans, one per tool call*
> 3. *`chat gemini-2.5-flash` — the CLIENT span for the actual LLM API call*"

**Click the `chat` span → Events tab**

> *"Span events store the full prompt and the full response — no truncation. This is how you answer 'what did the model actually receive and what did it say?' without a separate log search."*

**Click the `chat` span → Attributes tab**

> *"gen_ai.* semantic convention attributes: model name, input tokens, output tokens, peer.service set to 'google_gemini' — that's what drives the service map. And `ab.variant: A` — every span is tagged with which model was used, so we can compare A vs B in ES|QL."*

**Key message:** The agentic waterfall — agent → tools → LLM — is exactly what engineering teams need to debug slow responses and runaway costs.

---

## 🎬 Act 3 — Service Map (2 min)

**Click:** Service Map tab

> *"The service map is auto-discovered from the `peer.service` attribute on each CLIENT span. No configuration — Elastic builds this from the traces. You see:*
> - *`gemini-demo-agent` → `google_gemini` (the LLM call)*
> - *`gemini-demo-agent` → `tool.get_stock_price` (the stock API tool)*
> - *`gemini-demo-agent` → `tool.search_knowledge_base` (the search tool)*"

> *"In Elastic 9.5, this map is embedded on every alert detail page. If a latency alert fires, you open the alert and the service map is right there — dependency analysis without leaving the alert."*

---

## 🎬 Act 4 — Guardrails (1 min)

**Send in chat:** `"My email is test@company.com, can you help me?"`

> *"Watch the pills row — you'll see a red `⚠️ PII` badge. The agent detected an email address before calling the LLM, logged a guardrail violation event on the span, and incremented a metric counter. No data was blocked in this demo, but in production you'd mask or reject it here."*

**In APM → click the trace for this message → `invoke_agent` span → Events tab**

> *"The `guardrail.pii_detected` span event shows the PII type detected. This is your audit trail."*

---

## 🎬 Act 5 — Dashboard & Metrics (3 min)

**Screen:** Kibana → Dashboards → "🤖 LLM Observability — gemini-demo-agent"

> *"This dashboard is built from the OTel metrics the agent ships every 30 seconds. Four KPI tiles: agent turns, total tokens, average latency, tool calls. Two time-series panels below: turns over time and token burn rate."*

> *"All of this data is in `metrics-generic.otel-default` — Elasticsearch. You can write any ES|QL query against it, join it to your business data, alert on it."*

**Show the token time series:**

> *"This is your token budget control panel. You can see exactly when usage spikes, which model is burning tokens (the `ab.variant` dimension is in the data), and forecast cost."*

---

## 🎬 Act 6 — SLOs & Alerts (3 min)

**Screen:** Kibana → SLOs

> *"Two SLOs, created from the trace data: Response Latency (95% of turns under 6 seconds, 30-day rolling) and Availability (99% of turns succeed, 30-day rolling). The burn rate shows how fast we're spending our error budget."*

**Screen:** Kibana → Alerts → Rules

> *"Three alert rules: High Latency (fires if any turn exceeds 5 seconds), Error Spike (fires if 3+ errors in 5 minutes), and Token Budget (fires if token usage exceeds 50k per hour)."*

> *"In Elastic 9.5, when one of these alerts fires, two things happen automatically: the AI Assistant opens a triage investigation — it reads the alert, queries the relevant spans and logs, and proposes a root cause. And the service map is embedded right on the alert detail page so you can see which downstream dependency is contributing to the problem."*

**Key message:** From alert → AI-assisted triage → service map → trace waterfall — all in one platform, no tool switching.

---

## 🎬 Act 7 — A/B Model Comparison with ES|QL (2 min)

**Screen:** Kibana → Discover → Switch to ES|QL mode

```esql
FROM traces-generic.otel-default
| WHERE attributes.gen_ai.operation.name == "invoke_agent"
| STATS
    avg_latency_ms = AVG(duration) / 1000000,
    total_tokens   = SUM(attributes.gen_ai.usage.input_tokens),
    turns          = COUNT(*)
    BY attributes.ab.variant
| SORT attributes.ab.variant ASC
```

> *"This query compares model A vs model B: average latency, total tokens consumed, and number of turns — split by the `ab.variant` attribute we tag on every span. This is how you decide whether the lighter model is good enough to route 100% of traffic to."*

> *"Your data. Your query. No pre-built report to wait for."*

---

## 🎬 Act 8 — The Pitch (2 min)

> *"Let me summarise what you just saw:*

> *Three levels of spans per agent turn — root, tools, LLM call. Every turn traced at 100% sampling — nothing dropped. The full prompt and response stored on the span, not in a sidecar. PII guardrails emitting audit events. User feedback correlated back to the exact trace. A/B model routing visible in a single ES|QL query. SLOs, alert rules, and an AI assistant that triages incidents for you — all native in Elastic 9.5.*

> *This is built on OpenTelemetry gen_ai.* semantic conventions — the open standard. If your team already uses OTel for microservices, this is additive instrumentation. One Python file, one OTLP endpoint, zero new infrastructure."*

**Leave on screen:**
1. **Own your data** — traces, logs, metrics in Elasticsearch, query anything
2. **Open standard** — OTel gen_ai.* means no vendor lock-in
3. **One platform** — APM, SLOs, alerts, dashboards, AI triage, all in Kibana

---

## 💬 Q&A Handling

| Question | Answer |
|---|---|
| *"Can I see the actual conversation?"* | Yes — `gen_ai.content.prompt` and `gen_ai.content.completion` span events, full text, no truncation. Click any `chat` span → Events tab. |
| *"How does tool calling work?"* | Gemini function calling API — the agent decides which tools to call, each gets a child CLIENT span with `gen_ai.tool.name` and `peer.service`. The service map auto-discovers them. |
| *"How do I compare models?"* | Tag spans with `ab.variant` and query ES|QL. We showed this live — AVG latency and token usage split by variant in one query. |
| *"What about PII / data privacy?"* | PII detection before the LLM call, span events for audit trail, metric counter for dashboards. You can mask, block, or just flag — the guardrail is a hook in the span. |
| *"How much does ingestion cost?"* | A typical agent turn with tool calls is ~40 KB of trace data. 1000 turns/day = ~40 MB/day. Serverless pricing scales with actual usage. |
| *"Self-hosted models (Ollama, vLLM)?"* | Same OTel SDK, same gen_ai.* attributes — just set `gen_ai.system` to "ollama". The instrumentation is model-agnostic. |
| *"How is this different from Datadog LLM Obs?"* | You own the data in Elasticsearch. Arbitrary ES|QL queries, not pre-built reports. Traces + logs + metrics + SLOs + alerts in one system. No per-token ingestion tax from a third party. |
| *"What's Agent Observability & Monitoring in 9.5?"* | Tech preview — native Kibana UI for agentic traces. Exactly what we're emitting: tool call spans, LLM spans, reasoning steps. Our demo is the manual version of what that will productize. |

---

## 📊 Key Numbers to Quote

| Metric | Value |
|---|---|
| Sampling rate | 100% — every turn visible |
| OTel signals | 3 (traces, logs, metrics via OTLP) |
| Span levels per turn | 3 (agent → tools → LLM) |
| Tools available | 3 (search_knowledge_base, calculate, get_stock_price) |
| Models in A/B test | 2 (gemini-2.5-flash 70% · gemini-2.5-flash-lite 30%) |
| Metrics instruments | 10 (gen_ai.* + guardrails + feedback + host) |
| Alert rules | 3 (latency, errors, token budget) |
| SLOs | 2 (latency 95%, availability 99%) |
| Extra infrastructure | 0 (one Python file, one OTLP endpoint) |
| Time to add to existing OTel app | ~1 hour |

---

## 🔑 Navigation Reference

| What to show | Where |
|---|---|
| Live chat agent | http://localhost:5601 |
| APM service overview | APM → Services → gemini-demo-agent |
| Trace waterfall | APM → Transactions → invoke_agent → any row |
| Tool call spans | Trace waterfall → expand child spans |
| Full prompt/response | Click `chat` span → Events tab |
| Service map | APM → Service Map |
| LLM dashboard | Dashboards → "🤖 LLM Observability — gemini-demo-agent" |
| SLOs | Kibana → SLOs |
| Alert rules | Kibana → Alerts → Rules → filter "LLM" |
| A/B ES\|QL query | Discover → ES\|QL mode → query above |
| All conversations | Discover → traces-generic.otel-default |

---

## 🚀 Suggested Opening Line

> *"Before I share my screen — in the next 20 minutes you're going to watch a live AI agent call real tools, get traced end-to-end in Elastic APM, have its PII flagged by a guardrail, and be compared to a second model in a single ES|QL query. Everything you see is real data, generated right now, by the agent we're about to use."*

---

*Updated 2026-08-24 — Kibana 9.6 Serverless · v2.0*
