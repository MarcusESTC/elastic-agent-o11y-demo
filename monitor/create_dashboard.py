#!/usr/bin/env python3
"""
AI Agent O11y — Kibana Dashboard Creator (Kibana 9 serverless)

Creates (or overwrites) a monitoring dashboard with Vega-Lite visualizations:
  • OTel Traces over time (area chart)
  • OTel Logs over time (area chart)
  • OTel Metrics over time (area chart)
  • APM Services by trace volume (horizontal bar)
  • Info panel with links

Usage:
    python3 monitor/create_dashboard.py           # create / overwrite
    python3 monitor/create_dashboard.py --dry-run # print NDJSON, don't upload
"""
import os, sys, json, subprocess, tempfile, urllib.request, urllib.error
from pathlib import Path

# ── Credentials ─────────────────────────────────────────────────────────────
_env = Path(__file__).parent.parent / "web_agent" / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        if line.strip() and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            os.environ.setdefault(k.strip(), v.strip())

ES  = os.environ.get('ES_ENDPOINT', '').rstrip('/')
KEY = os.environ.get('ES_API_KEY', '')
if not ES or not KEY:
    sys.exit("❌  ES_ENDPOINT and ES_API_KEY must be set (check web_agent/.env)")
KB = os.environ.get('KIBANA_ENDPOINT', ES.replace('.es.', '.kb.')).rstrip('/')

# ── Vega-Lite specs ──────────────────────────────────────────────────────────

def _signal_ts_spec(index, title, color):
    """Area chart: doc count per hour for one OTel data stream."""
    return {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "title": title,
        "background": "transparent",
        "config": {
            "axis": {"labelColor": "#8b949e", "titleColor": "#8b949e",
                     "gridColor": "#21262d", "tickColor": "#21262d"},
            "view": {"stroke": "transparent"},
        },
        "data": {
            "url": {
                "%context%": True,
                "%timefield%": "@timestamp",
                "index": index,
                "body": {
                    "size": 0,
                    "aggs": {
                        "by_time": {
                            "date_histogram": {
                                "field": "@timestamp",
                                "calendar_interval": "1h",
                                "min_doc_count": 0,
                                "extended_bounds": {
                                    "min": {"%timefilter%": "min"},
                                    "max": {"%timefilter%": "max"}
                                }
                            }
                        }
                    }
                }
            },
            "format": {"property": "aggregations.by_time.buckets"}
        },
        "mark": {"type": "area", "color": color, "opacity": 0.7,
                 "line": {"color": color}},
        "encoding": {
            "x": {"field": "key_as_string", "type": "temporal",
                  "title": None, "axis": {"labelAngle": -30}},
            "y": {"field": "doc_count", "type": "quantitative",
                  "title": "Documents / hour"}
        }
    }

def _apm_services_spec():
    """Horizontal bar: APM services by trace volume, Gemini agent highlighted."""
    return {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "title": "APM Services — Trace Volume",
        "background": "transparent",
        "config": {
            "axis": {"labelColor": "#8b949e", "titleColor": "#8b949e",
                     "gridColor": "#21262d"},
            "view": {"stroke": "transparent"},
        },
        "data": {
            "url": {
                "%context%": False,
                "index": "traces-apm*,traces-generic.otel-default",
                "body": {
                    "size": 0,
                    "aggs": {
                        "services": {
                            "terms": {
                                "field": "service.name",
                                "size": 15,
                                "order": {"_count": "desc"}
                            }
                        }
                    }
                }
            },
            "format": {"property": "aggregations.services.buckets"}
        },
        "transform": [
            {
                "calculate": "test(/gemini|agent/, datum.key) ? 'live-agent' : 'synthetic'",
                "as": "kind"
            }
        ],
        "mark": "bar",
        "encoding": {
            "y": {"field": "key", "type": "nominal", "sort": "-x",
                  "title": None},
            "x": {"field": "doc_count", "type": "quantitative",
                  "title": "Traces (all time)"},
            "color": {
                "field": "kind", "type": "nominal",
                "scale": {
                    "domain": ["live-agent", "synthetic"],
                    "range":  ["#00BFB3",   "#3d444d"]
                },
                "legend": {"title": None}
            },
            "tooltip": [
                {"field": "key",       "title": "Service"},
                {"field": "doc_count", "title": "Traces"},
            ]
        }
    }

