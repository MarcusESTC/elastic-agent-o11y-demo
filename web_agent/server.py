#!/usr/bin/env python3
"""
web_agent/server.py — Full OTel Gemini chat agent → Elastic via OTLP

ALL signals flow through the OpenTelemetry SDK and ship via OTLP/HTTP to the
Elastic ingest endpoint — exactly like a production service running EDOT.

  📡 Traces  → OTel TraceProvider  → OTLP → traces-generic.otel-default
  📋 Logs    → OTel LoggerProvider → OTLP → logs-generic.otel-default
  📊 Metrics → OTel MeterProvider  → OTLP → metrics-generic.otel-default
               (gen_ai.* semantic conventions + system/process metrics via psutil)

Kibana shows:
  APM  → Services → gemini-demo-agent  (Transactions, Trace waterfall, Logs, Deps)
  Infra → Hosts   → <hostname>         (CPU, memory, network from real psutil)

Usage:
    export GEMINI_API_KEY="..."  ES_ENDPOINT="..."  ES_API_KEY="..."
    python3 web_agent/server.py
"""

import os, sys, time, json, uuid, threading, socket, logging
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

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
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, SimpleLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.trace import SpanKind, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.sdk._logs import LoggingHandler
from opentelemetry.metrics import Observation

# ── Config ────────────────────────────────────────────────────────────────────

GEMINI_KEY  = os.environ["GEMINI_API_KEY"]
ES_ENDPOINT = os.environ["ES_ENDPOINT"]
ES_API_KEY  = os.environ["ES_API_KEY"]
MODEL       = os.environ.get("GEMINI_MODEL",    "gemini-2.5-flash")
SVC_NAME    = "gemini-demo-agent"
HOST_NAME   = os.environ.get("HOST_NAME", socket.gethostname())
PORT        = int(os.environ.get("PORT", "5601"))
METRICS_INTERVAL_S = int(os.environ.get("METRICS_INTERVAL", "30"))

# OTLP endpoints — full paths required so the SDK doesn't mangle the URL join.
# Ingest endpoint handles traces + metrics via OTLP/HTTP.
# APM server endpoint handles logs via OTLP/HTTP.
_INGEST = ES_ENDPOINT.replace(".es.", ".ingest.")
_APM    = ES_ENDPOINT.replace(".es.", ".apm.")
OTLP_TRACES_EP  = f"{_INGEST}/v1/traces"
OTLP_METRICS_EP = f"{_INGEST}/v1/metrics"
OTLP_LOGS_EP    = f"{_APM}/v1/logs"
OTLP_HEADERS    = {"Authorization": f"ApiKey {ES_API_KEY}"}
OTLP_ENDPOINT   = _INGEST  # used only for display/config API

KB_ENDPOINT   = ES_ENDPOINT.replace(".es.", ".kb.")

SYSTEM_PROMPT = """You are a helpful, knowledgeable AI assistant powered by Google Gemini.
Answer any question clearly and concisely. Use markdown formatting where it helps readability.
Keep responses under 300 words unless more detail is genuinely needed."""

# ── OTel Resource ─────────────────────────────────────────────────────────────
# Identifies this service across all three signal types.

resource = Resource.create({
    "service.name":           SVC_NAME,
    "service.version":        "1.0.0",
    "deployment.environment": "demo",
    "service.instance.id":    HOST_NAME,
    "host.name":              HOST_NAME,
    "host.hostname":          HOST_NAME,
    "host.arch":              "arm64" if "arm" in os.uname().machine else "x86_64",
    "os.type":                "darwin" if sys.platform == "darwin" else "linux",
    "process.pid":            os.getpid(),
    "process.executable.name": "python3",
    "telemetry.sdk.name":     "opentelemetry",
    "telemetry.sdk.language": "python",
    "telemetry.distro.name":  "elastic",  # marks this as EDOT-compatible
})

# ── Traces ────────────────────────────────────────────────────────────────────

trace_exporter  = OTLPSpanExporter(endpoint=OTLP_TRACES_EP, headers=OTLP_HEADERS)
trace_provider  = TracerProvider(resource=resource)
trace_provider.add_span_processor(SimpleSpanProcessor(trace_exporter))
trace.set_tracer_provider(trace_provider)
tracer = trace.get_tracer(SVC_NAME, "1.0.0")

# ── Metrics ───────────────────────────────────────────────────────────────────
# PeriodicExportingMetricReader pushes metrics every METRICS_INTERVAL_S seconds.
# Instruments defined below are populated during chat turns and by psutil callbacks.

