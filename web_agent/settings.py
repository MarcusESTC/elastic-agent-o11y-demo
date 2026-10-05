"""Local connection configuration. Credentials never appear in API responses."""
import json
import os
import re
import secrets
import tempfile
from pathlib import Path
from privacy import PIPELINE, TRACE_STREAM, LOG_STREAM
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit, quote
from urllib.request import Request, build_opener, HTTPRedirectHandler

ENV_FILE = Path(__file__).with_name('.env')
CSRF_TOKEN = secrets.token_urlsafe(32)
FIELDS = {'elasticsearch_endpoint', 'kibana_endpoint', 'otlp_endpoint', 'api_key'}


class SettingsError(ValueError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def endpoint(value, label):
    if not isinstance(value, str) or any(c.isspace() for c in value):
        raise SettingsError(label + ' must be an HTTPS URL without spaces.')
    value = value.rstrip('/')
    try:
        parsed = urlsplit(value)
        port = parsed.port
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path or (port is not None and port < 1)):
            raise ValueError()
    except ValueError:
        raise SettingsError(label + ' must be an HTTPS base URL, without a path, credentials or query.') from None
    return urlunsplit(('https', parsed.netloc.lower(), '', '', ''))


def cloud_endpoints(es):
    parsed = urlsplit(es)
    if parsed.hostname.endswith('.elastic.cloud') and '.es.' in parsed.hostname:
        return {name: urlunsplit(('https', parsed.netloc.replace('.es.', '.' + component + '.', 1), '', '', ''))
                for name, component in [('kibana_endpoint', 'kb'), ('otlp_endpoint', 'ingest')]}
    return {}


def candidate(body, current=None):
    current = os.environ if current is None else current
    if not isinstance(body, dict) or set(body) - FIELDS:
        raise SettingsError('Unrecognized connection settings.')
    if any(not isinstance(value, str) for value in body.values()):
        raise SettingsError('Connection fields must be text.')
    es = endpoint(body.get('elasticsearch_endpoint', '').strip(), 'Elasticsearch endpoint')
    # Accept the Kibana base URL copied from the current Elastic Cloud project.
    parsed = urlsplit(es)
    if parsed.hostname.endswith('.elastic.cloud') and '.kb.' in parsed.hostname:
        es = es.replace('.kb.', '.es.', 1)
    same_es = es == current.get('ES_ENDPOINT', '').rstrip('/')
    inferred = cloud_endpoints(es)
    kb = body.get('kibana_endpoint', '').strip() or (current.get('KIBANA_ENDPOINT', '') if same_es else '') or inferred.get('kibana_endpoint', '')
    otlp = body.get('otlp_endpoint', '').strip() or (current.get('OTEL_EXPORTER_OTLP_ENDPOINT', '') if same_es else '') or inferred.get('otlp_endpoint', '')
    if not kb or not otlp:
        raise SettingsError('Add the Kibana and OTLP base URLs under Advanced endpoints.')
    kb = endpoint(kb, 'Kibana endpoint')
    otlp = endpoint(otlp, 'OTLP endpoint')
    key = body.get('api_key', '').strip()
    if key.startswith('ApiKey '): key = key[7:].strip()
    if key and (len(key) > 4096 or not re.fullmatch(r'[A-Za-z0-9+/=_-]+', key)):
        raise SettingsError('Enter the encoded Elastic API key, without quotes or extra text.')
    if not key and (not same_es or otlp != current.get('OTEL_EXPORTER_OTLP_ENDPOINT', '').rstrip('/')):
        raise SettingsError('Enter an API key when changing the Elasticsearch or OTLP destination.')
    if not key and not current.get('ES_API_KEY'):
        raise SettingsError('Enter an Elastic API key.')
    values = {'ES_ENDPOINT': es, 'KIBANA_ENDPOINT': kb, 'OTEL_EXPORTER_OTLP_ENDPOINT': otlp,
              'ELASTIC_BULK_ENDPOINT': otlp + '/_es', 'ES_API_KEY': key or current['ES_API_KEY'],
              'ES_READ_API_KEY': key or current.get('ES_READ_API_KEY') or current['ES_API_KEY']}
    for signal in ('TRACES', 'LOGS', 'METRICS'):
        values['OTEL_EXPORTER_OTLP_' + signal + '_ENDPOINT'] = otlp + '/v1/' + signal.lower()
    return values


def public_settings(env=None):
    env = os.environ if env is None else env
    return {'elasticsearch_endpoint': env.get('ES_ENDPOINT', ''),
            'kibana_endpoint': env.get('KIBANA_ENDPOINT', ''),
            'otlp_endpoint': env.get('OTEL_EXPORTER_OTLP_ENDPOINT', ''),
            'api_key_configured': bool(env.get('ES_API_KEY')), 'csrf_token': CSRF_TOKEN}