def _combined_signals_spec():
    """Multi-series area chart comparing all 3 OTel signal types over time."""
    # Overlay 3 layers (one per index) in one chart
    base = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "title": "All OTel Signals — Docs / Hour",
        "background": "transparent",
        "config": {
            "axis": {"labelColor": "#8b949e", "titleColor": "#8b949e",
                     "gridColor": "#21262d"},
            "view": {"stroke": "transparent"},
        },
        "layer": []
    }
    signals = [
        ("traces-generic.otel-default",  "Traces",  "#7B61FF"),
        ("logs-generic.otel-default",    "Logs",    "#00BFB3"),
        ("metrics-generic.otel-default", "Metrics", "#F04E98"),
    ]
    for idx, label, color in signals:
        base["layer"].append({
            "data": {
                "url": {
                    "%context%": True,
                    "%timefield%": "@timestamp",
                    "index": idx,
                    "body": {
                        "size": 0,
                        "aggs": {
                            "by_time": {
                                "date_histogram": {
                                    "field": "@timestamp",
                                    "calendar_interval": "1h",
                                    "min_doc_count": 0
                                }
                            }
                        }
                    }
                },
                "format": {"property": "aggregations.by_time.buckets"}
            },
            "transform": [{"calculate": f'"{label}"', "as": "signal"}],
            "mark": {"type": "line", "color": color, "strokeWidth": 2},
            "encoding": {
                "x": {"field": "key_as_string", "type": "temporal",
                      "title": None, "axis": {"labelAngle": -30}},
                "y": {"field": "doc_count", "type": "quantitative",
                      "title": "Docs / hour"},
                "color": {
                    "field": "signal", "type": "nominal",
                    "scale": {"range": [c for _, _, c in signals]},
                    "legend": {"title": None, "orient": "bottom"}
                }
            }
        })
    return base


# ── Visualization saved objects ──────────────────────────────────────────────

def _vis_obj(vis_id, title, spec):
    return {
        "type": "visualization",
        "id": vis_id,
        "attributes": {
            "title": title,
            "visState": json.dumps({"type": "vega", "params": {"spec": json.dumps(spec)}, "aggs": []}),
            "uiStateJSON": "{}",
            "kibanaSavedObjectMeta": {
                "searchSourceJSON": json.dumps(
                    {"query": {"language": "kuery", "query": ""}, "filter": []}
                )
            }
        },
        "references": [],
        "managed": False,
    }


# ── Dashboard panel helpers ──────────────────────────────────────────────────

def _vis_panel(panel_id, vis_id, x, y, w, h, title):
    """Panel that references a saved visualization."""
    return {
        "type": "visualization",
        "panelIndex": panel_id,
        "gridData": {"x": x, "y": y, "w": w, "h": h, "i": panel_id},
        "panelRefName": f"panel_{panel_id}",
        "embeddableConfig": {
            "title": title,
            "hidePanelTitles": False,
        }
    }

def _mkd_panel(panel_id, md, x, y, w, h):
    return {
        "type": "markdown",
        "panelIndex": panel_id,
        "gridData": {"x": x, "y": y, "w": w, "h": h, "i": panel_id},
        "embeddableConfig": {
            "title": "",
            "hidePanelTitles": True,
            "attributes": {"markdown": md, "openLinksInNewTab": True}
        }
    }


# ── Build everything ─────────────────────────────────────────────────────────

DASHBOARD_ID = "agent-o11y-index-health"
SIGNALS = [
    ("traces-generic.otel-default",  "otel-traces-ts",   "📡 OTel Traces",  "#7B61FF"),
    ("logs-generic.otel-default",    "otel-logs-ts",     "📋 OTel Logs",    "#00BFB3"),
    ("metrics-generic.otel-default", "otel-metrics-ts",  "📊 OTel Metrics", "#F04E98"),
]

