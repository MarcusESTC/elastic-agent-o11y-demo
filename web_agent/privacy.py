"""Ingestion contract: masking runs only in Elasticsearch, never in this app."""
PROCESSOR = 'elasticsearch-redact'
PIPELINE = 'agentic-demo-otel-redact'
DATASET = 'agentic_demo'
TRACE_STREAM = 'traces-agentic_demo.otel-default'
LOG_STREAM = 'logs-agentic_demo.otel-default'