def _request(url, key, label, data=None, protobuf=False):
    payload = data if protobuf else (json.dumps(data).encode() if data is not None else None)
    request = Request(url, data=payload, headers={'Authorization': 'ApiKey ' + key,
        'Content-Type': 'application/x-protobuf' if protobuf else 'application/json'},
        method='POST' if data is not None else 'GET')
    try:
        with build_opener(NoRedirect()).open(request, timeout=8) as response:
            raw = response.read()
            return {} if protobuf or not raw else json.loads(raw)
    except HTTPError as exc:
        if exc.code in (401, 403): reason = 'The API key is invalid or lacks permission.'
        elif exc.code == 404: reason = 'The endpoint or required demo resource was not found.'
        elif 300 <= exc.code < 400: reason = 'Use the final HTTPS endpoint; redirects are not followed.'
        else: reason = 'The server returned HTTP ' + str(exc.code) + '.'
        raise SettingsError(label + ': ' + reason) from None
    except (URLError, OSError, ValueError):
        raise SettingsError(label + ': Could not connect or read a valid response. Check the URL and network.') from None


def check_ingestion(values):
    """Read-only configuration checks. No text is sent to a redaction API."""
    es = values.get('ES_ENDPOINT', '').rstrip('/')
    key = values.get('ES_READ_API_KEY') or values.get('ES_API_KEY', '')
    if not es or not key: raise SettingsError('Configure the Elasticsearch endpoint and API key.')
    data = _request(es + '/_ingest/pipeline/' + PIPELINE, key, 'Ingest redaction pipeline')
    if not data.get(PIPELINE, {}).get('processors'):
        raise SettingsError('Install the demo ingest redaction pipeline before saving.')
    for signal, stream in [('traces', TRACE_STREAM), ('logs', LOG_STREAM)]:
        data = _request(es + '/_index_template/' + signal + '-agentic-demo', key, 'Demo ' + signal + ' template')
        templates = data.get('index_templates', [])
        template = templates[0].get('index_template', {}) if templates else {}
        config = template.get('template', {}).get('settings', {})
        final = config.get('index.final_pipeline') or config.get('index', {}).get('final_pipeline')
        failure_store = template.get('template', {}).get('data_stream_options', {}).get('failure_store', {}).get('enabled')
        if final != PIPELINE or failure_store is not False or template.get('index_patterns') != [signal + '-agentic_demo.otel-*']:
            raise SettingsError('Install the protected demo ' + signal + ' template before saving.')
        # Also inspect existing backing indices; changing a template alone is not retroactive.
        data = _request(es + '/' + stream + '/_settings?allow_no_indices=true&ignore_unavailable=true', key, 'Demo stream settings')
        if any(item.get('settings', {}).get('index', {}).get('final_pipeline') != PIPELINE for item in data.values()):
            raise SettingsError('Every existing demo backing index needs the final redaction pipeline.')
        options = _request(es + '/_data_stream/' + stream + '?expand_wildcards=all', key, 'Demo failure-store settings')
        if any(item.get('failure_store', {}).get('enabled') is not False for item in options.get('data_streams', [])):
            raise SettingsError('Disable failure-store capture on the demo streams so failed redaction cannot retain originals.')
    return ['Ingest redaction pipeline installed', 'Final pipeline on demo trace/log streams']


def test_connection(values, current=None):
    current = os.environ if current is None else current
    es = values['ES_ENDPOINT']; key = values['ES_API_KEY']
    _request(es + '/_security/_authenticate', key, 'Elasticsearch authentication')
    checks = ['Elasticsearch authentication']
    checks.extend(check_ingestion(values))
    index = current.get('ES_KNOWLEDGE_INDEX')
    if index:
        _request(es + '/' + quote(index, safe='') + '/_search', values['ES_READ_API_KEY'],
                 'Knowledge index ' + index, {'size': 0, 'query': {'match_all': {}}})
        checks.append('Knowledge index access')
    # Empty export requests verify credentials and routes without sending demo data.
    for signal in ('traces', 'logs', 'metrics'):
        _request(values['OTEL_EXPORTER_OTLP_ENDPOINT'] + '/v1/' + signal, key,
                 'OTLP ' + signal, b'', protobuf=True)
    checks.append('OTLP traces, logs and metrics')
    return checks


def save_settings(values, path=None):
    """Atomically replace only Elastic settings, preserving unrelated credentials."""
    path = ENV_FILE if path is None else Path(path)
    lines = path.read_text().splitlines() if path.exists() else []
    output = []; remaining = dict(values)
    for line in lines:
        name = line.strip().removeprefix('export ').split('=', 1)[0].strip()
        if name in values:
            if name in remaining:
                output.append(name + '=' + remaining.pop(name))
        else: output.append(line)
    output.extend(name + '=' + value for name, value in remaining.items())
    fd, temp = tempfile.mkstemp(prefix='.elastic-settings-', suffix='.env', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write('\n'.join(output) + '\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp): os.unlink(temp)