def build_objects():
    objects = []

    # 1) Individual signal time-series visualizations
    for idx, vis_id, label, color in SIGNALS:
        title = f"{label} — Docs / Hour"
        objects.append(_vis_obj(vis_id, title, _signal_ts_spec(idx, title, color)))

    # 2) Combined overlay
    objects.append(_vis_obj("otel-combined-ts", "All OTel Signals Over Time",
                            _combined_signals_spec()))

    # 3) APM services
    objects.append(_vis_obj("apm-services-bar", "APM Services — Trace Volume",
                            _apm_services_spec()))

    # 4) Dashboard
    info_md = (
        f"## 🔍 AI Agent O11y — Index Health Monitor\n\n"
        f"Live OTel signals from the **Gemini Demo Agent** → Elastic via OTLP/HTTP.  "
        f"Run `python3 monitor/health_check.py` for a CLI snapshot.\n\n"
        f"[🚀 APM Services]({KB}/app/apm/services)   |   "
        f"[💬 Chat UI](http://localhost:5601)   |   "
        f"[🔍 Discover]({KB}/app/discover)"
    )

    panels = [_mkd_panel("mkd0", info_md, 0, 0, 48, 7)]

    # Row 1: 3 individual signal charts
    for i, (_, vis_id, label, _c) in enumerate(SIGNALS):
        panels.append(_vis_panel(f"sig{i}", vis_id, i * 16, 7, 16, 14, label))

    # Row 2: combined + APM (split 24/24)
    panels.append(_vis_panel("comb0", "otel-combined-ts", 0,  21, 24, 14, "OTel Signals Combined"))
    panels.append(_vis_panel("apm0",  "apm-services-bar", 24, 21, 24, 14, "APM Services"))

    # References — must list each viz referenced by panel_ref_name
    references = []
    for i, (_, vis_id, label, _c) in enumerate(SIGNALS):
        references.append({"id": vis_id, "name": f"panel_sig{i}", "type": "visualization"})
    references.append({"id": "otel-combined-ts", "name": "panel_comb0", "type": "visualization"})
    references.append({"id": "apm-services-bar", "name": "panel_apm0",  "type": "visualization"})

    dashboard = {
        "type": "dashboard",
        "id": DASHBOARD_ID,
        "attributes": {
            "title":       "AI Agent O11y — Index Health",
            "description": "Monitoring dashboard: OTel traces, logs, metrics + APM service distribution.",
            "panelsJSON":  json.dumps(panels),
            "optionsJSON": json.dumps({"useMargins": True, "syncColors": False,
                                       "hidePanelTitles": False}),
            "timeRestore": True,
            "timeFrom":    "now-7d",
            "timeTo":      "now",
            "refreshInterval": {"pause": False, "value": 60000},
            "kibanaSavedObjectMeta": {
                "searchSourceJSON": json.dumps(
                    {"query": {"language": "kuery", "query": ""}, "filter": []}
                )
            }
        },
        "references": references,
        "managed": False,
    }
    objects.append(dashboard)
    return objects


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    dry = "--dry-run" in sys.argv
    objects = build_objects()
    ndjson  = "\n".join(json.dumps(o) for o in objects)

    if dry:
        print(ndjson)
        return 0

    import shutil
    if not shutil.which("curl"):
        sys.exit("❌  curl not found — install it or use --dry-run")

    tmp = tempfile.NamedTemporaryFile(suffix=".ndjson", mode="w", delete=False)
    tmp.write(ndjson)
    tmp.close()

    cmd = [
        "curl", "-s", "-X", "POST",
        f"{KB}/api/saved_objects/_import?overwrite=true",
        "-H", f"Authorization: ApiKey {KEY}",
        "-H", "kbn-xsrf: true",
        "-F", f"file=@{tmp.name}",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    os.unlink(tmp.name)

    try:
        result = json.loads(proc.stdout)
    except Exception:
        print(f"❌  Unexpected response:\n{proc.stdout[:600]}")
        return 1

    if result.get("success"):
        count = result.get("successCount", "?")
        print(f"✅  Dashboard created / updated  ({count} objects imported)")
        print(f"    {KB}/app/dashboards#/view/{DASHBOARD_ID}")
    else:
        errors = result.get("errors", [])
        print(f"⚠   Import response (success={result.get('success')}):")
        print(json.dumps(result, indent=2)[:800])
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
