#!/usr/bin/env python3
"""
AI Agent O11y — Elasticsearch Health Monitor
Checks OTel indices, APM services, and demo server health.

Usage:
    python3 monitor/health_check.py          # pretty terminal output
    python3 monitor/health_check.py --json   # machine-readable JSON
"""
import os, sys, json, datetime, urllib.request, urllib.error
from pathlib import Path

# ── Load .env ──────────────────────────────────────────────────────────────
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

# ── ANSI helpers ───────────────────────────────────────────────────────────
USE_COLOR = sys.stdout.isatty() and '--json' not in sys.argv
G  = '\033[92m' if USE_COLOR else ''   # green
Y  = '\033[93m' if USE_COLOR else ''   # yellow
R  = '\033[91m' if USE_COLOR else ''   # red
C  = '\033[96m' if USE_COLOR else ''   # cyan
B  = '\033[1m'  if USE_COLOR else ''   # bold
D  = '\033[2m'  if USE_COLOR else ''   # dim
X  = '\033[0m'  if USE_COLOR else ''   # reset

# ── ES request helper ──────────────────────────────────────────────────────
def es(path, body=None, method=None):
    method = method or ('POST' if body else 'GET')
    req = urllib.request.Request(f"{ES}/{path.lstrip('/')}", method=method)
    req.add_header('Authorization', f'ApiKey {KEY}')
    req.add_header('Content-Type', 'application/json')
    if body:
        req.data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {'_error': f'HTTP {e.code}: {e.reason}'}
    except Exception as e:
        return {'_error': str(e)}

# ── Checks ─────────────────────────────────────────────────────────────────
OTEL_SIGNALS = [
    ('traces-generic.otel-default',  '📡 Traces '),
    ('logs-generic.otel-default',    '📋 Logs   '),
    ('metrics-generic.otel-default', '📊 Metrics'),
]

APM_INDICES = 'traces-apm*,logs-apm*'

def check_signal(alias):
    """Doc counts + freshness for one OTel data stream."""
    count  = es(f'{alias}/_count')
    latest = es(f'{alias}/_search', {
        'size': 1, 'sort': [{'@timestamp': 'desc'}],
        '_source': ['@timestamp'], 'query': {'match_all': {}}
    })
    c1h = es(f'{alias}/_count', {'query': {'range': {'@timestamp': {'gte': 'now-1h'}}}})
    c24 = es(f'{alias}/_count', {'query': {'range': {'@timestamp': {'gte': 'now-24h'}}}})
    hits = latest.get('hits', {}).get('hits', [])
    return {
        'total':     count.get('count', -1),
        'last_1h':   c1h.get('count', -1),
        'last_24h':  c24.get('count', -1),
        'latest_ts': hits[0]['_source'].get('@timestamp') if hits else None,
        'error':     count.get('_error'),
    }

def check_apm_services(top=15):
    r = es('traces-apm*,traces-generic.otel-default/_search', {
        'size': 0,
        'aggs': {'svcs': {'terms': {'field': 'service.name', 'size': top}}}
    })
    return [(b['key'], b['doc_count'])
            for b in r.get('aggregations', {}).get('svcs', {}).get('buckets', [])]

def check_error_rate():
    r = es(f'{APM_INDICES}/_count', {
        'query': {'range': {'@timestamp': {'gte': 'now-1h'}}}
    })
    return r.get('count', -1)

def check_server(port=5601):
    try:
        with urllib.request.urlopen(f'http://localhost:{port}/api/config', timeout=3) as r:
            return r.status == 200
    except:
        return False

def age(ts):
    if not ts:
        return 'never'
    try:
        t = datetime.datetime.fromisoformat(ts.replace('Z', '+00:00'))
        d = datetime.datetime.now(datetime.timezone.utc) - t
        s = int(d.total_seconds())
        if s < 3600:   return f'{s // 60}m ago'
        if s < 86400:  return f'{s // 3600}h ago'
        return f'{s // 86400}d ago'
    except:
        return ts

def badge(s):
    if s.get('error'):              return R + '✗ ERR'  + X
    if s['last_1h']  > 0:           return G + '● LIVE' + X
    if s['last_24h'] > 0:           return Y + '◐ STALE'+ X
    return                                 R + '○ DARK' + X

