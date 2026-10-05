# Agent Studio: 8-minute walkthrough

Open **http://localhost:5601**. The app uses live Gemini calls, real arithmetic, and Elasticsearch search over a small collection of curated public Elastic references. It is a custom instrumented application, not Elastic Agent Builder. The telemetry SDK is upstream OpenTelemetry.

Use **Connection** in the header to enter an Elastic endpoint and API key. **Test connection** checks the destination; **Save & reconnect** applies verified settings when no run is active. Keep the key field empty to retain the saved key at the same destination. **Advanced endpoints** exposes Kibana and managed OTLP URLs. Existing demo resources must be installed on a new destination before saving.

**Deployment:** Configure your own endpoints in the private `web_agent/.env` or through **Connection**, then use **Open Elastic** to reach the configured Kibana deployment. Real deployment URLs and credentials must remain outside the repository. The demo uses ingestion-time redaction on dedicated OTel streams. Live redaction coverage is saved locally in `.runtime/ingest-redaction/coverage-indexed.json`. Earlier readiness results describe the previous redaction route.

1. **Successful run (1 minute).** Choose **Successful run** from the Scenario dropdown and Run. Point to each model call and the calculator step. Open **View trace** to follow the same run in APM, then **GenAI → Conversation** to show the question and answer. Each model span also records its input history and tool exchange. Use **View logs** for its trace-filtered native OTel logs. Traces created before the October 5 conversation-field fix retain their content in logs; run a new scenario for the GenAI conversation panel.
2. **Find the bottleneck (1 minute).** The calculator receives a clearly labeled 3-second injected delay. Identify the long tool step in the timeline and waterfall. This is controlled demo behavior, not a customer incident.
3. **Failure → recovery (1 minute).** The first tool attempt has an injected timeout; one retry succeeds. The failed tool span remains visible while the overall agent transaction succeeds. Separate a recovered dependency error from a failed customer request.
4. **Obfuscate in Elastic (2 minutes).** Run the synthetic card/email scenario. The original question remains visible in the local conversation and goes directly to Gemini. Expand **Elastic copy · redacted during ingestion**, then click **View Elastic copy** to show the masked question and answer read from the indexed trace. Open **View trace → GenAI → Conversation** to show the same masked content in Kibana. Dedicated trace/log streams run the `agentic-demo-otel-redact` final ingest pipeline. There is no pre-model redaction step, `_simulate` call, or separate chat-log write. Use **How it works** to show both updated diagrams. Do not call this complete PII detection or protection of model input.
5. **Search the evidence (1 minute).** The actual `search_knowledge_base` tool searches `agentic-demo-knowledge`; results link to official documentation. These are curated reference summaries, not an unrestricted search of the customer's data.
6. **Compare models (2 minutes).** The same prompt runs against Flash and Flash-Lite in independent conversations. Compare latency, tokens and estimated standard text API cost, then inspect both traces. One pair of runs is illustrative, not a performance benchmark. Feedback is recorded once per run and correlated with its trace.

The right-hand delivery badges reflect exporter acknowledgements, not a guarantee that Elasticsearch has indexed the latest event. Metrics export every 30 seconds. Exact trace links include a fixed time window; allow a few seconds for indexing.

## PII behavior

Matching synthetic PII is transformed during Elasticsearch ingestion and does not fail the application transaction. The pipeline covers the exported conversation fields, tool arguments/results, log bodies/previews and exception content. It preserves trace/span IDs and numerical telemetry. `skip_if_unlicensed` is false; pipeline errors prevent normal indexing. The model, local history and UI receive original text. Existing indexed records are not rewritten.

Native OTel ingestion remains on the managed OTLP endpoint, with dedicated `traces-agentic_demo.otel-default` and `logs-agentic_demo.otel-default` streams. Their templates and current backing indices require the final pipeline. There is no separate redaction API call. Export acceptance and completed indexing are distinct; the saved-copy button reports pending data and can be retried.

## Run and verify

```bash
python3 scripts/setup-ingestion.py  # one-time setup, with admin permissions
bash start.sh
python3 -m unittest discover -s tests -v
python3 scripts/verify-ingestion.py # synthetic data through real OTLP
```

Keep keys in `web_agent/.env` (ignored by Git), never in the browser. See the README for configuration-check, query and ingest privileges. Legacy `ES_REDACT_*` and `ES_PII_INDEX` settings are ignored. The current configuration guide is [PII-Redaction-Elastic.md](PII-Redaction-Elastic.md); older PDFs describe the superseded separate chat-log route.

The simplified UI keeps the timeline visible. Expand **Run details** for tokens, estimated cost and feedback; expand **Delivery & connection** for signal status.
