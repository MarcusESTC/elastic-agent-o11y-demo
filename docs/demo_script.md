# AI / LLM Observability Demo
### Story, Talk Track & Presenter's Script

> **Cluster:** your Elastic Cloud deployment  
> **Audience:** Engineering leaders, AI/Platform teams evaluating observability for their LLM systems  
> **Time:** ~20 minutes (adjust by skipping slides)

---

## 🧩 The Story

> *"Your team just shipped an AI-powered product. LLM calls are scattered across 4 agent services, 2 model providers, and 6 tools. Costs are climbing. Latency is unpredictable. The on-call engineer has no idea which model is misbehaving at 3 AM. How do you get visibility?"*

This demo answers that question using **Elastic APM** extended with **OpenTelemetry semantic conventions for LLMs** (`gen_ai.*`). We'll walk through 4 AI agent services — each running in production — and show everything you'd expect from a Datadog LLM Obs product, *built natively in Elasticsearch*.

---

## 📋 Pre-Demo Checklist

Before the call, confirm:
- [ ] Kibana is open to the APM section (`/app/apm`)
- [ ] Time range set to **Last 48 hours**
- [ ] Dashboard `LLM Observability — ai-research-agent` is bookmarked
- [ ] Browser tabs pre-opened: APM Services, the LLM dashboard, Discover
- [ ] Run `data/generate_ai_agent.py` if data is older than 2 days

---

## 🎬 Act 1 — The Problem (2 min)

**Slide / screen:** APM Services list

> *"Here's what an AI backend looks like in APM. Four agent services — `ai-research-agent`, `code-review-agent`, `customer-support-agent`, and `data-analyst-agent`. They use LangGraph, CrewAI, AutoGen, and LangChain respectively. Each calls OpenAI and Anthropic models, searches Elasticsearch, executes code, fetches URLs."*

> *"In a traditional APM product, you'd see latency and error rates — which is great. But for AI workloads you need more: token counts, model costs, per-model latency, conversation tracing. Let's look at that."*

**Key message:** Standard APM is necessary but not sufficient for AI. You need token economics.

---

## 🎬 Act 2 — Service Overview (3 min)

**Click:** `ai-research-agent` service → **Overview tab**

> *"The Overview tab shows us the classic APM view — throughput, latency P95, error rate. This agent is processing research tasks, hitting gpt-4o and Claude Sonnet. P95 is around 12 seconds — expected for a multi-step reasoning chain."*

**Click:** **Transactions tab**

> *"Every agent task shows up as a transaction. The transaction name is the scenario — 'technical research', 'market analysis'. I can click any one and see the full distributed trace waterfall: planning → embedding → retrieval → tool calls → inference → synthesis."*

**Click any transaction → Trace waterfall**

> *"This is the span waterfall. Every hop is here: the planning LLM call, the embedding call, the Elasticsearch retrieval, tool execution, the final chat inference. OTel gen_ai.* attributes — model, token counts, input/output messages — are attached to each span. Click the Labels tab on any LLM span and you'll see the prompt and response stored in the trace.*"

**Click an LLM span → Labels tab**

> *"Input and output messages, model name, provider, token counts — all in the span. This is how you answer 'what did the model actually say?' without a separate log ship."*

---

## 🎬 Act 3 — Token Economics & Cost (4 min)

**Navigate to:** Dashboard `LLM Observability — ai-research-agent`

> *"Now the story gets interesting. This dashboard is built 100% from ES|QL queries on the same APM trace data — no separate ingestion, no second tool. Let me walk through the KPI tiles."*

**Point to top row:**

> *"Total requests, input tokens, output tokens, estimated cost in USD, error rate, P95 latency. The cost tile uses a CASE expression in ES|QL — gpt-4o at \$2.50 per million input tokens, gpt-4o-mini at \$0.15, Claude Sonnet at \$3.00, Claude Haiku at \$0.80. One query, one number."*

**Point to token usage chart:**

> *"Token burn over time — stacked input vs output. You can see the evening research spike and the quieter overnight window. This is your first tool for budget forecasting."*

**Point to donuts:**

> *"Requests by model — Claude Haiku and gpt-4o-mini are doing most of the work, which is the right design. Requests by provider — we're dual-provider, which gives us resilience."*

**Point to cost bar:**

> *"Cost breakdown by model. Claude Sonnet is the most expensive per call but drives the highest-quality research steps. gpt-4o-mini handles the cheap planning steps. This is the conversation the AI platform team needs to have with finance."*

**Key message:** Elasticsearch is the system of record for your AI costs, not a third-party cost dashboard.

---

## 🎬 Act 4 — Dependencies & Service Map (3 min)

**Back in APM → `ai-research-agent` → Dependencies tab**

> *"The Dependencies tab shows every external service this agent calls — OpenAI for chat and embeddings, Anthropic for Sonnet/Haiku, Elasticsearch for retrieval, the Code Executor sandbox, and external web APIs. Error rates and latency per dependency, live."*

> *"If OpenAI's API degrades at 2 AM, this tab shows it first — before the model starts returning garbage, before users complain."*

**Click:** Service Map (left nav)

> *"The Service Map renders the full call graph: four agents fanning out to OpenAI, Anthropic, Elasticsearch, and external services. Thicker lines = more traffic. Red = errors. This is how an on-call engineer immediately understands blast radius during an incident."*

