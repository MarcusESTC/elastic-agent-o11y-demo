"""OTel traces, logs, metrics and exporter health for Agent Studio."""

import os, sys, re, time, json, uuid, random, threading, socket, logging
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from connection import load_environment, endpoints
from privacy import DATASET

load_environment()

try:
    import psutil
    _PSUTIL = True
except ImportError:
    _PSUTIL = False

from google import genai
from google.genai import types

# ── OpenTelemetry ──────────────────────────────────────────────────────────────
from opentelemetry import trace, metrics
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader, MetricExportResult
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import SimpleLogRecordProcessor, LogExportResult
from opentelemetry.sdk.resources import Resource
from opentelemetry.trace import SpanKind, StatusCode
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.sdk._logs import LoggingHandler
from opentelemetry.metrics import Observation

# ── Config ────────────────────────────────────────────────────────────────────

GEMINI_KEY  = os.environ["GEMINI_API_KEY"]
ES_ENDPOINT = os.environ.get("ES_ENDPOINT", "")
ES_API_KEY  = os.environ.get("ES_API_KEY", "")

MODEL_A   = os.environ.get("GEMINI_MODEL",   "gemini-2.5-flash")   # primary (70%)
MODEL_B   = os.environ.get("GEMINI_MODEL_B", "gemini-2.5-flash-lite")  # cheaper variant (30%)
AB_RATIO  = float(os.environ.get("AB_RATIO", "0.3"))               # fraction routed to B

SVC_NAME  = "gemini-demo-agent"
HOST_NAME = os.environ.get("HOST_NAME", socket.gethostname())
PORT      = int(os.environ.get("PORT", "5601"))
METRICS_INTERVAL_S = int(os.environ.get("METRICS_INTERVAL", "30"))

# All three signals use the configured managed OTLP endpoint.
# /_es is the separate managed bulk path, not an OTLP URL prefix.
try:
    _ENDPOINTS = endpoints()
except ValueError:
    # Keep the UI available for first-time Elastic setup or correction.
    _ENDPOINTS = {name: '' for name in ('otlp','traces','metrics','logs','kibana')}
OTLP_TRACES_EP  = _ENDPOINTS['traces']
OTLP_METRICS_EP = _ENDPOINTS['metrics']
OTLP_LOGS_EP    = _ENDPOINTS['logs']
OTLP_HEADERS    = {"Authorization": f"ApiKey {ES_API_KEY}"}
OTLP_ENDPOINT   = _ENDPOINTS['otlp']
KB_ENDPOINT     = _ENDPOINTS['kibana']

# Export acknowledgements are distinct from indexing confirmation.
_export_lock = threading.Lock()
_export_state = {s: {"state": "waiting", "last_success": None, "last_attempt": None, "batches": 0} for s in ("traces", "logs", "metrics")}

def _track(signal, result):
    ok = getattr(result, "name", "") == "SUCCESS"
    now = time.time()
    with _export_lock:
        item = _export_state[signal]
        item.update(state="accepted" if ok else "failed", last_attempt=now)
        if ok:
            item["last_success"] = now
            item["batches"] += 1
    return result

def export_health():
    with _export_lock:
        return {k: dict(v, age_seconds=round(time.time()-v["last_success"]) if v["last_success"] else None) for k,v in _export_state.items()}

class TrackedSpanExporter(OTLPSpanExporter):
    def export(self, spans):
        if not OTLP_ENDPOINT or not ES_API_KEY: return _track('traces', SpanExportResult.FAILURE)
        try: return _track("traces", super().export(spans))
        except Exception:
            _track("traces", None)
            raise

class TrackedMetricExporter(OTLPMetricExporter):
    def export(self, metrics_data, timeout_millis=10000, **kwargs):
        if not OTLP_ENDPOINT or not ES_API_KEY: return _track('metrics', MetricExportResult.FAILURE)
        try: return _track("metrics", super().export(metrics_data, timeout_millis=timeout_millis, **kwargs))
        except Exception:
            _track("metrics", None)
            raise

class TrackedLogExporter(OTLPLogExporter):
    def export(self, batch):
        if not OTLP_ENDPOINT or not ES_API_KEY: return _track('logs', LogExportResult.FAILURE)
        try: return _track("logs", super().export(batch))
        except Exception:
            _track("logs", None)
            raise

# ── OTel Resource ─────────────────────────────────────────────────────────────

resource = Resource.create({
    "service.name":            SVC_NAME,
    "service.version":         "3.0.0",
    "data_stream.dataset":     DATASET,
    "data_stream.namespace":   "default",
    "deployment.environment":  "demo",
    "service.instance.id":     HOST_NAME,
    "host.name":               HOST_NAME,
    "host.hostname":           HOST_NAME,
    "host.arch":               "arm64" if "arm" in os.uname().machine else "x86_64",
    "os.type":                 "darwin" if sys.platform == "darwin" else "linux",
    "process.pid":             os.getpid(),
    "process.executable.name": "python3",
    "telemetry.sdk.name":      "opentelemetry",
    "telemetry.sdk.language":  "python",
})