metric_exporter = OTLPMetricExporter(endpoint=OTLP_METRICS_EP, headers=OTLP_HEADERS)
metric_reader   = PeriodicExportingMetricReader(
    metric_exporter, export_interval_millis=METRICS_INTERVAL_S * 1000)
meter_provider  = MeterProvider(resource=resource, metric_readers=[metric_reader])
metrics.set_meter_provider(meter_provider)
meter = metrics.get_meter(SVC_NAME, "1.0.0")

# gen_ai semantic convention instruments
# https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-metrics/
token_counter = meter.create_counter(
    name="gen_ai.client.token.usage",
    unit="{token}",
    description="Number of tokens used by the generative AI model",
)
op_duration = meter.create_histogram(
    name="gen_ai.client.operation.duration",
    unit="s",
    description="Duration of generative AI operations",
)
error_counter = meter.create_counter(
    name="gen_ai.client.errors",
    unit="{error}",
    description="Number of errors from the generative AI client",
)

# System / process observable instruments (psutil callbacks)
if _PSUTIL:
    _cpu_pct   = 0.0
    _mem_pct   = 0.0
    _mem_bytes = 0
    _proc_cpu  = 0.0
    _proc_mem  = 0
    _net_in    = 0
    _net_out   = 0

    def _cb_cpu_util(opts):
        yield Observation(_cpu_pct, {"system.cpu.state": "user", "system.cpu.logical_number": 0})

    def _cb_mem_util(opts):
        yield Observation(_mem_pct, {})

    def _cb_mem_used(opts):
        yield Observation(_mem_bytes, {})

    def _cb_proc_cpu(opts):
        yield Observation(_proc_cpu, {"process.pid": str(os.getpid())})

    def _cb_proc_mem(opts):
        yield Observation(_proc_mem, {"process.pid": str(os.getpid())})

    def _cb_net_in(opts):
        yield Observation(_net_in, {"network.io.direction": "receive", "network.interface.name": "eth0"})

    def _cb_net_out(opts):
        yield Observation(_net_out, {"network.io.direction": "transmit", "network.interface.name": "eth0"})

    meter.create_observable_gauge(
        "system.cpu.utilization", callbacks=[_cb_cpu_util], unit="1",
        description="CPU utilization (0-1)")
    meter.create_observable_gauge(
        "system.memory.utilization", callbacks=[_cb_mem_util], unit="1",
        description="Memory utilization (0-1)")
    meter.create_observable_up_down_counter(
        "system.memory.usage", callbacks=[_cb_mem_used], unit="By",
        description="Memory usage in bytes")
    meter.create_observable_gauge(
        "process.cpu.utilization", callbacks=[_cb_proc_cpu], unit="1",
        description="Process CPU utilization (0-1)")
    meter.create_observable_up_down_counter(
        "process.memory.usage", callbacks=[_cb_proc_mem], unit="By",
        description="Process resident memory in bytes")
    meter.create_observable_counter(
        "system.network.io", callbacks=[_cb_net_in, _cb_net_out], unit="By",
        description="Network bytes received/transmitted")

# ── Logs ──────────────────────────────────────────────────────────────────────
# OTel LoggerProvider bridges Python's standard `logging` module.
# Any log.info/warning/error call is exported via OTLP, with trace context
# (trace_id, span_id) automatically injected when called inside an active span.

log_exporter   = OTLPLogExporter(endpoint=OTLP_LOGS_EP, headers=OTLP_HEADERS)
log_provider   = LoggerProvider(resource=resource)
log_provider.add_log_record_processor(SimpleLogRecordProcessor(log_exporter))

otel_handler = LoggingHandler(level=logging.DEBUG, logger_provider=log_provider)

# Attach to root logger so all log.* calls go through OTel
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(SVC_NAME)
logger.addHandler(otel_handler)
logger.propagate = False  # don't double-print to console handler

# ── Gemini client ─────────────────────────────────────────────────────────────

gemini = genai.Client(api_key=GEMINI_KEY)

# ── psutil scrape loop ────────────────────────────────────────────────────────

