"""Shared connection settings. Secrets stay in the ignored web_agent/.env file."""
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def load_environment():
    env_file = Path(__file__).with_name('.env')
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            if key.startswith('export '):
                key = key[7:]
            os.environ.setdefault(key.strip(), value.strip().strip('"\''))


def endpoints(env=None):
    env = os.environ if env is None else env
    es = env.get('ES_ENDPOINT', '').rstrip('/')
    bulk = env.get('ELASTIC_BULK_ENDPOINT', es).rstrip('/')
    otlp = (env.get('OTEL_EXPORTER_OTLP_ENDPOINT') or
            env.get('OTEL_INGEST_ENDPOINT') or '').rstrip('/')
    if not otlp and '.ingest.' in urlsplit(bulk).netloc:
        parsed = urlsplit(bulk)
        otlp = urlunsplit((parsed.scheme, parsed.netloc, '', '', ''))
    if not otlp:
        raise ValueError('Set OTEL_EXPORTER_OTLP_ENDPOINT to the managed OTLP URL from Elastic Cloud.')
    if urlsplit(otlp).path.rstrip('/') == '/_es':
        otlp = otlp[:-4]
    parsed = urlsplit(otlp)
    if parsed.scheme not in ('https', 'http') or not parsed.netloc:
        raise ValueError('OTEL_EXPORTER_OTLP_ENDPOINT must be an HTTP(S) URL.')
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Keep authentication in ES_API_KEY, not in the endpoint URL.')
    kb = env.get('KIBANA_ENDPOINT', '').rstrip('/')
    if not kb and '.es.' in urlsplit(es).netloc:
        kb = es.replace('.es.', '.kb.')
    return {
        'elasticsearch': es,
        'bulk': bulk,
        'otlp': otlp,
        'kibana': kb,
        **{signal: env.get('OTEL_EXPORTER_OTLP_' + signal.upper() + '_ENDPOINT')
           or otlp + '/v1/' + signal for signal in ('traces', 'metrics', 'logs')},
    }
