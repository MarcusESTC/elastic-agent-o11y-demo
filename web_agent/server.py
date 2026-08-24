#!/usr/bin/env python3
"""
web_agent/server.py — Full OTel Gemini chat agent → Elastic via OTLP

ALL signals flow via OpenTelemetry SDK → OTLP/HTTP → Elastic Cloud:
  📡 Traces  → traces-generic.otel-default  (agentic waterfall: agent → tools → LLM)
  📋 Logs    → logs-generic.otel-default    (structured, trace-correlated)
  📊 Metrics → metrics-generic.otel-default (gen_ai.*, guardrails, feedback, host)

Demo features:
  ✅ Gemini function calling  → real tool call child spans in APM waterfall
  ✅ A/B model routing        → per-session, visible in metrics/traces
  ✅ PII guardrail detection  → span events + counter
  ✅ User feedback loop       → 👍/👎 → OTel log correlated to trace_id
  ✅ psutil real host metrics → CPU, memory, network observable gauges
  ✅ 100% sampling (AlwaysOn) → every turn visible in APM
"""

import os, sys, re, time, json, uuid, random, threading, socket, logging
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
from opentelemetry.sdk._logs.export import SimpleLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.trace import SpanKind, StatusCode
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.sdk._logs import LoggingHandler
from opentelemetry.metrics import Observation

# ── Config ────────────────────────────────────────────────────────────────────

GEMINI_KEY  = os.environ["GEMINI_API_KEY"]
ES_ENDPOINT = os.environ["ES_ENDPOINT"]
ES_API_KEY  = os.environ["ES_API_KEY"]

MODEL_A   = os.environ.get("GEMINI_MODEL",   "gemini-2.5-flash")   # primary (70%)
MODEL_B   = os.environ.get("GEMINI_MODEL_B", "gemini-2.5-flash-lite")  # cheaper variant (30%)
AB_RATIO  = float(os.environ.get("AB_RATIO", "0.3"))               # fraction routed to B

SVC_NAME  = "gemini-demo-agent"
HOST_NAME = os.environ.get("HOST_NAME", socket.gethostname())
PORT      = int(os.environ.get("PORT", "5601"))
METRICS_INTERVAL_S = int(os.environ.get("METRICS_INTERVAL", "30"))

# OTLP endpoints — full paths required (base URL causes silent 404)
_INGEST         = ES_ENDPOINT.replace(".es.", ".ingest.")
_APM            = ES_ENDPOINT.replace(".es.", ".apm.")
OTLP_TRACES_EP  = f"{_INGEST}/v1/traces"
OTLP_METRICS_EP = f"{_INGEST}/v1/metrics"
OTLP_LOGS_EP    = f"{_APM}/v1/logs"
OTLP_HEADERS    = {"Authorization": f"ApiKey {ES_API_KEY}"}
OTLP_ENDPOINT   = _INGEST   # display only
KB_ENDPOINT     = ES_ENDPOINT.replace(".es.", ".kb.")

SYSTEM_PROMPT = (
    "You are a helpful AI assistant. You have access to tools — use them when they "
    "would help you give a better, more accurate answer. Always search before answering "
    "factual questions. Keep responses concise (under 300 words) and use markdown."
)

# ── OTel Resource ─────────────────────────────────────────────────────────────

resource = Resource.create({
    "service.name":            SVC_NAME,
    "service.version":         "2.0.0",
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
    "telemetry.distro.name":   "elastic",
})

# ── Traces ────────────────────────────────────────────────────────────────────

trace_exporter = OTLPSpanExporter(endpoint=OTLP_TRACES_EP, headers=OTLP_HEADERS)
trace_provider = TracerProvider(resource=resource)
trace_provider.add_span_processor(SimpleSpanProcessor(trace_exporter))
trace.set_tracer_provider(trace_provider)
tracer = trace.get_tracer(SVC_NAME, "2.0.0")

# ── Metrics ───────────────────────────────────────────────────────────────────

metric_exporter = OTLPMetricExporter(endpoint=OTLP_METRICS_EP, headers=OTLP_HEADERS)
metric_reader   = PeriodicExportingMetricReader(
    metric_exporter, export_interval_millis=METRICS_INTERVAL_S * 1000)
meter_provider  = MeterProvider(resource=resource, metric_readers=[metric_reader])
metrics.set_meter_provider(meter_provider)
meter = metrics.get_meter(SVC_NAME, "2.0.0")

# gen_ai semantic convention instruments
token_counter = meter.create_counter(
    "gen_ai.client.token.usage", unit="{token}",
    description="Tokens used by the generative AI model")
op_duration = meter.create_histogram(
    "gen_ai.client.operation.duration", unit="s",
    description="Duration of generative AI operations")
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

# Guardrail instruments
guardrail_counter = meter.create_counter(
    "gen_ai.guardrail.violations", unit="{violation}",
    description="Guardrail violations detected (PII, policy, etc.)")

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

log_exporter = OTLPLogExporter(endpoint=OTLP_LOGS_EP, headers=OTLP_HEADERS)
log_provider = LoggerProvider(resource=resource)
log_provider.add_log_record_processor(SimpleLogRecordProcessor(log_exporter))

otel_handler = LoggingHandler(level=logging.DEBUG, logger_provider=log_provider)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(SVC_NAME)
logger.addHandler(otel_handler)
logger.propagate = False

# ── Gemini client ─────────────────────────────────────────────────────────────