def _psutil_scrape_loop():
    """Update module-level gauge values that the OTel callbacks read."""
    if not _PSUTIL:
        return
    global _cpu_pct, _mem_pct, _mem_bytes, _proc_cpu, _proc_mem, _net_in, _net_out
    proc = psutil.Process(os.getpid())
    psutil.cpu_percent(interval=None)   # prime
    net_prev = (0, 0)
    while True:
        time.sleep(METRICS_INTERVAL_S)
        try:
            _cpu_pct   = round(psutil.cpu_percent(interval=None) / 100.0, 4)
            mem        = psutil.virtual_memory()
            _mem_pct   = round(mem.percent / 100.0, 4)
            _mem_bytes = mem.used
            with proc.oneshot():
                _proc_cpu = round(proc.cpu_percent(interval=None) / 100.0, 4)
                _proc_mem = proc.memory_info().rss
            net        = psutil.net_io_counters()
            _net_in    = max(0, net.bytes_recv - net_prev[0])
            _net_out   = max(0, net.bytes_sent - net_prev[1])
            net_prev   = (net.bytes_recv, net.bytes_sent)
        except Exception:
            pass


# ── Chat turn ─────────────────────────────────────────────────────────────────

def chat_turn(user_message: str, conversation_id: str, history: list) -> dict:
    """
    One turn = one OTel trace with two spans:

      invoke_agent  [SERVER]   ← root span → APM transaction
        └── chat {MODEL}  [CLIENT]  ← child span → APM span (type=external/llm)

    gen_ai.* semantic conventions:
      Span attributes:  gen_ai.operation.name, gen_ai.system, gen_ai.request.model,
                        gen_ai.usage.input_tokens, gen_ai.usage.output_tokens,
                        gen_ai.input.messages, gen_ai.output.messages,
                        gen_ai.conversation.id
      Metrics:          gen_ai.client.token.usage (counter, input+output)
                        gen_ai.client.operation.duration (histogram, seconds)
      Logs:             Emitted inside the active span → trace_id injected by OTel
    """
    t0 = time.time()
    trace_id = tx_id = None

    with tracer.start_as_current_span(
        "invoke_agent",
        kind=SpanKind.SERVER,
        attributes={
            "gen_ai.operation.name":  "invoke_agent",
            "gen_ai.system":          "google_gemini",
            "gen_ai.request.model":   MODEL,
            "gen_ai.conversation.id": conversation_id,
            "gen_ai.agent.name":      SVC_NAME,
        },
    ) as root:
        ctx = root.get_span_context()
        trace_id = format(ctx.trace_id, "032x")
        tx_id    = format(ctx.span_id,  "016x")

        logger.info(
            "Turn started",
            extra={
                "conversation_id": conversation_id,
                "model":           MODEL,
                "user_message_len": len(user_message),
            },
        )

        with tracer.start_as_current_span(
            f"chat {MODEL}",
            kind=SpanKind.CLIENT,
            attributes={
                "gen_ai.operation.name": "chat",
                "gen_ai.system":         "google_gemini",
                "gen_ai.request.model":  MODEL,
                "gen_ai.input.messages": user_message[:800],
                # peer.service + server.address are what Kibana APM uses to draw
                # the service map edge from gemini-demo-agent → google_gemini
                "peer.service":          "google_gemini",
                "server.address":        "generativelanguage.googleapis.com",
                "server.port":           443,
                "network.protocol.name": "https",
                "rpc.system":            "http",
                "rpc.service":           "google.ai.generativelanguage",
                "rpc.method":            "GenerateContent",
            },
        ) as llm:
            try:
                messages = [
                    types.Content(role=t["role"], parts=[types.Part(text=t["text"])])
                    for t in history
                ] + [types.Content(role="user", parts=[types.Part(text=user_message)])]

                response = gemini.models.generate_content(
                    model=MODEL,
                    contents=messages,
                    config=types.GenerateContentConfig(
                        system_instruction=SYSTEM_PROMPT,
                        temperature=0.7,
                        max_output_tokens=512,
                    ),
                )

                output_text = response.text
                usage       = response.usage_metadata
                in_tok      = getattr(usage, "prompt_token_count",     0) or 0
                out_tok     = getattr(usage, "candidates_token_count", 0) or 0

                # Span attributes (OTel gen_ai semantic conventions)
                llm.set_attribute("gen_ai.response.model",      MODEL)
                llm.set_attribute("gen_ai.usage.input_tokens",  in_tok)
                llm.set_attribute("gen_ai.usage.output_tokens", out_tok)
                llm.set_attribute("gen_ai.output.messages",     output_text[:800])
                llm.set_status(StatusCode.OK)

                # Span events — full request/response text, no truncation.
                # Visible in APM trace waterfall → span → Events tab.
                # OTel gen_ai semantic conventions:
                #   gen_ai.content.prompt      → the user message sent to the LLM
                #   gen_ai.content.completion  → the LLM response
                llm.add_event("gen_ai.content.prompt", {
                    "gen_ai.prompt": user_message,
                })
                llm.add_event("gen_ai.content.completion", {
                    "gen_ai.completion": output_text,
                })

                root.set_attribute("gen_ai.usage.input_tokens",  in_tok)
                root.set_attribute("gen_ai.usage.output_tokens", out_tok)
                root.set_status(StatusCode.OK)

                # Metrics (gen_ai semantic conventions)
                attrs = {
                    "gen_ai.operation.name": "chat",
                    "gen_ai.system":         "google_gemini",
                    "gen_ai.request.model":  MODEL,
                }
                token_counter.add(in_tok,  {**attrs, "gen_ai.token.type": "input"})
                token_counter.add(out_tok, {**attrs, "gen_ai.token.type": "output"})

                is_error = False

            except Exception as e:
                llm.set_status(StatusCode.ERROR, str(e))
                llm.record_exception(e)
                root.set_status(StatusCode.ERROR, str(e))
                logger.error(f"Gemini API error: {e}", exc_info=True,
                             extra={"model": MODEL, "conversation_id": conversation_id})
                error_counter.add(1, {"gen_ai.operation.name": "chat",
                                      "gen_ai.system": "google_gemini",
                                      "error.type": type(e).__name__})
                output_text = f"Sorry, I hit an error: {e}"
                in_tok = out_tok = 0
                is_error = True

    elapsed_s  = time.time() - t0
    elapsed_ms = elapsed_s * 1000
    cost_usd   = (in_tok * 0.075 + out_tok * 0.30) / 1_000_000

    # Duration histogram (outside spans so it captures full round-trip)
    op_duration.record(elapsed_s, {
        "gen_ai.operation.name": "chat",
        "gen_ai.system":         "google_gemini",
        "gen_ai.request.model":  MODEL,
        "gen_ai.response.model": MODEL,
        "error.occurred":        is_error,
    })

    logger.info(
        "Turn completed",
        extra={
            "conversation_id": conversation_id,
            "model":           MODEL,
            "in_tokens":       in_tok,
            "out_tokens":      out_tok,
            "latency_ms":      round(elapsed_ms),
            "cost_usd":        round(cost_usd, 6),
            "is_error":        is_error,
        },
    )

    return {
        "text":       output_text,
        "trace_id":   trace_id,
        "tx_id":      tx_id,
        "in_tokens":  in_tok,
        "out_tokens": out_tok,
        "latency_ms": round(elapsed_ms),
        "cost_usd":   round(cost_usd, 6),
        "is_error":   is_error,
        "transport":  "otlp",
        "apm_url":    f"{KB_ENDPOINT}/app/apm/services/{SVC_NAME}/transactions/view"
                      f"?transactionName=invoke_agent&transactionType=request",
    }