---

## 🎬 Act 5 — Errors & Logs (3 min)

**Click:** `ai-research-agent` → **Errors tab**

> *"The Errors tab groups exceptions by type. Here we see RateLimitError from OpenAI, ConnectionTimeout from web fetches, NotFoundError from Elasticsearch. Each error is linked to its trace — one click and I'm in the waterfall that produced it."*

> *"This matters because a 'high error rate' alert without a trace is useless. With Elastic, the error IS the trace."*

**Click:** **Logs tab**

> *"Finally, structured application logs correlated to every trace. Each log line carries trace.id and transaction.id — so if a customer reports a bad response, I search by conversation ID, find the trace, jump to the logs, and see exactly what the agent was doing. From alert to root cause in under 2 minutes."*

**Key message:** Errors + logs + traces in one place, correlated. No pivoting between tools.

---

## 🎬 Act 6 — Scale It: All Four Agents (2 min)

**Go back to:** APM Services list

> *"We've been looking at one agent. You have four — and each one has its own error patterns, model mix, and cost profile. Switch to `code-review-agent`: it's running CrewAI with Claude Sonnet for the heavy review passes and gpt-4o-mini for the pre-checks. Error profile is different: CalledProcessError from the code sandbox, rather than rate limits."*

> *"The customer-support agent is your highest-volume service — 200 transactions per day, mostly Claude Haiku, tight latency SLA. The data-analyst agent has the highest token cost per call because it's doing multi-step pandas + LLM narration on large datasets."*

> *"One Elasticsearch cluster. One APM integration. Four completely different AI architectures — all visible in the same UI, comparable side by side."*

---

## 🎬 Act 7 — The Pitch (2 min)

> *"Let's talk about what you just saw. This is all built on OpenTelemetry — specifically the gen_ai.* semantic conventions. Any language, any framework. If your team already uses OTel for their microservices, adding LLM observability is additive instrumentation. No new agents, no new ingestion pipelines."*

> *"And critically: you own this data. It's in Elasticsearch. You can write arbitrary ES|QL queries against it, set alert rules on token costs, build SLOs on model error rates, join LLM traces to your business event data. That's something no SaaS LLM obs tool can give you."*

**Three takeaways to leave on screen:**
1. **Same agent, richer signal** — APM + gen_ai.* attributes = token economics for free
2. **One platform** — traces, logs, errors, metrics, dashboards, alerts all from one cluster
3. **Open standard** — OTel gen_ai.* means no vendor lock-in on the instrumentation side

---

## 💬 Q&A Handling

| Question | Answer |
|---|---|
| *"Can I see actual conversation messages?"* | Yes — stored in span labels `gen_ai_input_messages` / `gen_ai_output_messages`. Click any LLM span → Labels. For a full conversation view, filter Discover by `labels.gen_ai_conversation_id`. |
| *"How much does ingestion cost?"* | APM spans are very small — a typical agent trace is ~30 KB with all attributes. 500 requests/day = ~15 MB/day. Serverless pricing scales with actual data. |
| *"Do you support streaming responses?"* | OTel gen_ai.* supports streaming via span events. We can show that as a follow-up with actual instrumented code. |
| *"What about self-hosted models (Ollama, vLLM)?"* | Same OTel instrumentation works. The `gen_ai.provider.name` attribute just says "ollama" or "vllm". |
| *"How do we correlate AI errors to user complaints?"* | Add `gen_ai.conversation_id` to your spans — it links every API call in a user session. Search by conversation ID in Discover. |
| *"How is this different from Datadog LLM Obs?"* | Ownership: your data stays in your cluster. Flexibility: ES|QL lets you build any analysis, not just what Datadog pre-built. Integration: traces, logs, metrics, and AI data in one system instead of three. Cost: no per-token ingestion surcharge from a third party. |

---

## 📊 Key Numbers to Quote

| Metric | Value |
|---|---|
| Agent services monitored | 4 (LangGraph, CrewAI, AutoGen, LangChain) |
| Model providers | 2 (OpenAI, Anthropic) |
| Models tracked | gpt-4o, gpt-4o-mini, claude-3-5-sonnet, claude-3-5-haiku |
| External dependencies | 6 (OpenAI, Anthropic, Elasticsearch, Code Executor, Web, Tools) |
| OTel gen_ai.* attributes captured | 30+ per span |
| APM tabs fully populated | 5/5 (Overview, Transactions, Dependencies, Errors, Logs) |
| Time to add LLM obs to existing OTel app | ~1 hour of instrumentation |

---

## 🔑 Navigation Reference

| What to show | Where to go |
|---|---|
| Service list | APM → Services |
| Trace waterfall | APM → Service → Transaction → click any row |
| Conversation messages | Click any `chat` span → Labels tab |
| Dependencies per service | APM → Service → Dependencies |
| Full topology | APM → Service Map |
| Error grouping | APM → Service → Errors |
| Correlated logs | APM → Service → Logs |
| Token cost dashboard | Dashboards → "LLM Observability — ai-research-agent" |
| Custom ES|QL query | Dev Tools: `FROM traces-apm-default | WHERE service.name == "ai-research-agent" ...` |

---

*Script created 2026-08-20 — Kibana serverless*