gemini = genai.Client(api_key=GEMINI_KEY)

# ── Gemini Tools (function declarations) ──────────────────────────────────────
# Each tool call in the agentic loop becomes a child span in the APM waterfall.

GEMINI_TOOLS = types.Tool(function_declarations=[
    types.FunctionDeclaration(
        name="search_knowledge_base",
        description=(
            "Search the knowledge base, documentation, or web for relevant information "
            "on any topic. Use this before answering factual or technical questions."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "query": types.Schema(
                    type=types.Type.STRING,
                    description="The search query to look up"
                )
            },
            required=["query"],
        ),
    ),
    types.FunctionDeclaration(
        name="calculate",
        description=(
            "Evaluate a mathematical expression or perform a calculation. "
            "Use for any arithmetic, unit conversion, or numeric computation."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "expression": types.Schema(
                    type=types.Type.STRING,
                    description="Mathematical expression to evaluate, e.g. '2 ** 10' or '(100 * 1.08) / 12'"
                )
            },
            required=["expression"],
        ),
    ),
    types.FunctionDeclaration(
        name="get_stock_price",
        description="Look up the current or recent price and basic info for a stock ticker symbol.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "ticker": types.Schema(
                    type=types.Type.STRING,
                    description="Stock ticker symbol, e.g. ESTC, GOOG, AAPL"
                )
            },
            required=["ticker"],
        ),
    ),
])


def _execute_tool(name: str, args: dict) -> str:
    """Execute a tool call and return its result string."""
    if name == "search_knowledge_base":
        query = args.get("query", "")
        time.sleep(0.08)  # simulate network round-trip
        snippets = [
            f"[Doc 1] Overview of {query}: This topic covers fundamental concepts and best practices used in production systems.",
            f"[Doc 2] Advanced {query} techniques: Performance optimization and scaling strategies for enterprise deployments.",
            f"[Doc 3] {query} troubleshooting guide: Common issues, diagnostics, and resolution steps.",
        ]
        return f"Found 3 results for '{query}':\n" + "\n".join(snippets)

    elif name == "calculate":
        expr = args.get("expression", "0")
        try:
            import math
            safe_globals = {k: getattr(math, k) for k in dir(math) if not k.startswith("_")}
            result = eval(expr, {"__builtins__": {}}, safe_globals)
            return f"{expr} = {result}"
        except Exception as e:
            return f"Calculation error: {e}"

    elif name == "get_stock_price":
        ticker = args.get("ticker", "").upper()
        time.sleep(0.06)  # simulate API latency
        # Simulated prices for common tickers
        prices = {
            "ESTC": 78.42, "GOOG": 182.15, "AAPL": 227.83,
            "MSFT": 415.20, "AMZN": 196.47, "NVDA": 124.38,
        }
        price = prices.get(ticker, round(random.uniform(50, 500), 2))
        change = round(random.uniform(-3.5, 4.2), 2)
        pct    = round(change / price * 100, 2)
        sign   = "+" if change >= 0 else ""
        return (f"{ticker}: ${price:.2f}  {sign}{change} ({sign}{pct}%)  "
                f"[simulated · {datetime.now(timezone.utc).strftime('%H:%M UTC')}]")

    return f"Unknown tool: {name}"


# ── PII detection ─────────────────────────────────────────────────────────────

_PII_PATTERNS = {
    "email":       r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
    "phone":       r"\b\d{3}[\-.\s]?\d{3}[\-.\s]?\d{4}\b",
    "ssn":         r"\b\d{3}-\d{2}-\d{4}\b",
    "credit_card": r"\b\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b",
}

def detect_pii(text: str) -> list[str]:
    return [pii_type for pii_type, pat in _PII_PATTERNS.items()
            if re.search(pat, text, re.IGNORECASE)]


# ── psutil scrape loop ────────────────────────────────────────────────────────

def _psutil_scrape_loop():
    if not _PSUTIL:
        return
    global _cpu_pct, _mem_pct, _mem_bytes, _proc_cpu, _proc_mem, _net_in, _net_out
    proc = psutil.Process(os.getpid())
    psutil.cpu_percent(interval=None)
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
            net       = psutil.net_io_counters()
            _net_in   = max(0, net.bytes_recv - net_prev[0])
            _net_out  = max(0, net.bytes_sent - net_prev[1])
            net_prev  = (net.bytes_recv, net.bytes_sent)
        except Exception:
            pass


# ── Chat turn ─────────────────────────────────────────────────────────────────

