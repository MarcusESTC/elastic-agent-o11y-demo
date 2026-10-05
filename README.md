# Agent Studio - Elastic + OpenTelemetry

A live Gemini chat demo with a compact scenario picker, conversation, and execution timeline. Open **http://localhost:5601** after starting it.

## Before you start

You must supply your own credentials and deployment:

- **Gemini API key:** set `GEMINI_API_KEY` in your private `web_agent/.env` file. The demo calls Gemini directly for model responses.
- **Elastic deployment and API key:** set `ES_ENDPOINT`, `ES_API_KEY`, `KIBANA_ENDPOINT`, and `OTEL_EXPORTER_OTLP_ENDPOINT` in that file. The deployment must support the Elasticsearch `redact` processor and the setup permissions described below. Elastic settings can also be updated through **Connection** in the UI after startup.

No API keys or real deployment endpoints are bundled with this repository. Copy `web_agent/.env.example` to `web_agent/.env` and replace its placeholders with your own values. The private `.env` file is ignored by Git; never commit it. The UI's **Connection** panel configures Elastic only, so enter the Gemini key in the private environment file.

## Run

```bash
pip install google-genai opentelemetry-sdk opentelemetry-exporter-otlp-proto-http psutil
cp web_agent/.env.example web_agent/.env
# Fill in the private credentials and endpoint URLs.
python3 scripts/setup-ingestion.py
bash start.sh
```

For an isolated test instance: `bash start.sh --port 5602`.
Docker is optional: `cd web_agent && docker compose up --build`. The Docker Collector sidecar configuration is separate from the directly instrumented app.

## What the demo shows

- Separate spans for each model invocation and tool attempt, plus exact trace and correlated-log links.
- Six scenarios: success, injected delay, injected timeout/recovery, PII obfuscation, real document search, and model comparison.
- Real Elasticsearch retrieval from a configured knowledge index, with links to source documentation. Without a search key/index it explicitly uses bundled reference documents.
- Estimated standard Gemini API cost per model, including provider-reported thinking tokens; feedback counted once per response.
- Actual exporter acknowledgements for traces, logs and metrics; real host CPU, memory and network measurements.
- Successful requests with synthetic PII: Elasticsearch masks stored conversation, tool and error telemetry during ingestion. Gemini and the chat UI receive the original content.

This is a custom demo built with the upstream OTel Python SDK, not Elastic Agent Builder or an EDOT SDK. Fault injection and public reference data are labeled.

## Connections

Open **Connection** in the UI header to configure the Elasticsearch endpoint and encoded API key. Kibana and managed OTLP URLs are filled in for the standard Elastic Cloud URL format; edit them under **Advanced endpoints** when needed. **Test connection** checks authentication, the ingest pipeline, protected templates/backing indices, knowledge access and all three OTLP routes without saving. **Save & reconnect** repeats those checks, saves the settings and restarts the server when they change. Existing runs must finish first; the next run starts a new conversation.

Leave the key blank to preserve existing credentials. Changing the Elasticsearch or OTLP destination requires entering a key. Saved keys are never sent back to the browser or kept in browser storage. The settings API is restricted to localhost with same-origin and CSRF checks. Local settings are saved atomically in the ignored `web_agent/.env` with mode `0600`, preserving the Gemini key and other settings. For Docker, persist the file in a private volume and keep container environment overrides consistent with it.

The destination must have the demo ingest pipeline and dedicated trace/log templates. Run `python3 scripts/setup-ingestion.py` once with administrative pipeline/template/index permissions. Runtime configuration checks need `read_pipeline`, permission to read index templates (`manage_index_templates`), and `view_index_metadata` on the demo streams; the saved-copy view needs `read` on the demo traces. Knowledge retrieval needs `read` on its index. Managed OTLP export requires `event:write` for the `apm` application. The UI remains available if configuration checks fail, but new runs are blocked until the destination is configured.

`GEMINI_API_KEY` authenticates model calls. `ES_API_KEY` authenticates managed ingestion. All OTel signals use `OTEL_EXPORTER_OTLP_ENDPOINT` with `/v1/traces`, `/v1/metrics`, and `/v1/logs`. `KIBANA_ENDPOINT` controls the UI links.

`ES_ENDPOINT` is the direct Elasticsearch API URL. `ES_READ_API_KEY` (falling back to `ES_API_KEY` for configuration/proof) reads configuration and stored traces; it also performs knowledge searches when `ES_KNOWLEDGE_INDEX` is configured. No keys are exposed by the frontend. The previous `ES_REDACT_PIPELINE`, `ES_REDACT_API_KEY` and `ES_PII_INDEX` settings are no longer used.