# ── In-memory sessions ────────────────────────────────────────────────────────

_sessions = {}
_lock     = threading.Lock()

def get_or_create_session(session_id=None):
    with _lock:
        if not session_id or session_id not in _sessions:
            session_id = uuid.uuid4().hex[:12]
            _sessions[session_id] = {"id": session_id, "conv_id": uuid.uuid4().hex[:8], "history": []}
        return session_id, _sessions[session_id]


# ── Web UI (embedded HTML) ────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Elastic AI Agent — Full OTel Demo</title>
<style>
  :root{
    --bg:#0d1117;--surface:#161b22;--border:#30363d;
    --text:#e6edf3;--text-2:#8b949e;--text-3:#6e7681;
    --blue:#58a6ff;--green:#3fb950;--yellow:#d29922;
    --red:#f85149;--elastic:#00bfb3;--elastic-2:#003d38;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;height:100vh;display:flex;flex-direction:column;overflow:hidden}

  header{background:var(--surface);border-bottom:1px solid var(--border);padding:12px 20px;display:flex;align-items:center;gap:14px;flex-shrink:0}
  .logo{font-size:18px;font-weight:700;color:var(--elastic)}
  .logo span{color:var(--text-2);font-weight:400;font-size:13px;margin-left:6px}
  .badges{display:flex;gap:8px;margin-left:auto}
  .badge{font-size:11px;border-radius:4px;padding:2px 8px;border:1px solid var(--border);color:var(--text-2)}
  .badge.otel{border-color:#7b61ff;color:#7b61ff}
  .badge.live{border-color:var(--elastic);color:var(--elastic)}
  .apm-btn{color:var(--blue);text-decoration:none;font-size:12px;border:1px solid var(--border);border-radius:4px;padding:3px 10px;transition:border-color .15s}
  .apm-btn:hover{border-color:var(--blue)}

  main{display:flex;flex:1;min-height:0}

  .chat-col{flex:1;display:flex;flex-direction:column;min-width:0}
  .messages{flex:1;overflow-y:auto;padding:20px;display:flex;flex-direction:column;gap:14px}
  .messages::-webkit-scrollbar{width:5px}
  .messages::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}

  .msg{display:flex;gap:10px;max-width:82%}
  .msg.user{align-self:flex-end;flex-direction:row-reverse}
  .avatar{width:30px;height:30px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:14px;flex-shrink:0;margin-top:2px}
  .msg.user .avatar{background:#1c2d3a;border:1px solid var(--blue)}
  .msg.agent .avatar{background:#001a18;border:1px solid var(--elastic)}
  .bubble{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:11px 15px;font-size:14px;line-height:1.65;word-break:break-word}
  .msg.user .bubble{background:#1c2d3a;border-color:var(--blue)}
  .bubble pre{background:#0d1117;border:1px solid var(--border);border-radius:6px;padding:10px;overflow-x:auto;font-size:12px;margin:8px 0}
  .bubble code{background:#111;border:1px solid var(--border);border-radius:3px;padding:1px 5px;font-size:12px}
  .bubble strong{color:var(--text)}

  .trace-row{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-left:40px;margin-top:-8px}
  .pill{font-size:11px;border-radius:4px;padding:2px 8px;border:1px solid}
  .pill.trace{border-color:var(--elastic);color:var(--elastic);text-decoration:none;cursor:pointer}
  .pill.trace:hover{background:var(--elastic-2)}
  .pill.dim{border-color:var(--border);color:var(--text-3)}
  .pill.err{border-color:var(--red);color:var(--red)}
  .pill.otlp{border-color:#7b61ff;color:#7b61ff}

  .typing{display:flex;align-items:center;gap:6px;padding:10px 15px;color:var(--text-3);font-size:13px}
  .dots{display:flex;gap:4px}
  .dot{width:6px;height:6px;border-radius:50%;background:var(--elastic);animation:bop .8s infinite alternate}
  .dot:nth-child(2){animation-delay:.15s}.dot:nth-child(3){animation-delay:.3s}
  @keyframes bop{to{transform:translateY(-4px);opacity:.4}}

  .input-row{border-top:1px solid var(--border);padding:14px 20px;display:flex;gap:10px;background:var(--surface);flex-shrink:0}
  textarea{flex:1;background:var(--bg);border:1px solid var(--border);border-radius:8px;color:var(--text);padding:10px 14px;font-size:14px;font-family:inherit;resize:none;height:42px;max-height:120px;line-height:1.5;transition:border-color .15s}
  textarea:focus{outline:none;border-color:var(--elastic)}
  .send-btn{background:var(--elastic);border:none;color:#000;border-radius:8px;padding:10px 20px;font-size:14px;font-weight:600;cursor:pointer;transition:opacity .15s}
  .send-btn:hover{opacity:.85}.send-btn:disabled{opacity:.35;cursor:not-allowed}
  .new-btn{background:transparent;border:1px solid var(--border);color:var(--text-2);border-radius:8px;padding:10px 14px;font-size:13px;cursor:pointer}
  .new-btn:hover{border-color:var(--text-2)}

  /* Telemetry sidebar */
  .tele{width:272px;border-left:1px solid var(--border);background:var(--surface);display:flex;flex-direction:column;overflow-y:auto;flex-shrink:0}
  .tele-hdr{padding:10px 14px;border-bottom:1px solid var(--border);font-size:11px;font-weight:700;color:var(--text-2);text-transform:uppercase;letter-spacing:.6px}
  .tele-sec{padding:12px 14px;border-bottom:1px solid var(--border)}
  .tele-lbl{font-size:10px;text-transform:uppercase;color:var(--text-3);letter-spacing:.5px;margin-bottom:7px}
  .kv{display:flex;justify-content:space-between;margin-bottom:5px;font-size:12px}
  .kv span:first-child{color:var(--text-2)}
  .kv span:last-child{color:var(--text);font-weight:500;font-variant-numeric:tabular-nums}
  .kv span.g{color:var(--green)}.kv span.b{color:var(--blue)}.kv span.y{color:var(--yellow)}.kv span.e{color:var(--elastic)}.kv span.r{color:var(--red)}
  .mono{font-size:10px;font-family:monospace;color:var(--text-3);word-break:break-all;margin-top:3px;line-height:1.4}
  .sig-row{display:flex;align-items:center;gap:7px;margin-bottom:5px;font-size:12px}
  .dot-s{width:7px;height:7px;border-radius:50%;flex-shrink:0}
  .dot-s.ok{background:var(--green)}.dot-s.dim{background:var(--text-3)}
  .sig-txt{color:var(--text-2)}
  .sig-sub{color:var(--text-3);font-size:10px}

  .welcome{text-align:center;padding:60px 30px;color:var(--text-2)}
  .welcome h2{color:var(--text);font-size:20px;margin-bottom:8px}
  .welcome p{font-size:13px;line-height:1.6;max-width:360px;margin:0 auto 10px}
  .welcome .tag{color:var(--elastic);font-size:12px}

  /* Data flow diagram */
  .flow{display:flex;flex-direction:column;align-items:center;gap:4px;margin-top:6px}
  .fnode{border:1px solid var(--border);border-radius:8px;padding:6px 10px;font-size:11px;text-align:center;width:100%;background:var(--bg)}
  .fnode.user-node{border-color:var(--blue);color:var(--blue)}
  .fnode.app-node{border-color:var(--elastic);color:var(--elastic)}
  .fnode.ingest-node{border-color:#7b61ff;color:#7b61ff}
  .fnode.kibana-node{border-color:var(--green);color:var(--green)}
  .fsub{color:var(--text-3);font-size:9px;margin-top:2px}
  .farrow{font-size:10px;color:var(--text-3)}
  .frow3{display:flex;gap:4px;width:100%}
  .fsmall{flex:1;border:1px solid var(--border);border-radius:6px;padding:5px 3px;font-size:10px;text-align:center;background:var(--bg);color:var(--text-2)}
</style>
</head>
<body>
<header>
  <div class="logo">Elastic<span>/ AI Agent Observability</span></div>
  <div class="badges">
    <span class="badge" id="hdr-model">—</span>
    <span class="badge otel">OTel OTLP</span>
    <span class="badge live">● LIVE</span>
    <a id="apm-link" class="apm-btn" href="#" target="_blank">APM ↗</a>
  </div>
</header>

<main>
  <div class="chat-col">
    <div class="messages" id="msgs">
      <div class="welcome">
        <h2>Gemini AI Agent</h2>
        <p>Ask anything about Elasticsearch or Elastic observability. Every message sends
        real OpenTelemetry signals to Elastic — traces, logs, and metrics, all via OTLP.</p>
        <p class="tag">Watch APM → Services → gemini-demo-agent →</p>
      </div>
    </div>
    <div class="input-row">
      <button class="new-btn" id="new-btn">+ New</button>
      <textarea id="inp" placeholder="Ask about ES|QL, APM, ELSER, vector search…"></textarea>
      <button class="send-btn" id="send-btn">Send</button>
    </div>
  </div>

  <div class="tele">
    <div class="tele-hdr">Live OTel Telemetry</div>

    <div class="tele-sec">
      <div class="tele-lbl">Signals via OTLP/HTTP</div>
      <div class="sig-row"><div class="dot-s ok"></div><div><div class="sig-txt">Traces</div><div class="sig-sub">traces-generic.otel-default</div></div></div>
      <div class="sig-row"><div class="dot-s ok"></div><div><div class="sig-txt">Logs</div><div class="sig-sub">logs-generic.otel-default</div></div></div>
      <div class="sig-row"><div class="dot-s ok"></div><div><div class="sig-txt">Metrics</div><div class="sig-sub">metrics-generic.otel-default</div></div></div>
      <div class="sig-row"><div class="dot-s" id="psutil-dot"></div><div><div class="sig-txt">Host metrics</div><div class="sig-sub">psutil → OTel gauge callbacks</div></div></div>
    </div>

    <div class="tele-sec">
      <div class="tele-lbl">Last Turn</div>
      <div class="kv"><span>Latency</span><span class="b" id="t-lat">—</span></div>
      <div class="kv"><span>Tokens in</span><span id="t-in">—</span></div>
      <div class="kv"><span>Tokens out</span><span id="t-out">—</span></div>
      <div class="kv"><span>Cost</span><span class="y" id="t-cost">—</span></div>
      <div class="tele-lbl" style="margin-top:10px">Trace ID</div>
      <div class="mono" id="t-tid">—</div>
    </div>

    <div class="tele-sec">
      <div class="tele-lbl">Session Totals</div>
      <div class="kv"><span>Turns</span><span class="e" id="s-turns">0</span></div>
      <div class="kv"><span>Tokens in</span><span id="s-in">0</span></div>
      <div class="kv"><span>Tokens out</span><span id="s-out">0</span></div>
      <div class="kv"><span>Total cost</span><span class="y" id="s-cost">$0.000000</span></div>
    </div>

    <div class="tele-sec">
      <div class="tele-lbl">Config</div>
      <div class="kv"><span>Model</span><span class="e" id="cfg-m">—</span></div>
      <div class="kv"><span>Transport</span><span id="cfg-t">OTLP/HTTP</span></div>
      <div class="kv"><span>Host</span><span id="cfg-h">—</span></div>
      <div class="kv"><span>Metrics every</span><span id="cfg-mi">—</span></div>
    </div>

    <div class="tele-sec" style="padding-bottom:16px">
      <div class="tele-lbl">Data Flow</div>
      <div class="flow">
        <div class="fnode user-node">🌐 Browser</div>
        <div class="farrow">↓ HTTP POST /api/chat</div>
        <div class="fnode app-node">⚡ Python Server<div class="fsub">OTel SDK · psutil</div></div>
        <div class="farrow">↓ OTLP/HTTP</div>
        <div class="fnode ingest-node">☁️ Elastic APM Server<div class="fsub">apm.elastic.cloud</div></div>
        <div class="frow3">
          <div class="fsmall">📡<br>Traces</div>
          <div class="fsmall">📋<br>Logs</div>
          <div class="fsmall">📊<br>Metrics</div>
        </div>
        <div class="farrow">↓ Kibana APM</div>
        <div class="fnode kibana-node">🔍 Kibana<div class="fsub">APM · Infra · Logs</div></div>
      </div>
    </div>

  </div>
</main>

<script>
const $ = id => document.getElementById(id);
let sid = null, turns = 0, totIn = 0, totOut = 0, totCost = 0, cfg = {};

fetch('/api/config').then(r=>r.json()).then(c=>{
  cfg = c;
  $('hdr-model').textContent = c.model;
  $('apm-link').href = c.apm_url;
  $('cfg-m').textContent = c.model;
  $('cfg-h').textContent = c.host;
  $('cfg-mi').textContent = c.metrics_interval + 's';
  $('psutil-dot').className = 'dot-s ' + (c.psutil ? 'ok' : 'dim');
});

const ta = $('inp');
ta.addEventListener('input', ()=>{ ta.style.height='42px'; ta.style.height=Math.min(ta.scrollHeight,120)+'px'; });
ta.addEventListener('keydown', e=>{ if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send();} });

$('new-btn').onclick = ()=>{
  sid = null; turns = totIn = totOut = totCost = 0; statUpdate();
  $('msgs').innerHTML=`<div class="welcome"><h2>New Conversation</h2><p>Fresh session — traces will appear in APM as a new conversation group.</p></div>`;
};

function md(t){
  return t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/```([\s\S]*?)```/g,'<pre><code>$1</code></pre>')
    .replace(/`([^`]+)`/g,'<code>$1</code>')
    .replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>')
    .replace(/\*(.+?)\*/g,'<em>$1</em>')
    .replace(/^#{1,3}\s(.+)$/gm,'<strong>$1</strong>')
    .replace(/^[-*]\s(.+)$/gm,'&nbsp;&nbsp;• $1')
    .replace(/\n/g,'<br>');
}

async function send(){
  const msg = ta.value.trim(); if(!msg) return;
  ta.value=''; ta.style.height='42px';
  $('send-btn').disabled = true;
  const w = $('msgs').querySelector('.welcome'); if(w) w.remove();
  addBubble('user', msg);
  const typing = addTyping();
  try{
    const body = {message:msg}; if(sid) body.session_id=sid;
    const r = await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const d = await r.json();
    typing.remove(); sid = d.session_id;
    addBubble('agent', d.text);
    addPills(d);
    update(d);
  }catch(e){typing.remove(); addBubble('agent','**Error:** '+e.message);}
  finally{$('send-btn').disabled=false; ta.focus();}
}
$('send-btn').onclick = send;

function addBubble(who, text){
  const d = document.createElement('div'); d.className='msg '+who;
  d.innerHTML=`<div class="avatar">${who==='user'?'👤':'⚡'}</div><div class="bubble">${md(text)}</div>`;
  $('msgs').appendChild(d); scroll(); return d;
}
function addPills(d){
  const r = document.createElement('div'); r.className='trace-row';
  const traceUrl = cfg.apm_url || '#';
  r.innerHTML=`
    <a class="pill trace" href="${traceUrl}" target="_blank">📡 ${(d.trace_id||'').substring(0,8)}…</a>
    <span class="pill otlp">OTLP</span>
    <span class="pill dim">${(d.in_tokens||0).toLocaleString()}↑ ${(d.out_tokens||0).toLocaleString()}↓</span>
    <span class="pill dim">${d.latency_ms}ms</span>
    <span class="pill dim">$${(d.cost_usd||0).toFixed(5)}</span>
    ${d.is_error?'<span class="pill err">error</span>':''}`;
  $('msgs').appendChild(r); scroll();
}
function addTyping(){
  const d=document.createElement('div'); d.className='msg agent';
  d.innerHTML='<div class="avatar">⚡</div><div class="typing"><div class="dots"><div class="dot"></div><div class="dot"></div><div class="dot"></div></div>Thinking…</div>';
  $('msgs').appendChild(d); scroll(); return d;
}
function scroll(){ const m=$('msgs'); m.scrollTop=m.scrollHeight; }

function update(d){
  $('t-lat').textContent = d.latency_ms+'ms';
  $('t-in').textContent  = (d.in_tokens||0).toLocaleString();
  $('t-out').textContent = (d.out_tokens||0).toLocaleString();
  $('t-cost').textContent= '$'+(d.cost_usd||0).toFixed(5);
  $('t-tid').textContent = d.trace_id||'—';
  turns++; totIn+=d.in_tokens||0; totOut+=d.out_tokens||0; totCost+=d.cost_usd||0;
  statUpdate();
}
function statUpdate(){
  $('s-turns').textContent = turns;
  $('s-in').textContent    = totIn.toLocaleString();
  $('s-out').textContent   = totOut.toLocaleString();
  $('s-cost').textContent  = '$'+totCost.toFixed(6);
}
</script>
</body>
</html>"""


# ── HTTP server ───────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def send_json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/config":
            return self.send_json(200, {
                "model":            MODEL,
                "host":             HOST_NAME,
                "metrics_interval": METRICS_INTERVAL_S,
                "psutil":           _PSUTIL,
                "otlp_endpoint":    OTLP_ENDPOINT,
                "apm_url":          f"{KB_ENDPOINT}/app/apm/services/{SVC_NAME}/overview",
            })
        body = HTML.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/api/chat":
            sid, session = get_or_create_session(body.get("session_id"))
            try:
                result = chat_turn(body["message"], session["conv_id"], session["history"])
                session["history"].append({"role": "user",  "text": body["message"]})
                session["history"].append({"role": "model", "text": result["text"]})
                session["history"] = session["history"][-40:]
                result["session_id"] = sid
                self.send_json(200, result)
            except Exception as e:
                logger.exception("chat_turn failed")
                self.send_json(500, {"error": str(e), "session_id": sid})
        else:
            self.send_json(404, {"error": "not found"})


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    # Start psutil scrape loop
    if _PSUTIL:
        psutil.cpu_percent(interval=None)  # prime
        threading.Thread(target=_psutil_scrape_loop, daemon=True, name="psutil-scrape").start()

    logger.info(f"Web agent starting — model={MODEL} host={HOST_NAME} port={PORT} "
                f"otlp={OTLP_ENDPOINT} psutil={_PSUTIL}")

    print(f"\n{'─'*64}")
    print(f"  Elastic AI Agent — Full OTel Demo")
    print(f"{'─'*64}")
    print(f"  Chat UI  →  http://localhost:{PORT}")
    print(f"  APM UI   →  {KB_ENDPOINT}/app/apm/services/{SVC_NAME}/overview")
    print(f"  Host     :  {HOST_NAME}")
    print(f"  Model    :  {MODEL}")
    print(f"{'─'*64}")
    print(f"  OTel signals → {OTLP_ENDPOINT}")
    print(f"    📡 Traces  → /v1/traces  → traces-generic.otel-default")
    print(f"    📋 Logs    → /v1/logs    → logs-generic.otel-default")
    print(f"    📊 Metrics → /v1/metrics → metrics-generic.otel-default")
    print(f"       (gen_ai.* conventions + {'real host metrics via psutil' if _PSUTIL else 'install psutil for host metrics'})")
    print(f"{'─'*64}\n")

    server = HTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down…")
    finally:
        metric_reader.shutdown()
        trace_provider.shutdown()
        log_provider.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
