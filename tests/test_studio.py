"""Privacy boundaries and agent execution, with no external API calls."""
import io
import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'web_agent'))
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

exporter = InMemorySpanExporter()
provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(exporter))
fake_tel = SimpleNamespace(GEMINI_KEY='unit-test', MODEL_A='gemini-2.5-flash',
    MODEL_B='gemini-2.5-flash-lite', AB_RATIO=.3, SVC_NAME='test-agent',
    KB_ENDPOINT='https://example.test', ES_ENDPOINT='https://example.test',
    tracer=provider.get_tracer('test'), trace=trace, logger=Mock())
for instrument in ['guardrail_counter','tool_calls_counter','tool_duration','error_counter',
                   'token_counter','op_duration','agent_duration','feedback_counter']:
    setattr(fake_tel, instrument, Mock())
sys.modules['telemetry'] = fake_tel
import studio
from google.genai import types

def response(*parts):
    return types.GenerateContentResponse(candidates=[types.Candidate(
        content=types.Content(role='model',parts=list(parts)),finish_reason='STOP')],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=20,candidates_token_count=5))

class StudioTests(unittest.TestCase):
    def setUp(self):
        exporter.clear(); fake_tel.logger.reset_mock()
        fake_tel.feedback_counter.reset_mock()
        studio._sessions.clear();studio._runs.clear()
        self.session=studio.session_for(None,'B')
        self.env=patch.dict(os.environ,{'ES_ENDPOINT':'https://example.test','ES_READ_API_KEY':'test-only'})
        self.env.start(); self.addCleanup(self.env.stop)

    def test_original_content_reaches_model_ui_and_otel_without_redaction_request(self):
        captures=[]; events=[]
        sample='Card 4111 1111 1111 1111; demo@example.com'
        def model(**kwargs):
            captures.append(str(kwargs['contents']))
            return response(types.Part(text=sample))
        with patch.object(studio.client.models,'generate_content',side_effect=model), \
             patch.object(studio,'urlopen',side_effect=AssertionError('Unexpected app HTTP request')), \
             patch.object(studio.connection_settings,'_request',side_effect=AssertionError('Unexpected Elastic request')):
            result=studio.run_agent(sample,self.session,'pii',emit=events.append)
        self.assertFalse(result['is_error'])
        self.assertEqual(result['text'],sample)
        self.assertEqual(result['privacy_mode'],'elasticsearch_ingest')
        self.assertEqual(result['elastic_redaction']['status'],'pending')
        self.assertIn(sample,captures[0])
        self.assertEqual(self.session['history'][0]['text'],sample)
        self.assertEqual(events[0]['message'],sample)
        self.assertFalse(any(step['kind']=='privacy' for step in result['steps']))
        spans=exporter.get_finished_spans()
        self.assertEqual(len(spans),2)
        for span in spans:
            self.assertIn('4111 1111 1111 1111',span.attributes['gen_ai.input.messages'])
            self.assertIn('demo@example.com',span.attributes['gen_ai.output.messages'])

    def test_conversation_attributes_include_history_and_actual_tool_exchange(self):
        self.session['history']=[{'role':'user','text':'Remember 125.'},
                                 {'role':'model','text':'Remembered.'}]
        responses=iter([response(types.Part.from_function_call(name='calculate',args={'expression':'125*18'})),
                        response(types.Part(text='2250'))])
        with patch.object(studio.client.models,'generate_content',side_effect=lambda **kw:next(responses)):
            result=studio.run_agent('Multiply it by 18.',self.session)
        self.assertFalse(result['is_error'])
        models=[s for s in exporter.get_finished_spans() if s.name.startswith('chat ')]
        first=json.loads(models[0].attributes['gen_ai.input.messages'])
        self.assertEqual([m['role'] for m in first],['user','assistant','user'])
        self.assertEqual(first[0]['parts'][0]['content'],'Remember 125.')
        second=json.loads(models[1].attributes['gen_ai.input.messages'])
        self.assertEqual(second[-2]['parts'][0]['type'],'tool_call')
        self.assertEqual(second[-1]['role'],'tool')
        self.assertEqual(second[-1]['parts'][0]['result'],{'result':'2250'})
        root=exporter.get_finished_spans()[-1]
        self.assertEqual(json.loads(root.attributes['gen_ai.output.messages'])[0]['parts'][0]['content'],'2250')

    def test_original_tool_payloads_are_emitted_and_thoughts_are_excluded(self):
        contents=[types.Content(role='model',parts=[
            types.Part(text='Private reasoning',thought=True),
            types.Part(text='Card 4111 1111 '),types.Part(text='1111 1111'),
            types.Part.from_function_call(name='search_knowledge_base',args={'query':'demo@example.com'})]),
            types.Content(role='user',parts=[types.Part.from_function_response(
                name='search_knowledge_base',response={'value':'demo@example.com'})])]
        encoded=studio.conversation_json(contents)
        self.assertNotIn('Private reasoning',encoded)
        self.assertIn('4111 1111 1111 1111',encoded)
        self.assertIn('demo@example.com',encoded)

    def test_provider_error_is_emitted_for_ingest_redaction(self):
        with patch.object(studio.client.models,'generate_content',
                          side_effect=RuntimeError('Bad card 4111 1111 1111 1111')):
            result=studio.run_agent('hello',self.session)
        self.assertTrue(result['is_error'])
        spans=exporter.get_finished_spans()
        self.assertEqual(len(spans),2)
        for span in spans:
            self.assertEqual(span.status.status_code,trace.StatusCode.ERROR)
            self.assertIn('4111 1111 1111 1111',span.to_json())

    def test_retry_is_visible_and_recovery_is_successful(self):
        responses=iter([response(types.Part.from_function_call(name='calculate',args={'expression':'125*18'})),
                        response(types.Part(text='2250'))])
        with patch.object(studio.client.models,'generate_content',side_effect=lambda **kw:next(responses)),patch.object(studio.time,'sleep'):
            result=studio.run_agent('calculate 125*18',self.session,'retry_tool')
        self.assertFalse(result['is_error'])
        tools=[s for s in result['steps'] if s['kind']=='tool']
        self.assertEqual([s['status'] for s in tools],['error','complete'])
        self.assertEqual(len(exporter.get_finished_spans()),5)

    def test_calculator_rejects_code_and_unbounded_work(self):
        self.assertEqual(studio.calculate('(125*18)+240'),'2490')
        for expression in ['__import__("os").getcwd()','2**999999','[1][0]','1/0']:
            with self.assertRaises(Exception):studio.calculate(expression)

    def test_feedback_only_counts_once(self):
        with patch.object(studio.client.models,'generate_content',return_value=response(types.Part(text='Hello'))):
            result=studio.run_agent('hello',self.session)
        def post():
            body=json.dumps({'run_id':result['run_id'],'session_id':result['session_id'],'rating':1}).encode()
            handler=object.__new__(studio.Handler);handler.path='/api/feedback'
            handler.headers={'Content-Length':str(len(body))};handler.rfile=io.BytesIO(body);handler.json=Mock()
            handler.do_POST();return handler.json.call_args.args
        self.assertEqual(post()[0],200)
        self.assertTrue(post()[1]['duplicate'])
        fake_tel.feedback_counter.add.assert_called_once()

    def test_estimate_includes_model_specific_output_rate(self):
        self.assertEqual(studio.estimate_cost('gemini-2.5-flash',1000000,1000000),2.8)
        self.assertEqual(studio.estimate_cost('gemini-2.5-flash-lite',1000000,1000000),.5)

    def test_stored_proof_reads_indexed_root_and_never_processes_text(self):
        run={'trace_id':'trace','span_id':'span','started':1}
        attrs={'privacy.processor':'elasticsearch-redact','privacy.mode':'elasticsearch_ingest',
               'privacy.action':'obfuscated','privacy.redacted_fields':2,'privacy.pipeline':'agentic-demo-otel-redact',
               'gen_ai.input.messages':json.dumps([{'role':'user','parts':[{'type':'text','content':'Card [CREDIT_CARD]'}]}]),
               'gen_ai.output.messages':json.dumps([{'role':'assistant','parts':[{'type':'text','content':'[EMAIL]'}]}])}
        with patch.object(studio.connection_settings,'_request',return_value={'hits':{'hits':[{'_source':{'attributes':attrs}}]}}) as request:
            proof=studio.stored_conversation(run)
        self.assertEqual(proof['status'],'stored');self.assertEqual(proof['input'],'Card [CREDIT_CARD]')
        self.assertEqual(proof['output'],'[EMAIL]')
        self.assertTrue(request.call_args.args[0].endswith('/traces-agentic_demo.otel-default/_search'))
        self.assertEqual(request.call_args.args[3]['query']['bool']['filter'],[{'term':{'trace_id':'trace'}},{'term':{'span_id':'span'}}])

    def test_proof_distinguishes_indexing_delay_and_unverified_content(self):
        for data,status in [({'hits':{'hits':[]}},'pending'),
                            ({'hits':{'hits':[{'_source':{'attributes':{'gen_ai.input.messages':'raw'}}}]}},'unverified')]:
            with patch.object(studio.connection_settings,'_request',return_value=data):
                proof=studio.stored_conversation({'trace_id':'t','span_id':'s','started':1})
            self.assertEqual(proof,{'status':status})

    def test_stored_copy_requires_matching_run_session(self):
        studio._runs['run']={'session_id':'owner'}
        handler=object.__new__(studio.Handler);handler.path='/api/runs/run/stored?session_id=wrong';handler.json=Mock()
        with patch.object(studio,'stored_conversation') as stored:handler.do_GET()
        stored.assert_not_called();self.assertEqual(handler.json.call_args.args[0],404)