def is_healthy(s):
    return not s.get('error') and (s['last_1h'] > 0 or s['last_24h'] > 0)

# ── Main ───────────────────────────────────────────────────────────────────
def run():
    now  = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    emit_json = '--json' in sys.argv

    signals  = {alias: check_signal(alias) for alias, _ in OTEL_SIGNALS}
    services = check_apm_services()
    apm_errs = check_error_rate()
    server   = check_server()
    healthy  = all(is_healthy(signals[a]) for a, _ in OTEL_SIGNALS) and server

    if emit_json:
        print(json.dumps({
            'timestamp': now,
            'healthy': healthy,
            'server_up': server,
            'apm_errors_1h': apm_errs,
            'signals': {
                alias: signals[alias] for alias, _ in OTEL_SIGNALS
            },
            'services': dict(services),
        }, indent=2))
        return 0 if healthy else 1

    # ── Pretty output ──────────────────────────────────────────────────────
    W = 64
    print(f"\n{B}{'━'*W}{X}")
    print(f"{B}  🔍  AI Agent O11y — Health Check{X}   {D}{now}{X}")
    print(f"{B}{'━'*W}{X}\n")

    # OTel signals table
    print(f"{B}  OTel Signals{X}")
    print(f"  {D}{'Signal':<12} {'Total':>10}  {'1h':>6}  {'24h':>8}  {'Latest':<16} Status{X}")
    print(f"  {'─'*60}")
    for alias, label in OTEL_SIGNALS:
        s  = signals[alias]
        tot = f"{s['total']:,}" if s['total'] >= 0 else 'ERR'
        h1  = str(s['last_1h'])  if s['last_1h']  >= 0 else 'ERR'
        h24 = str(s['last_24h']) if s['last_24h'] >= 0 else 'ERR'
        ag  = age(s['latest_ts'])
        print(f"  {label}  {tot:>10}  {h1:>6}  {h24:>8}  {ag:<16} {badge(s)}")

    # Demo server
    print(f"\n{B}  Demo Server{X}")
    if server:
        print(f"  localhost:5601   {G}● UP{X}  (web UI + /api/chat responding)")
    else:
        print(f"  localhost:5601   {R}○ DOWN{X}  — run: bash start.sh")

    # APM services
    print(f"\n{B}  APM Services  {D}(all-time traces){X}")
    if services:
        max_count = max(c for _, c in services)
        for svc, count in services:
            bar  = '█' * min(18, max(1, int(count / max_count * 18)))
            hi   = C if any(t in svc for t in ('gemini', 'agent', 'otel')) else D
            print(f"  {hi}{svc:<36}{X}  {count:>9,}  {D}{bar}{X}")
    else:
        print(f"  {Y}No services found{X}")

    # APM errors last hour
    print(f"\n{B}  APM Errors / App Logs (last 1h){X}")
    if apm_errs >= 0:
        color = R if apm_errs > 50 else Y if apm_errs > 0 else G
        print(f"  {color}{apm_errs:,} error docs{X}")
    else:
        print(f"  {D}Unable to query{X}")

    # Kibana link
    print(f"\n{B}  Links{X}")
    print(f"  {C}APM{X}        {KB}/app/apm/services")
    print(f"  {C}Dashboards{X} {KB}/app/dashboards")
    print(f"  {C}Chat UI{X}    http://localhost:5601")

    # Summary
    print(f"\n{B}{'━'*W}{X}")
    if healthy:
        print(f"  {G}{B}✓ All systems healthy{X}")
    else:
        parts = []
        for alias, label in OTEL_SIGNALS:
            if not is_healthy(signals[alias]):
                parts.append(label.strip())
        if not server:
            parts.append('Demo server')
        print(f"  {Y}{B}⚠ Issues: {', '.join(parts)}{X}")
        if any(not is_healthy(signals[a]) for a, _ in OTEL_SIGNALS):
            print(f"  {D}  Traces/logs go stale when no demo conversations run.{X}")
            print(f"  {D}  Open http://localhost:5601 and send a message to refresh.{X}")
    print(f"{B}{'━'*W}{X}\n")

    return 0 if healthy else 1

if __name__ == '__main__':
    sys.exit(run())
