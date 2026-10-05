# Elasticsearch ingestion redaction for Agent Studio

The current flow is:

- Conversation: browser ↔ Agent Studio ↔ Gemini, using original text.
- Telemetry: OpenTelemetry SDK → managed OTLP → Elasticsearch final ingest pipeline → masked trace/log data streams → Kibana.

There is no separate redaction API request. The app does not call `_simulate` and does not write an extra chat-log copy. Redaction protects stored telemetry; it does not change what Gemini or the local UI sees.

## Install

Configure `web_agent/.env`, then run:

```bash
python3 scripts/setup-ingestion.py
```

The script installs [the pipeline](../config/pii-redact-otel.json), clones the deployment's current native OTel templates for the narrow `traces-agentic_demo.otel-*` / `logs-agentic_demo.otel-*` patterns, and sets `index.final_pipeline` on current demo backing indices. It preserves the native mappings and leaves other services and generic streams alone.

Pipeline: `agentic-demo-otel-redact`. Runtime streams: `traces-agentic_demo.otel-default` and `logs-agentic_demo.otel-default`.

## Fields and behavior

The pipeline runs Elastic's `redact` processor over exported GenAI question/answer/instruction JSON, tool arguments/results, log bodies/previews, status messages and exception content. Temporary working fields are removed before indexing. `attributes.privacy.processor`, `privacy.mode`, `privacy.pipeline`, `privacy.action` and `privacy.redacted_fields` describe processing. The last field counts changed content fields, not individual matches.

Patterns replace card-like digit sequences, email, SSN, phone and labelled CVV values. IDs and numerical measurements remain unchanged. The rules are a demonstration, not comprehensive PII detection or card validation. The processor requires a supporting license; `skip_if_unlicensed` stays false. No failure handler stores the original document in the normal target index when processing fails.

## Verify

```bash
python3 scripts/verify-ingestion.py
```

This sends synthetic traces, span events and logs over real OTLP and queries the stored `_source`. It verifies all configured patterns, tool content, errors, unchanged no-match text and intact IDs. It does not use pipeline simulation.

In the UI, run **Obfuscate in Elastic**, expand **Elastic copy · redacted during ingestion**, and click **View Elastic copy**. That button reads the stored trace and displays its masked question/answer. Retry if indexing is still pending. **How it works** shows both current paths.

## Settings and lifecycle

Connection settings read the installed pipeline and final-pipeline bindings; they do not redact a canary or user content. New runs are blocked if required configuration is missing. During a run, telemetry delivery and indexing failures are distinct from model success. Existing stored documents are not retroactively changed. The older `agentic-demo-pii-redact` pipeline and `agentic-demo-chat-logs` records remain historical and receive no new writes from the app.

References: [Elastic redact processor](https://www.elastic.co/docs/reference/ingest-processor/redact-processor), [final pipeline](https://www.elastic.co/docs/reference/elasticsearch/index-settings/index-modules), [custom OTel ingest pipelines](https://www.elastic.co/docs/reference/opentelemetry/compatibility/limitations), [managed OTLP](https://www.elastic.co/docs/reference/opentelemetry/managed-inputs/managed-otlp-endpoint).

Failure-store capture is explicitly disabled for these dedicated demo trace/log streams and their templates, so failed processing cannot retain the original document in a failure index. Redaction failures reject the telemetry document; they do not roll back a successful model response. Monitor ingestion failures separately.