class SettingsHandlerTests(unittest.TestCase):
    def handler(self, headers=None, address='127.0.0.1'):
        handler=object.__new__(studio.Handler);handler.path='/api/settings'
        handler.headers={'Host':'localhost:5601','Origin':'http://localhost:5601','Content-Type':'application/json',
                         'X-CSRF-Token':studio.connection_settings.CSRF_TOKEN}
        if headers:handler.headers.update(headers)
        handler.client_address=(address,1234);handler.json=Mock()
        return handler

    def test_settings_reject_remote_clients_cross_origin_and_missing_token(self):
        with patch.object(fake_tel,'PORT',5601,create=True):
            for headers,address in [({'X-CSRF-Token':''},'127.0.0.1'),({'Origin':'https://other.example'},'127.0.0.1'),
                    ({'Host':'other.example:5601'},'127.0.0.1'),({},'192.0.2.1'),({'Sec-Fetch-Site':'cross-site'},'127.0.0.1')]:
                handler=self.handler(headers,address)
                self.assertFalse(handler.settings_allowed(write=True))
                self.assertEqual(handler.json.call_args.args[0],403)
            self.assertTrue(self.handler().settings_allowed(write=True))

    def test_saving_is_blocked_while_a_run_is_active(self):
        capacity=__import__('threading').BoundedSemaphore(4);capacity.acquire()
        with patch.object(fake_tel,'PORT',5601,create=True),patch.object(studio,'_capacity',capacity),patch.object(studio.connection_settings,'candidate',return_value={'ES_API_KEY':'new'}),patch.object(studio.connection_settings,'test_connection',return_value=[]),patch.object(studio.connection_settings,'save_settings') as save:
            handler=self.handler();handler.connection_post({})
        self.assertEqual(handler.json.call_args.args[0],409);save.assert_not_called()
        for _ in range(3):self.assertTrue(capacity.acquire(blocking=False))
        self.assertFalse(capacity.acquire(blocking=False))

    def test_failed_check_does_not_save_or_restart(self):
        with patch.object(fake_tel,'PORT',5601,create=True),patch.object(studio.connection_settings,'candidate',return_value={}),patch.object(studio.connection_settings,'test_connection',side_effect=studio.connection_settings.SettingsError('Authentication failed')),patch.object(studio.connection_settings,'save_settings') as save,patch.object(studio,'_request_restart') as restart:
            handler=self.handler();handler.connection_post({})
        self.assertEqual(handler.json.call_args.args[0],400);save.assert_not_called();restart.assert_not_called()

if __name__=='__main__':unittest.main()
