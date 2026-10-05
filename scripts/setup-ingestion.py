"""Install only Agent Studio's dedicated ingest pipeline/templates on ES_ENDPOINT."""
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'web_agent'))
from connection import load_environment
from privacy import PIPELINE, TRACE_STREAM, LOG_STREAM
from settings import NoRedirect


def request(method, path, data=None):
    body = json.dumps(data).encode() if data is not None else None
    req = Request(os.environ['ES_ENDPOINT'].rstrip('/') + path, data=body, method=method,
                  headers={'Authorization': 'ApiKey ' + os.environ['ES_API_KEY'], 'Content-Type': 'application/json'})
    with build_opener(NoRedirect()).open(req, timeout=20) as response:
        return json.load(response)


def main():
    load_environment()
    request('PUT', '/_ingest/pipeline/' + PIPELINE, json.loads((ROOT / 'config/pii-redact-otel.json').read_text()))
    for signal, stream in [('traces', TRACE_STREAM), ('logs', LOG_STREAM)]:
        # Preserve the deployment's current native mappings and component templates.
        template = request('GET', '/_index_template/' + signal + '-otel@template')['index_templates'][0]['index_template']
        for key in ('created_date_millis', 'modified_date_millis', 'version'): template.pop(key, None)
        template.update(index_patterns=[signal + '-agentic_demo.otel-*'], priority=250,
                        _meta={'description': 'Agent Studio only: mandatory Elasticsearch ingest redaction'})
        template.setdefault('template', {}).setdefault('settings', {})['index.final_pipeline'] = PIPELINE
        template['template']['data_stream_options'] = {'failure_store': {'enabled': False}}
        request('PUT', '/_index_template/' + signal + '-agentic-demo', template)
        try: request('GET', '/_data_stream/' + stream)
        except HTTPError as exc:
            if exc.code != 404: raise
            request('PUT', '/_data_stream/' + stream)
        request('PUT', '/' + stream + '/_settings', {'index.final_pipeline': PIPELINE})
        request('PUT', '/_data_stream/' + stream + '/_options', {'failure_store': {'enabled': False}})
        print('Protected:', stream)
    print('Installed:', PIPELINE)


if __name__ == '__main__':
    try: main()
    except HTTPError as exc: sys.exit('Elastic setup failed with HTTP ' + str(exc.code) + '; check setup permissions.')