def chat_turn(user_message: str, conversation_id: str, history: list,
              model: str, variant: str) -> dict:
    """
    Full agentic turn with OTel instrumentation:

      invoke_agent [SERVER]              ← root span / APM transaction
        ├── tool:search_knowledge_base [CLIENT]  ← tool call spans
        ├── tool:calculate [CLIENT]
        └── chat {MODEL} [CLIENT]        ← LLM call span

    Also emits:
      - PII guardrail check (span event + metric if triggered)
      - gen_ai.content.prompt/completion span events
      - token_counter, op_duration, tool_calls_counter metrics
      - Structured logs correlated to trace_id
    """
    t0 = time.time()
    trace_id = tx_id = None
    tool_calls_made = []   # [{name, args_summary, result_summary, duration_ms}]

    with tracer.start_as_current_span(
        "invoke_agent",
        kind=SpanKind.SERVER,
        attributes={
            "gen_ai.operation.name":   "invoke_agent",
            "gen_ai.system":           "google_gemini",
            "gen_ai.request.model":    model,
            "gen_ai.conversation.id":  conversation_id,
            "gen_ai.agent.name":       SVC_NAME,
            "ab.variant":              variant,
            "ab.model":                model,
        },
    ) as root:
        ctx      = root.get_span_context()
        trace_id = format(ctx.trace_id, "032x")
        tx_id    = format(ctx.span_id,  "016x")

        # ── PII guardrail ───────────────────────────────────────────────
        pii_types = detect_pii(user_message)
        if pii_types:
            root.add_event("guardrail.pii_detected", {
                "pii.types":          ", ".join(pii_types),
                "pii.message_length": len(user_message),
            })
            root.set_attribute("guardrail.pii_detected", True)
            for pii_type in pii_types:
                guardrail_counter.add(1, {
                    "guardrail.type":   "pii",
                    "pii.type":         pii_type,
                    "gen_ai.system":    "google_gemini",
                })
            logger.warning("PII detected in user message",
                           extra={"pii_types": pii_types, "conversation_id": conversation_id})

        logger.info("Turn started", extra={
            "conversation_id": conversation_id,
            "model": model, "variant": variant,
            "user_message_len": len(user_message),
            "pii_detected": bool(pii_types),
        })

        # ── Agentic LLM loop with tool calls ───────────────────────────
        in_tok = out_tok = 0
        is_error = False
        output_text = ""

        with tracer.start_as_current_span(
            f"chat {model}",
            kind=SpanKind.CLIENT,
            attributes={
                "gen_ai.operation.name":  "chat",
                "gen_ai.system":          "google_gemini",
                "gen_ai.request.model":   model,
                "gen_ai.input.messages":  user_message[:800],
                "peer.service":           "google_gemini",
                "server.address":         "generativelanguage.googleapis.com",
                "server.port":            443,
                "network.protocol.name":  "https",
                "rpc.system":             "http",
                "rpc.service":            "google.ai.generativelanguage",
                "rpc.method":             "GenerateContent",
                "ab.variant":             variant,
            },
        ) as llm:
            try:
                # Build conversation contents
                contents = [
                    types.Content(role=t["role"], parts=[types.Part(text=t["text"])])
                    for t in history
                ] + [types.Content(role="user", parts=[types.Part(text=user_message)])]

                cfg = types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    temperature=0.7,
                    max_output_tokens=512,
                    tools=[GEMINI_TOOLS],
                )

                # Agentic loop — iterate until model returns text (no more tool calls)
                for _iteration in range(6):
                    response = gemini.models.generate_content(
                        model=model, contents=contents, config=cfg)

                    # Accumulate tokens across iterations
                    usage   = response.usage_metadata
                    in_tok  += getattr(usage, "prompt_token_count",     0) or 0
                    out_tok += getattr(usage, "candidates_token_count", 0) or 0

                    # Collect function call parts
                    candidate_parts = response.candidates[0].content.parts
                    fn_calls = [p.function_call for p in candidate_parts
                                if hasattr(p, "function_call") and p.function_call]

                    if not fn_calls:
                        # Final text response
                        output_text = response.text or ""
                        break

                    # Execute each tool call — each gets its own child span
                    fn_response_parts = []
                    for fc in fn_calls:
                        tool_name = fc.name
                        tool_args = dict(fc.args) if fc.args else {}
                        t_tool = time.time()

                        with tracer.start_as_current_span(
                            f"tool:{tool_name}",
                            kind=SpanKind.CLIENT,
                            attributes={
                                "gen_ai.tool.name":       tool_name,
                                "gen_ai.tool.call.id":    f"{tool_name}_{_iteration}",
                                "gen_ai.operation.name":  "tool",
                                "gen_ai.system":          "google_gemini",
                                "peer.service":           f"tool.{tool_name}",
                                "tool.arguments":         json.dumps(tool_args)[:400],
                            },
                        ) as tool_span:
                            result = _execute_tool(tool_name, tool_args)
                            tool_dur = time.time() - t_tool

                            tool_span.set_attribute("tool.result_length", len(result))
                            tool_span.set_attribute("tool.duration_ms", round(tool_dur * 1000))
                            tool_span.set_status(StatusCode.OK)

                            # Metrics
                            tool_calls_counter.add(1, {
                                "gen_ai.tool.name": tool_name,
                                "gen_ai.system":    "google_gemini",
                            })
                            tool_duration.record(tool_dur, {"gen_ai.tool.name": tool_name})

                            tool_calls_made.append({
                                "name":           tool_name,
                                "args_summary":   json.dumps(tool_args)[:120],
                                "result_summary": result[:120],
                                "duration_ms":    round(tool_dur * 1000),
                            })

                        fn_response_parts.append(
                            types.Part.from_function_response(
                                name=tool_name, response={"result": result}
                            )
                        )

                    # Append model's function call message + our function results
                    contents.append(response.candidates[0].content)
                    contents.append(types.Content(role="user", parts=fn_response_parts))

                # Span events — full prompt + completion text
                llm.add_event("gen_ai.content.prompt", {"gen_ai.prompt": user_message})
                llm.add_event("gen_ai.content.completion", {"gen_ai.completion": output_text})

                llm.set_attribute("gen_ai.response.model",      model)
                llm.set_attribute("gen_ai.usage.input_tokens",  in_tok)
                llm.set_attribute("gen_ai.usage.output_tokens", out_tok)
                llm.set_attribute("gen_ai.output.messages",     output_text[:800])
                llm.set_attribute("gen_ai.tool.calls_count",    len(tool_calls_made))
                llm.set_status(StatusCode.OK)

                root.set_attribute("gen_ai.usage.input_tokens",  in_tok)
                root.set_attribute("gen_ai.usage.output_tokens", out_tok)
                root.set_attribute("gen_ai.tool.calls_count",    len(tool_calls_made))
                root.set_status(StatusCode.OK)

                # Token metrics
                attrs = {"gen_ai.operation.name": "chat", "gen_ai.system": "google_gemini",
                         "gen_ai.request.model": model, "ab.variant": variant}
                token_counter.add(in_tok,  {**attrs, "gen_ai.token.type": "input"})
                token_counter.add(out_tok, {**attrs, "gen_ai.token.type": "output"})

            except Exception as e:
                llm.set_status(StatusCode.ERROR, str(e))
                llm.record_exception(e)
                root.set_status(StatusCode.ERROR, str(e))
                logger.error(f"Gemini API error: {e}", exc_info=True,
                             extra={"model": model, "conversation_id": conversation_id})
                error_counter.add(1, {"gen_ai.operation.name": "chat",
                                      "gen_ai.system": "google_gemini",
                                      "error.type": type(e).__name__,
                                      "ab.variant": variant})
                output_text = f"Sorry, I encountered an error: {e}"
                is_error = True

    elapsed_s  = time.time() - t0
    cost_usd   = (in_tok * 0.075 + out_tok * 0.30) / 1_000_000

    op_duration.record(elapsed_s, {
        "gen_ai.operation.name":  "chat",
        "gen_ai.system":          "google_gemini",
        "gen_ai.request.model":   model,
        "gen_ai.response.model":  model,
        "ab.variant":             variant,
        "error.occurred":         is_error,
    })

    logger.info("Turn completed", extra={
        "conversation_id": conversation_id,
        "model": model, "variant": variant,
        "in_tokens": in_tok, "out_tokens": out_tok,
        "latency_ms": round(elapsed_s * 1000),
        "cost_usd": round(cost_usd, 6),
        "tool_calls": len(tool_calls_made),
        "is_error": is_error,
    })

    return {
        "text":        output_text,
        "trace_id":    trace_id,
        "tx_id":       tx_id,
        "in_tokens":   in_tok,
        "out_tokens":  out_tok,
        "latency_ms":  round(elapsed_s * 1000),
        "cost_usd":    round(cost_usd, 6),
        "is_error":    is_error,
        "variant":     variant,
        "model_used":  model,
        "tool_calls":  tool_calls_made,
        "pii_detected": bool(pii_types),
        "transport":   "otlp",
        "apm_url":     (f"{KB_ENDPOINT}/app/apm/services/{SVC_NAME}/transactions/view"
                        f"?transactionName=invoke_agent&transactionType=request"),
    }