The managed bulk URL ending in `/_es` is a separate logs-only input, not the Elasticsearch query API or an OTLP prefix. [Elastic managed bulk documentation](https://www.elastic.co/docs/reference/opentelemetry/managed-inputs/elasticsearch-bulk).

## Telemetry and privacy

Native signals are routed using `data_stream.dataset=agentic_demo` and namespace `default`. Traces land in `traces-agentic_demo.otel-default`, logs and span events in `logs-agentic_demo.otel-default`, and metrics in `metrics-agentic_demo.otel-default`. These streams are dedicated to this app; existing generic streams and other services are unchanged.

`agentic-demo-otel-redact`, defined in `config/pii-redact-otel.json`, is the mandatory `index.final_pipeline` on the demo trace/log streams. Their index templates retain Elastic's native OTel components and apply the final pipeline to future backing indices. The pipeline processes conversation JSON, tool arguments/results, log bodies/previews, span status messages, and exception content, then records `attributes.privacy.*` as proof. Numeric measurements and correlation IDs are preserved. It uses Elastic's `redact` processor with `skip_if_unlicensed: false`; a processor failure prevents normal indexing. Pattern matching is not complete PII detection or card validation.

The app sends ordinary OTLP without calling a redaction API. It makes no `_simulate` requests and no separate chat-log writes. **Gemini, local history and the conversation UI receive original content.** This is ingestion privacy, not a model-input guardrail. Read-only startup/settings checks inspect pipeline bindings, not user text. Runtime telemetry failures remain separate from application success; an OTLP acknowledgment alone does not prove successful indexing.

Root and model spans include `gen_ai.input.messages`, `gen_ai.output.messages`, and `gen_ai.system_instructions` for Kibana's **GenAI → Conversation** panel. Tool exchanges and history are retained. Stored fields are masked by the final pipeline; pre-existing records are not rewritten.

After a run, expand **Elastic copy · redacted during ingestion**, then choose **View Elastic copy**. This performs a read-only search of the already-indexed root span. It shows the stored question and answer only when pipeline metadata is present, or explicitly reports pending/unverified data. It never sends the prompt through a redaction API.

**How it works** opens two updated network diagrams: **Conversation** shows browser ↔ app ↔ Gemini with original text; **Telemetry & storage** shows app → managed OTLP → Elasticsearch final pipeline → masked native streams → Kibana. Numeric metrics also use managed OTLP. Close with × or Escape.

## Guides and tests

- [Demo walkthrough](docs/AGENT-STUDIO-DEMO.md)
- [Copyable PII configuration](docs/PII-Redaction-Elastic.md)
- [Historical endpoint change](docs/ENDPOINT-CHANGE-2026-09-24.md) — superseded deployment

```bash
python3 -m unittest discover -s tests -v
# Uses synthetic PII through real OTLP, then reads indexed _source:
python3 scripts/verify-ingestion.py
```

Offline tests cover original model/UI content, absence of extra redaction requests, actual tool exchanges, retries, safe arithmetic, duplicate feedback, configuration checks and stored-copy access. The live ingestion test covers card/email/SSN/phone/CVV replacements in spans, logs, exceptions and tool content, unchanged non-PII text, and intact IDs. Cost estimates use [Google standard text API pricing](https://ai.google.dev/gemini-api/docs/pricing); free-tier allowances, caching and external tool charges are excluded.

## Implementation

`web_agent/server.py` starts `studio.py`. `telemetry.py` configures signals and health, `privacy.py` defines the ingestion contract, `connection.py` resolves endpoint URLs, `knowledge.json` holds reference summaries, and `static/` contains the interface. Local keys and `.runtime/` are ignored by Git.

Keep actual deployment endpoints and API keys in the ignored private environment file. Public documentation and examples must use placeholders; tests use fictional deployment names. Do not commit local connection files, screenshots of connection settings, or runtime verification output.

Historical material is retained for reference: `docs/demo_script.md` and `presentation/index.html` describe the earlier UI and pre-model guardrails; `config/pii-redact-ecs.json` and `scripts/build-pii-onepager.py` describe the superseded chat-log route. The optional `monitor/` helpers target the older generic streams. Use the walkthrough and ingestion configuration above for the current demo. Generated PDFs in `output/` are local artifacts and are not published.

Failure-store capture is explicitly disabled for these dedicated demo trace/log streams and their templates, so failed processing cannot retain the original document in a failure index. Redaction failures reject the telemetry document; they do not roll back a successful model response. Monitor ingestion failures separately.