# ── Traces ────────────────────────────────────────────────────────────────────

trace_exporter = TrackedSpanExporter(endpoint=OTLP_TRACES_EP, headers=OTLP_HEADERS)
trace_provider = TracerProvider(resource=resource)
trace_provider.add_span_processor(SimpleSpanProcessor(trace_exporter))
trace.set_tracer_provider(trace_provider)
tracer = trace.get_tracer(SVC_NAME, "3.0.0")

# ── Metrics ───────────────────────────────────────────────────────────────────

metric_exporter = TrackedMetricExporter(endpoint=OTLP_METRICS_EP, headers=OTLP_HEADERS)
metric_reader   = PeriodicExportingMetricReader(
    metric_exporter, export_interval_millis=METRICS_INTERVAL_S * 1000)
meter_provider  = MeterProvider(resource=resource, metric_readers=[metric_reader])
metrics.set_meter_provider(meter_provider)
meter = metrics.get_meter(SVC_NAME, "3.0.0")

# gen_ai semantic convention instruments
token_counter = meter.create_counter(
    "gen_ai.client.token.usage", unit="{token}",
    description="Tokens used by the generative AI model")
op_duration = meter.create_histogram(
    "gen_ai.client.operation.duration", unit="s",
    description="Duration of generative AI operations")
agent_duration = meter.create_histogram(
    "gen_ai.agent.duration", unit="s",
    description="End-to-end agent turn duration")
error_counter = meter.create_counter(
    "gen_ai.client.errors", unit="{error}",
    description="Errors from the generative AI client")

# Tool call instruments
tool_calls_counter = meter.create_counter(
    "gen_ai.tool.calls", unit="{call}",
    description="Number of tool calls made by the agent")
tool_duration = meter.create_histogram(
    "gen_ai.tool.duration", unit="s",
    description="Duration of individual tool executions")

# Feedback instruments
feedback_counter = meter.create_counter(
    "gen_ai.feedback", unit="{response}",
    description="User feedback on agent responses (positive/negative)")

# System / process observable instruments (psutil callbacks)
if _PSUTIL:
    _cpu_pct = _mem_pct = _proc_cpu = 0.0
    _mem_bytes = _proc_mem = _net_in = _net_out = 0

    def _cb_cpu_util(opts):  yield Observation(_cpu_pct, {"system.cpu.state": "user"})
    def _cb_mem_util(opts):  yield Observation(_mem_pct, {})
    def _cb_mem_used(opts):  yield Observation(_mem_bytes, {})
    def _cb_proc_cpu(opts):  yield Observation(_proc_cpu, {"process.pid": str(os.getpid())})
    def _cb_proc_mem(opts):  yield Observation(_proc_mem, {"process.pid": str(os.getpid())})
    def _cb_net_in(opts):    yield Observation(_net_in,  {"network.io.direction": "receive"})
    def _cb_net_out(opts):   yield Observation(_net_out, {"network.io.direction": "transmit"})

    meter.create_observable_gauge("system.cpu.utilization",   callbacks=[_cb_cpu_util], unit="1")
    meter.create_observable_gauge("system.memory.utilization",callbacks=[_cb_mem_util], unit="1")
    meter.create_observable_up_down_counter("system.memory.usage",  callbacks=[_cb_mem_used], unit="By")
    meter.create_observable_gauge("process.cpu.utilization",  callbacks=[_cb_proc_cpu], unit="1")
    meter.create_observable_up_down_counter("process.memory.usage", callbacks=[_cb_proc_mem], unit="By")
    meter.create_observable_counter("system.network.io",
        callbacks=[_cb_net_in, _cb_net_out], unit="By")

# ── Logs ──────────────────────────────────────────────────────────────────────

log_exporter = TrackedLogExporter(endpoint=OTLP_LOGS_EP, headers=OTLP_HEADERS)
log_provider = LoggerProvider(resource=resource)
log_provider.add_log_record_processor(SimpleLogRecordProcessor(log_exporter))

otel_handler = LoggingHandler(level=logging.DEBUG, logger_provider=log_provider)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(SVC_NAME)
logger.addHandler(otel_handler)
logger.propagate = False


def host_loop():
    if not _PSUTIL: return
    global _cpu_pct, _mem_pct, _mem_bytes, _proc_cpu, _proc_mem, _net_in, _net_out
    process = psutil.Process()
    psutil.cpu_percent(interval=None)
    while True:
        try:
            _cpu_pct = psutil.cpu_percent(interval=None) / 100.0
            memory = psutil.virtual_memory()
            _mem_pct, _mem_bytes = memory.percent / 100.0, memory.used
            _proc_cpu, _proc_mem = process.cpu_percent(interval=None) / 100.0, process.memory_info().rss
            net = psutil.net_io_counters()
            _net_in, _net_out = net.bytes_recv, net.bytes_sent
        except (psutil.Error, OSError):
            pass
        time.sleep(METRICS_INTERVAL_S)