# ── In-memory sessions ────────────────────────────────────────────────────────

_sessions = {}
_lock     = threading.Lock()

def get_or_create_session(session_id=None):
    with _lock:
        if not session_id or session_id not in _sessions:
            session_id = uuid.uuid4().hex[:12]
            # Assign A/B variant at session creation — consistent within a session
            variant = "B" if random.random() < AB_RATIO else "A"
            model   = MODEL_B if variant == "B" else MODEL_A
            _sessions[session_id] = {
                "id":      session_id,
                "conv_id": uuid.uuid4().hex[:8],
                "history": [],
                "variant": variant,
                "model":   model,
            }
        return session_id, _sessions[session_id]


# ── Web UI ────────────────────────────────────────────────────────────────────

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
    --red:#f85149;--elastic:#00bfb3;--elastic-2:#003d38;--otel:#7b61ff;
    --tool:#f0883e;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;height:100vh;display:flex;flex-direction:column;overflow:hidden}

  header{background:var(--surface);border-bottom:1px solid var(--border);padding:10px 20px;display:flex;align-items:center;gap:14px;flex-shrink:0}
  .logo{font-size:17px;font-weight:700;color:var(--elastic)}
  .logo span{color:var(--text-2);font-weight:400;font-size:13px;margin-left:6px}
  .badges{display:flex;gap:8px;margin-left:auto;align-items:center}
  .badge{font-size:11px;border-radius:4px;padding:2px 8px;border:1px solid var(--border);color:var(--text-2)}
  .badge.otel{border-color:var(--otel);color:var(--otel)}
  .badge.live{border-color:var(--elastic);color:var(--elastic)}
  .badge.ab-a{border-color:var(--blue);color:var(--blue)}
  .badge.ab-b{border-color:var(--tool);color:var(--tool)}
  .apm-btn{color:var(--blue);text-decoration:none;font-size:12px;border:1px solid var(--border);border-radius:4px;padding:3px 10px;transition:border-color .15s}
  .apm-btn:hover{border-color:var(--blue)}

  main{display:flex;flex:1;min-height:0}

  /* Chat column */
  .chat-col{flex:1;display:flex;flex-direction:column;min-width:0}
  .messages{flex:1;overflow-y:auto;padding:20px;display:flex;flex-direction:column;gap:12px}
  .messages::-webkit-scrollbar{width:5px}
  .messages::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}

  .msg{display:flex;gap:10px;max-width:84%}
  .msg.user{align-self:flex-end;flex-direction:row-reverse}
  .avatar{width:30px;height:30px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:14px;flex-shrink:0;margin-top:2px}
  .msg.user .avatar{background:#1c2d3a;border:1px solid var(--blue)}
  .msg.agent .avatar{background:#001a18;border:1px solid var(--elastic)}
  .bubble{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:10px 14px;font-size:14px;line-height:1.65;word-break:break-word}
  .msg.user .bubble{background:#1c2d3a;border-color:var(--blue)}
  .bubble pre{background:#0d1117;border:1px solid var(--border);border-radius:6px;padding:10px;overflow-x:auto;font-size:12px;margin:8px 0}
  .bubble code{background:#111;border:1px solid var(--border);border-radius:3px;padding:1px 5px;font-size:12px}
  .bubble strong{color:var(--text)}

  /* Tool call display */
  .tools-row{margin-left:40px;margin-top:-6px;display:flex;flex-direction:column;gap:4px}
  .tool-chip{display:flex;align-items:center;gap:6px;font-size:11px;color:var(--tool);background:#1a0f00;border:1px solid #3d2000;border-radius:6px;padding:4px 10px;width:fit-content;cursor:pointer;transition:border-color .15s}
  .tool-chip:hover{border-color:var(--tool)}
  .tool-chip .tc-name{font-weight:600}
  .tool-chip .tc-dur{color:var(--text-3);margin-left:auto}
  .tool-detail{display:none;font-size:11px;font-family:monospace;color:var(--text-3);background:#0d0800;border:1px solid #2a1a00;border-radius:4px;padding:6px 10px;margin-left:40px;word-break:break-all}
  .tool-detail.open{display:block}

  /* Pills row */
  .pills-row{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-left:40px;margin-top:-4px}
  .pill{font-size:11px;border-radius:4px;padding:2px 8px;border:1px solid}
  .pill.trace{border-color:var(--elastic);color:var(--elastic);text-decoration:none;cursor:pointer}
  .pill.trace:hover{background:var(--elastic-2)}
  .pill.dim{border-color:var(--border);color:var(--text-3)}
  .pill.err{border-color:var(--red);color:var(--red)}
  .pill.otlp{border-color:var(--otel);color:var(--otel)}
  .pill.pii{border-color:var(--red);color:var(--red)}
  .pill.ab-a{border-color:var(--blue);color:var(--blue)}
  .pill.ab-b{border-color:var(--tool);color:var(--tool)}

  /* Feedback buttons */
  .feedback-row{display:flex;gap:8px;align-items:center;margin-left:40px;margin-top:2px}
  .fb-btn{background:transparent;border:1px solid var(--border);color:var(--text-3);border-radius:6px;padding:3px 10px;font-size:13px;cursor:pointer;transition:all .15s}
  .fb-btn:hover{border-color:var(--text-2);color:var(--text)}
  .fb-btn.voted-up{border-color:var(--green);color:var(--green);background:#0a1f10}
  .fb-btn.voted-down{border-color:var(--red);color:var(--red);background:#1a0808}
  .fb-lbl{font-size:11px;color:var(--text-3)}

  /* Typing indicator */
  .typing{display:flex;align-items:center;gap:6px;padding:10px 14px;color:var(--text-3);font-size:13px}
  .dots{display:flex;gap:4px}
  .dot{width:6px;height:6px;border-radius:50%;background:var(--elastic);animation:bop .8s infinite alternate}
  .dot:nth-child(2){animation-delay:.15s}.dot:nth-child(3){animation-delay:.3s}
  @keyframes bop{to{transform:translateY(-4px);opacity:.4}}

  .input-row{border-top:1px solid var(--border);padding:12px 20px;display:flex;gap:10px;background:var(--surface);flex-shrink:0}
  textarea{flex:1;background:var(--bg);border:1px solid var(--border);border-radius:8px;color:var(--text);padding:10px 14px;font-size:14px;font-family:inherit;resize:none;height:42px;max-height:120px;line-height:1.5;transition:border-color .15s}
  textarea:focus{outline:none;border-color:var(--elastic)}
  .send-btn{background:var(--elastic);border:none;color:#000;border-radius:8px;padding:10px 20px;font-size:14px;font-weight:600;cursor:pointer;transition:opacity .15s}
  .send-btn:hover{opacity:.85}.send-btn:disabled{opacity:.35;cursor:not-allowed}
  .new-btn{background:transparent;border:1px solid var(--border);color:var(--text-2);border-radius:8px;padding:10px 14px;font-size:13px;cursor:pointer}
  .new-btn:hover{border-color:var(--text-2)}

  /* Sidebar */
  .tele{width:272px;border-left:1px solid var(--border);background:var(--surface);display:flex;flex-direction:column;overflow-y:auto;flex-shrink:0}
  .tele-hdr{padding:10px 14px;border-bottom:1px solid var(--border);font-size:11px;font-weight:700;color:var(--text-2);text-transform:uppercase;letter-spacing:.6px}
  .tele-sec{padding:12px 14px;border-bottom:1px solid var(--border)}
  .tele-lbl{font-size:10px;text-transform:uppercase;color:var(--text-3);letter-spacing:.5px;margin-bottom:7px}
  .kv{display:flex;justify-content:space-between;margin-bottom:5px;font-size:12px}
  .kv span:first-child{color:var(--text-2)}
  .kv span:last-child{color:var(--text);font-weight:500;font-variant-numeric:tabular-nums}
  .kv span.g{color:var(--green)}.kv span.b{color:var(--blue)}.kv span.y{color:var(--yellow)}
  .kv span.e{color:var(--elastic)}.kv span.r{color:var(--red)}.kv span.t{color:var(--tool)}
  .mono{font-size:10px;font-family:monospace;color:var(--text-3);word-break:break-all;margin-top:3px;line-height:1.4}
  .sig-row{display:flex;align-items:center;gap:7px;margin-bottom:5px;font-size:12px}
  .dot-s{width:7px;height:7px;border-radius:50%;flex-shrink:0}
  .dot-s.ok{background:var(--green)}.dot-s.dim{background:var(--text-3)}
  .dot-s.tool{background:var(--tool)}
  .sig-txt{color:var(--text-2)}.sig-sub{color:var(--text-3);font-size:10px}

  .welcome{text-align:center;padding:50px 30px;color:var(--text-2)}
  .welcome h2{color:var(--text);font-size:20px;margin-bottom:8px}
  .welcome p{font-size:13px;line-height:1.6;max-width:360px;margin:0 auto 10px}
  .welcome .tag{color:var(--elastic);font-size:12px}

  /* Data flow diagram */
  .flow{display:flex;flex-direction:column;align-items:center;gap:4px;margin-top:6px}
  .fnode{border:1px solid var(--border);border-radius:8px;padding:6px 10px;font-size:11px;text-align:center;width:100%;background:var(--bg)}
  .fnode.user-node{border-color:var(--blue);color:var(--blue)}
  .fnode.app-node{border-color:var(--elastic);color:var(--elastic)}
  .fnode.tool-node{border-color:var(--tool);color:var(--tool)}
  .fnode.llm-node{border-color:var(--otel);color:var(--otel)}
  .fnode.ingest-node{border-color:#5a3ea0;color:#a371f7}
  .fnode.kibana-node{border-color:var(--green);color:var(--green)}
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
    <span class="badge" id="hdr-ab">—</span>
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
        <p>Ask anything. The agent calls real tools (search, calculate, stock price lookup) and every interaction ships traces, logs, and metrics to Elastic via OpenTelemetry.</p>
        <p class="tag">Try: "What is vector search?" · "Calculate 2^32" · "ESTC stock price"</p>
      </div>
    </div>
    <div class="input-row">
      <button class="new-btn" id="new-btn">+ New</button>
      <textarea id="inp" placeholder="Ask anything — tools will be called automatically…"></textarea>
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
      <div class="sig-row"><div class="dot-s tool"></div><div><div class="sig-txt">Tool spans</div><div class="sig-sub">child spans per tool call</div></div></div>
      <div class="sig-row"><div class="dot-s" id="psutil-dot"></div><div><div class="sig-txt">Host metrics</div><div class="sig-sub">psutil → OTel gauges</div></div></div>
    </div>

    <div class="tele-sec">
      <div class="tele-lbl">A/B Routing</div>
      <div class="kv"><span>This session</span><span class="b" id="t-variant">—</span></div>
      <div class="kv"><span>Model</span><span class="e" id="t-model-used">—</span></div>
      <div class="kv"><span>A = primary</span><span class="b" id="cfg-ma">—</span></div>
      <div class="kv"><span>B = lighter</span><span class="t" id="cfg-mb">—</span></div>
    </div>

    <div class="tele-sec">
      <div class="tele-lbl">Last Turn</div>
      <div class="kv"><span>Latency</span><span class="b" id="t-lat">—</span></div>
      <div class="kv"><span>Tokens in</span><span id="t-in">—</span></div>
      <div class="kv"><span>Tokens out</span><span id="t-out">—</span></div>
      <div class="kv"><span>Cost</span><span class="y" id="t-cost">—</span></div>
      <div class="kv"><span>Tool calls</span><span class="t" id="t-tools">—</span></div>
      <div class="tele-lbl" style="margin-top:10px">Trace ID</div>
      <div class="mono" id="t-tid">—</div>
    </div>

    <div class="tele-sec">
      <div class="tele-lbl">Session Totals</div>
      <div class="kv"><span>Turns</span><span class="e" id="s-turns">0</span></div>
      <div class="kv"><span>Tokens in</span><span id="s-in">0</span></div>
      <div class="kv"><span>Tokens out</span><span id="s-out">0</span></div>
      <div class="kv"><span>Total cost</span><span class="y" id="s-cost">$0.000000</span></div>
      <div class="kv"><span>Tool calls</span><span class="t" id="s-tools">0</span></div>
      <div class="kv"><span>Feedback 👍</span><span class="g" id="s-up">0</span></div>
      <div class="kv"><span>Feedback 👎</span><span class="r" id="s-down">0</span></div>
    </div>

    <div class="tele-sec" style="padding-bottom:16px">
      <div class="tele-lbl">Data Flow</div>
      <div class="flow">
        <div class="fnode user-node">🌐 Browser</div>
        <div class="farrow">↓ HTTP POST /api/chat</div>
        <div class="fnode app-node">⚡ Python Server<div style="font-size:9px;color:#6e7681;margin-top:2px">PII check · A/B route · OTel SDK</div></div>
        <div class="farrow">↓ function calling</div>
        <div class="fnode tool-node">🔧 Tools<div style="font-size:9px;color:#6e7681;margin-top:2px">search · calculate · stocks</div></div>
        <div class="farrow">↓ generate_content</div>
        <div class="fnode llm-node">🤖 Google Gemini<div style="font-size:9px;color:#6e7681;margin-top:2px">gemini-2.5-flash · 1.5-flash</div></div>
        <div class="farrow">↓ OTLP/HTTP</div>
        <div class="fnode ingest-node">☁️ Elastic Cloud</div>
        <div class="frow3">
          <div class="fsmall">📡<br>Traces</div>
          <div class="fsmall">📋<br>Logs</div>
          <div class="fsmall">📊<br>Metrics</div>
        </div>
        <div class="farrow">↓ Kibana</div>
        <div class="fnode kibana-node">🔍 Kibana APM</div>
      </div>
    </div>

  </div>
</main>

<script>
const $ = id => document.getElementById(id);
let sid=null, turns=0, totIn=0, totOut=0, totCost=0, totTools=0, totUp=0, totDown=0, cfg={};

fetch('/api/config').then(r=>r.json()).then(c=>{
  cfg = c;
  $('hdr-model').textContent = c.model_a;
  $('cfg-ma').textContent = c.model_a;
  $('cfg-mb').textContent = c.model_b;
  $('apm-link').href = c.apm_url;
  $('psutil-dot').className = 'dot-s ' + (c.psutil ? 'ok' : 'dim');
});

const ta = $('inp');
ta.addEventListener('input', ()=>{ ta.style.height='42px'; ta.style.height=Math.min(ta.scrollHeight,120)+'px'; });
ta.addEventListener('keydown', e=>{ if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send();} });

$('new-btn').onclick = ()=>{
  sid=null; turns=totIn=totOut=totCost=totTools=totUp=totDown=0; statUpdate();
  $('t-variant').textContent='—'; $('t-model-used').textContent='—';
  $('hdr-ab').textContent='—'; $('hdr-ab').className='badge';
  $('msgs').innerHTML=`<div class="welcome"><h2>New Conversation</h2><p>Fresh session — new A/B variant assigned. Traces appear in APM as a new conversation group.</p></div>`;
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
  const msg=ta.value.trim(); if(!msg) return;
  ta.value=''; ta.style.height='42px';
  $('send-btn').disabled=true;
  const w=$('msgs').querySelector('.welcome'); if(w) w.remove();
  addBubble('user', msg);
  const typing=addTyping();
  try{
    const body={message:msg}; if(sid) body.session_id=sid;
    const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const d=await r.json();
    typing.remove(); sid=d.session_id;

    // Tool call chips (shown before agent bubble)
    if(d.tool_calls && d.tool_calls.length>0) addToolChips(d.tool_calls);

    addBubble('agent', d.text);
    const msgIdx=turns;
    addPills(d);
    addFeedback(d.trace_id, msgIdx);
    updateAB(d);
    update(d);
  }catch(e){typing.remove();addBubble('agent','**Error:** '+e.message);}
  finally{$('send-btn').disabled=false; ta.focus();}
}
$('send-btn').onclick=send;

function addBubble(who, text){
  const d=document.createElement('div'); d.className='msg '+who;
  d.innerHTML=`<div class="avatar">${who==='user'?'👤':'⚡'}</div><div class="bubble">${md(text)}</div>`;
  $('msgs').appendChild(d); scroll(); return d;
}

function addToolChips(tools){
  const wrap=document.createElement('div'); wrap.className='tools-row';
  tools.forEach((t,i)=>{
    const chip=document.createElement('div'); chip.className='tool-chip';
    chip.innerHTML=`<span>🔧</span><span class="tc-name">${t.name}(${JSON.stringify(JSON.parse(t.args_summary||'{}')).slice(0,40)})</span><span class="tc-dur">${t.duration_ms}ms</span>`;
    const detail=document.createElement('div'); detail.className='tool-detail';
    detail.textContent=`→ ${t.result_summary}`;
    chip.onclick=()=>detail.classList.toggle('open');
    wrap.appendChild(chip); wrap.appendChild(detail);
  });
  $('msgs').appendChild(wrap); scroll();
}

function addPills(d){
  const r=document.createElement('div'); r.className='pills-row';
  const abClass=d.variant==='B'?'ab-b':'ab-a';
  r.innerHTML=`
    <a class="pill trace" href="${cfg.apm_url||'#'}" target="_blank">📡 ${(d.trace_id||'').substring(0,8)}…</a>
    <span class="pill otlp">OTLP</span>
    <span class="pill ${abClass}">${d.variant||'A'}:${(d.model_used||'').split('-').slice(-2).join('-')}</span>
    <span class="pill dim">${(d.in_tokens||0).toLocaleString()}↑ ${(d.out_tokens||0).toLocaleString()}↓</span>
    <span class="pill dim">${d.latency_ms}ms</span>
    <span class="pill dim">$${(d.cost_usd||0).toFixed(5)}</span>
    ${d.pii_detected?'<span class="pill pii">⚠️ PII</span>':''}
    ${d.is_error?'<span class="pill err">error</span>':''}`;
  $('msgs').appendChild(r); scroll();
}

function addFeedback(traceId, msgIdx){
  const r=document.createElement('div'); r.className='feedback-row';
  r.innerHTML=`<span class="fb-lbl">Helpful?</span>
    <button class="fb-btn" id="up-${msgIdx}">👍</button>
    <button class="fb-btn" id="dn-${msgIdx}">👎</button>`;
  $('msgs').appendChild(r);

  const vote=(rating)=>{
    fetch('/api/feedback',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({session_id:sid,trace_id:traceId,rating})});
    $(`up-${msgIdx}`).className='fb-btn'+(rating===1?' voted-up':'');
    $(`dn-${msgIdx}`).className='fb-btn'+(rating===-1?' voted-down':'');
    if(rating===1){totUp++;$('s-up').textContent=totUp;}
    else{totDown++;$('s-down').textContent=totDown;}
  };
  $(`up-${msgIdx}`).onclick=()=>vote(1);
  $(`dn-${msgIdx}`).onclick=()=>vote(-1);
  scroll();
}

function addTyping(){
  const d=document.createElement('div'); d.className='msg agent';
  d.innerHTML='<div class="avatar">⚡</div><div class="typing"><div class="dots"><div class="dot"></div><div class="dot"></div><div class="dot"></div></div>Thinking…</div>';
  $('msgs').appendChild(d); scroll(); return d;
}
function scroll(){ const m=$('msgs'); m.scrollTop=m.scrollHeight; }

function updateAB(d){
  $('t-variant').textContent = d.variant||'A';
  $('t-model-used').textContent = d.model_used||'—';
  const abClass=d.variant==='B'?'ab-b':'ab-a';
  $('hdr-ab').textContent=(d.variant==='B'?'B:lighter':'A:primary');
  $('hdr-ab').className='badge '+abClass;
}

function update(d){
  $('t-lat').textContent   = d.latency_ms+'ms';
  $('t-in').textContent    = (d.in_tokens||0).toLocaleString();
  $('t-out').textContent   = (d.out_tokens||0).toLocaleString();
  $('t-cost').textContent  = '$'+(d.cost_usd||0).toFixed(5);
  $('t-tools').textContent = (d.tool_calls||[]).length;
  $('t-tid').textContent   = d.trace_id||'—';
  turns++; totIn+=d.in_tokens||0; totOut+=d.out_tokens||0;
  totCost+=d.cost_usd||0; totTools+=(d.tool_calls||[]).length;
  statUpdate();
}
function statUpdate(){
  $('s-turns').textContent = turns;
  $('s-in').textContent    = totIn.toLocaleString();
  $('s-out').textContent   = totOut.toLocaleString();
  $('s-cost').textContent  = '$'+totCost.toFixed(6);
  $('s-tools').textContent = totTools;
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
                "model_a":          MODEL_A,
                "model_b":          MODEL_B,
                "ab_ratio":         AB_RATIO,
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
                result = chat_turn(
                    body["message"],
                    session["conv_id"],
                    session["history"],
                    session["model"],
                    session["variant"],
                )
                session["history"].append({"role": "user",  "text": body["message"]})
                session["history"].append({"role": "model", "text": result["text"]})
                session["history"] = session["history"][-40:]
                result["session_id"] = sid
                self.send_json(200, result)
            except Exception as e:
                logger.exception("chat_turn failed")
                self.send_json(500, {"error": str(e), "session_id": sid})

        elif self.path == "/api/feedback":
            # User thumbs up/down — log as OTel record correlated to trace_id
            rating    = body.get("rating", 0)       # 1 = positive, -1 = negative
            trace_id  = body.get("trace_id", "")
            session_id = body.get("session_id", "")

            label = "positive" if rating > 0 else "negative"
            feedback_counter.add(1, {
                "feedback.rating": label,
                "gen_ai.system":   "google_gemini",
            })
            logger.info("User feedback received", extra={
                "feedback.rating":     label,
                "feedback.score":      rating,
                "feedback.trace_id":   trace_id,
                "session_id":          session_id,
            })
            self.send_json(200, {"ok": True})

        else:
            self.send_json(404, {"error": "not found"})


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if _PSUTIL:
        psutil.cpu_percent(interval=None)
        threading.Thread(target=_psutil_scrape_loop, daemon=True, name="psutil-scrape").start()

    logger.info(f"Web agent v2 starting — model_a={MODEL_A} model_b={MODEL_B} "
                f"ab_ratio={AB_RATIO} host={HOST_NAME} port={PORT} psutil={_PSUTIL}")

    print(f"\n{'─'*64}")
    print(f"  Elastic AI Agent — Full OTel Demo  v2.0")
    print(f"{'─'*64}")
    print(f"  Chat UI  →  http://localhost:{PORT}")
    print(f"  APM UI   →  {KB_ENDPOINT}/app/apm/services/{SVC_NAME}/overview")
    print(f"  Host     :  {HOST_NAME}")
    print(f"  Model A  :  {MODEL_A}  (primary, {round((1-AB_RATIO)*100)}%)")
    print(f"  Model B  :  {MODEL_B}  (lighter, {round(AB_RATIO*100)}%)")
    print(f"{'─'*64}")
    print(f"  OTel signals → {OTLP_ENDPOINT}")
    print(f"    📡 Traces  (+ tool spans)  → traces-generic.otel-default")
    print(f"    📋 Logs    (+ feedback)    → logs-generic.otel-default")
    print(f"    📊 Metrics (gen_ai.*, guardrails, feedback, host)")
    print(f"{'─'*64}")
    print(f"  Features: tool calling · A/B routing · PII guardrails · feedback loop")
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
