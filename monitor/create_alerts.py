#!/usr/bin/env python3
"""
AI Agent O11y — Kibana Alerting Rules Creator

Creates (or updates) Kibana rules that monitor OTel index health natively:
  1. OTel Traces — Data Freshness    (.index-threshold, fires if <1 trace in 24h)
  2. OTel Logs   — Data Freshness    (.index-threshold, fires if <1 log in 24h)
  3. OTel Metrics — Continuous Flow  (.index-threshold, fires if <100 metrics in 1h)
  4. APM — Gemini Agent Error Rate   (apm.error_rate, fires if >10 errors/min)

All rules appear in:
  Stack Management → Alerts and Insights → Rules

Usage:
    python3 monitor/create_alerts.py
    python3 monitor/create_alerts.py --list     # list existing o11y rules
    python3 monitor/create_alerts.py --delete   # delete rules created by this script
"""
import os, sys, json, urllib.request, urllib.error
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
    sys.exit("❌  ES_ENDPOINT / ES_API_KEY not set (check web_agent/.env)")
KB = os.environ.get('KIBANA_ENDPOINT', ES.replace('.es.', '.kb.')).rstrip('/')

# ── Kibana helper ────────────────────────────────────────────────────────────
def kb(path, body=None, method=None):
    method = method or ('POST' if body is not None else 'GET')
    req = urllib.request.Request(f"{KB}/{path.lstrip('/')}", method=method)
    req.add_header('Authorization', f'ApiKey {KEY}')
    req.add_header('Content-Type',  'application/json')
    req.add_header('kbn-xsrf',      'true')
    if body is not None:
        req.data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b'{}')

# ── Rule definitions ─────────────────────────────────────────────────────────
# Tag all our rules so we can find/delete them later
TAG = 'agent-o11y-monitor'

RULES = [
    {
        'name':         '🔴 OTel Traces — Data Freshness',
        'rule_type_id': '.index-threshold',
        'consumer':     'stackAlerts',
        'schedule':     {'interval': '1h'},
        'params': {
            'index':               ['traces-generic.otel-default'],
            'timeField':           '@timestamp',
            'aggType':             'count',
            'groupBy':             'all',
            'threshold':           [1],
            'thresholdComparator': '<',
            'timeWindowSize':      24,
            'timeWindowUnit':      'h',
        },
        'actions': [],
        'tags': [TAG, 'otel', 'traces'],
        '_description': 'Fires when no OTel traces arrive for 24h — demo may be idle',
    },
    {
        'name':         '🔴 OTel Logs — Data Freshness',
        'rule_type_id': '.index-threshold',
        'consumer':     'stackAlerts',
        'schedule':     {'interval': '1h'},
        'params': {
            'index':               ['logs-generic.otel-default'],
            'timeField':           '@timestamp',
            'aggType':             'count',
            'groupBy':             'all',
            'threshold':           [1],
            'thresholdComparator': '<',
            'timeWindowSize':      24,
            'timeWindowUnit':      'h',
        },
        'actions': [],
        'tags': [TAG, 'otel', 'logs'],
        '_description': 'Fires when no OTel logs arrive for 24h',
    },
    {
        'name':         '🟡 OTel Metrics — Continuous Flow',
        'rule_type_id': '.index-threshold',
        'consumer':     'stackAlerts',
        'schedule':     {'interval': '1h'},
        'params': {
            'index':               ['metrics-generic.otel-default'],
            'timeField':           '@timestamp',
            'aggType':             'count',
            'groupBy':             'all',
            'threshold':           [50],
            'thresholdComparator': '<',
            'timeWindowSize':      1,
            'timeWindowUnit':      'h',
        },
        'actions': [],
        'tags': [TAG, 'otel', 'metrics'],
        '_description': 'Fires when metric ingest drops below 50 docs/hour (should always be flowing)',
    },
    {
        'name':         '⚠️ APM — Gemini Agent Error Rate',
        'rule_type_id': 'apm.error_rate',
        'consumer':     'apm',
        'schedule':     {'interval': '5m'},
        'params': {
            'serviceName': 'gemini-demo-agent',
            'environment': 'ENVIRONMENT_ALL',
            'threshold':   10,
            'windowSize':  5,
            'windowUnit':  'm',
        },
        'actions': [],
        'tags': [TAG, 'apm', 'gemini'],
        '_description': 'Fires when Gemini agent error count exceeds 10 per 5min window',
    },
    {
        'name':         '⚠️ APM — Gemini Agent Latency (p99)',
        'rule_type_id': 'apm.transaction_duration',
        'consumer':     'apm',
        'schedule':     {'interval': '5m'},
        'params': {
            'serviceName':   'gemini-demo-agent',
            'environment':   'ENVIRONMENT_ALL',
            'threshold':     30000,          # 30 000ms = 30s — Gemini can be slow
            'windowSize':    5,
            'windowUnit':    'm',
            'aggregationType': '99th',
        },
        'actions': [],
        'tags': [TAG, 'apm', 'gemini'],
        '_description': 'Fires when Gemini agent p99 latency exceeds 30s',
    },
]

# ── Create or update rules ───────────────────────────────────────────────────
def find_existing_rules():
    """Find rules tagged with our tag."""
    status, resp = kb(f'api/alerting/rules/_find?filter=alert.attributes.tags:"{TAG}"&per_page=50')
    if status != 200:
        return []
    return resp.get('data', [])

def create_or_update(rule_def):
    name = rule_def['name']
    description = rule_def.pop('_description', '')
    existing = find_existing_rules()
    match = next((r for r in existing if r['name'] == name), None)

    payload = {k: v for k, v in rule_def.items() if k != '_description'}

    if match:
        rid = match['id']
        status, resp = kb(f'api/alerting/rule/{rid}', payload, method='PUT')
        verb = 'Updated'
    else:
        status, resp = kb('api/alerting/rule', payload)
        verb = 'Created'

    ok = status in (200, 201)
    rid = resp.get('id', '?')
    print(f"  {'✓' if ok else '✗'} {verb}: {name}")
    if not ok:
        err = resp.get('message', json.dumps(resp)[:120])
        print(f"       HTTP {status}: {err}")
    return ok, rid


def list_rules():
    rules = find_existing_rules()
    if not rules:
        print("  No agent-o11y-monitor rules found.")
        return
    for r in rules:
        enabled = '● ' if r.get('enabled') else '○ '
        sched = r.get('schedule', {}).get('interval', '?')
        print(f"  {enabled}{r['name']:<50} every {sched}  id={r['id'][:8]}…")


def delete_rules():
    rules = find_existing_rules()
    if not rules:
        print("  Nothing to delete.")
        return
    for r in rules:
        status, _ = kb(f"api/alerting/rule/{r['id']}", method='DELETE')
        print(f"  {'✓' if status == 204 else '✗'} Deleted: {r['name']}")


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    if '--list' in sys.argv:
        print(f"\n  Kibana Rules tagged [{TAG}]\n")
        list_rules()
        print()
        return 0

    if '--delete' in sys.argv:
        print(f"\n  Deleting rules tagged [{TAG}]...\n")
        delete_rules()
        print()
        return 0

    print(f"\n  Creating Kibana alerting rules on {KB}\n")
    all_ok = True
    for rule in RULES:
        import copy
        ok, _ = create_or_update(copy.deepcopy(rule))
        if not ok:
            all_ok = False

    rules_url = f"{KB}/app/management/insightsAndAlerting/triggersActions/rules"
    print(f"\n  Rules UI → {rules_url}\n")
    return 0 if all_ok else 1


if __name__ == '__main__':
    sys.exit(main())
