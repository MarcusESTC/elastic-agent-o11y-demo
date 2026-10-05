# Agentic O11y endpoint update — September 24, 2026

> **Historical deployment.** The current ingestion architecture is documented in [Agent Studio: 8-minute walkthrough](AGENT-STUDIO-DEMO.md). The verification below records the September 24 deployment only. Actual deployment addresses and credentials are intentionally omitted.

On September 24, the live Gemini demo was configured to send traces, logs and metrics to an Elastic deployment, with the local UI at [localhost:5601](http://localhost:5601).

- Kibana is configured with `KIBANA_ENDPOINT`; use **Open Elastic** in the app.
- Managed OTLP is configured with `OTEL_EXPORTER_OTLP_ENDPOINT`.
- The managed bulk input uses the managed ingestion host with the `/_es` suffix.
- Direct Elasticsearch queries use `ES_ENDPOINT`.
- Credentials remain in the ignored `web_agent/.env`, with mode 0600.

## What changed

The server and Docker configuration use an explicit managed OTLP endpoint for all three signals. Logs no longer use the old APM host. Kibana links use the supplied Kibana URL. The shared connection helper removes the `/_es` suffix if a managed bulk URL is supplied as the OTLP base. Local launch and legacy helper scripts read the updated environment; old cluster/credential defaults were removed from the legacy live-agent and dashboard helpers.

The `/_es` input is for bulk logs, so the live OTel SDK sends to the sibling `/v1/traces`, `/v1/metrics` and `/v1/logs` paths. [Elastic documentation](https://www.elastic.co/docs/reference/opentelemetry/managed-inputs/elasticsearch-bulk).

## Verification

All three OTLP endpoints returned HTTP 200. A real Gemini request invoked `calculate` for `2 + 2` and returned `4`. Its trace and three correlated log records were visible in Kibana. A read-only search in Kibana Dev Tools verified 142 documents in the generic OTel metrics stream and five in the generic OTel logs stream at the time of checking. Metrics were current through 17:30:25 UTC. Python syntax, endpoint normalization and launcher checks passed.

The supplied key is accepted for ingestion but returns HTTP 403 for direct Elasticsearch reads. CLI health queries and setup tools may need appropriately scoped read/setup credentials. Indexed data was verified through the existing signed-in Kibana account. Historical data, dashboards and rules from the old deployment were not migrated as part of this endpoint change.

## Start again

From this project folder:

```bash
bash start.sh
```

The process sends host metrics every 30 seconds while running. Chat requests create agent/tool/model traces and correlated logs.
