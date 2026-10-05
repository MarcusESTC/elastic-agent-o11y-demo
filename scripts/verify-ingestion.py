"""Exercise real OTLP ingestion using synthetic PII, then inspect indexed _source."""
import json
import os
import sys
import time
import uuid
from pathlib import Path
from urllib.request import Request, build_opener
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'web_agent'))
from connection import load_environment
from privacy import PIPELINE, DATASET, TRACE_STREAM, LOG_STREAM
from settings import NoRedirect
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest

SAMPLE = 'Card 4111 1111 1111 1111; demo@example.com; 123-45-6789; (212) 555-0198; CVV: 123'
ORIGINALS = ['4111 1111 1111 1111', 'demo@example.com', '123-45-6789', '(212) 555-0198', 'CVV: 123']
LABELS = ['[CREDIT_CARD]', '[EMAIL]', '[SSN]', '[PHONE]', '[CARD_SECURITY_CODE]']


def send(url, data, protobuf=False):
    req = Request(url, data=data if protobuf else json.dumps(data).encode(),
                  headers={'Authorization': 'ApiKey ' + os.environ['ES_API_KEY'],
                           'Content-Type': 'application/x-protobuf' if protobuf else 'application/json'})
    with build_opener(NoRedirect()).open(req, timeout=20) as response:
        raw = response.read()
        return raw if protobuf else json.loads(raw)


def attributes(target, values):
    for key, value in values.items():
        item = target.add(); item.key = key
        if isinstance(value, int): item.value.int_value = value
        else: item.value.string_value = value


def resource(target):
    attributes(target.attributes, {'service.name': 'gemini-demo-agent', 'deployment.environment': 'demo',
                                  'data_stream.dataset': DATASET, 'data_stream.namespace': 'default'})


def main():
    load_environment()
    trace = uuid.uuid4().bytes; span_id = bytes.fromhex('1234567890123456'); now = time.time_ns()
    request = ExportTraceServiceRequest(); group = request.resource_spans.add(); resource(group.resource)
    scope = group.scope_spans.add(); scope.scope.name = 'ingest-redaction-verification'
    span = scope.spans.add(trace_id=trace, span_id=span_id, name='ingest redaction coverage', kind=1,
                           start_time_unix_nano=now, end_time_unix_nano=now + 1000000)
    span.status.code = 2; span.status.message = SAMPLE
    conversation = json.dumps([{'role': 'user', 'parts': [{'type': 'text', 'content': SAMPLE}]}])
    attributes(span.attributes, {field: conversation for field in ['gen_ai.input.messages', 'gen_ai.output.messages', 'gen_ai.system_instructions']})
    attributes(span.attributes, {'tool.arguments': json.dumps({'nested': [SAMPLE]}), 'tool.result': SAMPLE,
                                'gen_ai.usage.input_tokens': 125, 'demo.run_id': '12345678901234567890'})
    event = span.events.add(name='exception', time_unix_nano=now)
    attributes(event.attributes, {'exception.type': 'SyntheticDemoError', 'exception.message': SAMPLE,
                                 'exception.stacktrace': 'Synthetic stack: ' + SAMPLE})
    endpoint = os.environ['OTEL_EXPORTER_OTLP_ENDPOINT'].rstrip('/')
    send(endpoint + '/v1/traces', request.SerializeToString(), True)
    request = ExportLogsServiceRequest(); group = request.resource_logs.add(); resource(group.resource)
    scope = group.scope_logs.add(); scope.scope.name = 'ingest-redaction-verification'
    for text in [SAMPLE, 'No sensitive values: 125 * 18 = 2250']:
        record = scope.log_records.add(time_unix_nano=now, observed_time_unix_nano=now, trace_id=trace,
                                       span_id=span_id, severity_number=9, severity_text='INFO')
        record.body.string_value = text
        attributes(record.attributes, {'message.preview': text, 'answer.preview': text})
    send(endpoint + '/v1/logs', request.SerializeToString(), True)
    url = os.environ['ES_ENDPOINT'].rstrip('/') + '/' + TRACE_STREAM + ',' + LOG_STREAM + '/_search'
    query = {'size': 30, 'query': {'term': {'trace_id': trace.hex()}}}
    for attempt in range(30):
        result = send(url, query); hits = result.get('hits', {}).get('hits', [])
        if len(hits) >= 4: break
        time.sleep(2)
    assert len(hits) >= 4, 'Expected span, span event and two logs to be indexed'
    out = ROOT / '.runtime/ingest-redaction'; out.mkdir(parents=True, exist_ok=True)
    (out / 'coverage-indexed.json').write_text(json.dumps(result, indent=2))
    serialized = json.dumps(hits)
    assert all(value not in serialized for value in ORIGINALS), 'Unmasked synthetic value found in indexed data'
    assert all(value in serialized for value in LABELS), 'Missing redaction placeholders'
    assert '1234567890123456' in serialized and '12345678901234567890' in serialized, 'Correlation IDs changed'
    assert all(hit['_source']['attributes']['privacy.pipeline'] == PIPELINE for hit in hits), 'Pipeline marker missing'
    unchanged = [hit['_source'] for hit in hits if hit['_source'].get('body', {}).get('text', '').startswith('No sensitive')]
    assert unchanged and unchanged[0]['attributes']['privacy.action'] == 'no_match', 'No-match content changed'
    print(json.dumps({'ok': True, 'trace_id': trace.hex(), 'documents': len(hits),
                      'verified': ['span attributes', 'tool payloads', 'status message', 'exception event',
                                   'log body', 'log attributes', 'unchanged non-PII', 'unchanged IDs']}, indent=2))


if __name__ == '__main__':
    try: main()
    except HTTPError as exc: sys.exit('Elastic verification HTTP ' + str(exc.code) + '; check connection and permissions.')
