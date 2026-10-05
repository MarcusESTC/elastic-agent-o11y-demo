"""Connection settings validation, credential boundaries and atomic persistence."""
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'web_agent'))
import settings

CURRENT={'ES_ENDPOINT':'https://example.es.us-east1.gcp.elastic.cloud',
         'KIBANA_ENDPOINT':'https://example.kb.us-east1.gcp.elastic.cloud',
         'OTEL_EXPORTER_OTLP_ENDPOINT':'https://example.ingest.us-east1.gcp.elastic.cloud',
         'ES_API_KEY':'private-key','ES_READ_API_KEY':'read-key','ES_REDACT_API_KEY':'redact-key',
         'ES_PII_INDEX':'demo-chat','ES_KNOWLEDGE_INDEX':'demo-knowledge'}

class SettingsTests(unittest.TestCase):
    def test_key_is_never_returned_to_ui(self):
        data=settings.public_settings(CURRENT)
        for secret in ('private-key','read-key','redact-key'):self.assertNotIn(secret,json.dumps(data))
        self.assertTrue(data['api_key_configured'])

    def test_blank_key_preserves_current_scoped_credentials(self):
        data=settings.candidate({'elasticsearch_endpoint':CURRENT['ES_ENDPOINT'],'api_key':''},CURRENT)
        for name in ('ES_API_KEY','ES_READ_API_KEY'):self.assertEqual(data[name],CURRENT[name])

    def test_cloud_url_derivation_and_new_key_for_all_routes(self):
        data=settings.candidate({'elasticsearch_endpoint':'https://new.kb.us-east1.gcp.elastic.cloud/','api_key':'ApiKey new-key'},CURRENT)
        self.assertEqual(data['ES_ENDPOINT'],'https://new.es.us-east1.gcp.elastic.cloud')
        self.assertEqual(data['OTEL_EXPORTER_OTLP_ENDPOINT'],'https://new.ingest.us-east1.gcp.elastic.cloud')
        for name in ('ES_API_KEY','ES_READ_API_KEY'):self.assertEqual(data[name],'new-key')
        for signal in ('TRACES','LOGS','METRICS'):
            self.assertEqual(data['OTEL_EXPORTER_OTLP_'+signal+'_ENDPOINT'],data['OTEL_EXPORTER_OTLP_ENDPOINT']+'/v1/'+signal.lower())

    def test_saved_key_cannot_be_forwarded_to_a_new_destination(self):
        for body in ({'elasticsearch_endpoint':'https://new.es.us-east1.gcp.elastic.cloud'},
                     {'elasticsearch_endpoint':CURRENT['ES_ENDPOINT'],'otlp_endpoint':'https://other.example'}):
            with self.assertRaisesRegex(settings.SettingsError,'Enter an API key'):settings.candidate(body,CURRENT)

    def test_urls_and_keys_reject_injection_and_insecure_transport(self):
        for url in ('http://example.com','https://user:secret@example.com','https://example.com/path',
                    'https://example.com?q=key','https://example.com#fragment','https://example.com\nES_API_KEY=bad'):
            with self.assertRaises(settings.SettingsError):settings.candidate({'elasticsearch_endpoint':url,'api_key':'key'},CURRENT)
        with self.assertRaises(settings.SettingsError):
            settings.candidate({'elasticsearch_endpoint':CURRENT['ES_ENDPOINT'],'api_key':'key\nOTHER=value'},CURRENT)

    def test_atomic_save_preserves_gemini_and_private_permissions(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'.env';path.write_text('# Existing config\nGEMINI_API_KEY=gemini-secret\nES_API_KEY=old\nexport ES_API_KEY=duplicate\nPORT=5601\n')
            settings.save_settings({'ES_API_KEY':'new','ES_ENDPOINT':'https://example.com'},path)
            text=path.read_text();self.assertIn('GEMINI_API_KEY=gemini-secret',text);self.assertIn('PORT=5601',text)
            self.assertEqual(text.count('ES_API_KEY='),1);self.assertNotIn('duplicate',text)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600)
            self.assertEqual(len(list(Path(folder).iterdir())),1)

    def test_server_error_body_and_credentials_are_not_exposed(self):
        error=HTTPError('https://example.com',403,'failed',{},io.BytesIO(b'private-key original text'))
        with patch.object(settings,'build_opener') as opener:
            opener.return_value.open.side_effect=error
            with self.assertRaises(settings.SettingsError) as caught:settings._request('https://example.com','private-key','Authentication')
        self.assertNotIn('private-key',str(caught.exception));self.assertNotIn('original text',str(caught.exception))

    def configuration(self,url,key,label,data=None,protobuf=False):
        from privacy import PIPELINE
        if '/_ingest/pipeline/' in url: return {PIPELINE:{'processors':[{'redact':{}}]}}
        if '/_index_template/' in url:
            signal='traces' if 'traces-agentic-demo' in url else 'logs'
            return {'index_templates':[{'index_template':{'index_patterns':[signal+'-agentic_demo.otel-*'],
                    'template':{'settings':{'index':{'final_pipeline':PIPELINE}},'data_stream_options':{'failure_store':{'enabled':False}}}}}]}
        if '/_data_stream/' in url:return {'data_streams':[{'failure_store':{'enabled':False}}]}
        if '/_settings?' in url: return {'backing':{'settings':{'index':{'final_pipeline':PIPELINE}}}}
        return {}

    def test_connection_checks_final_pipelines_without_redaction_or_index_writes(self):
        values=settings.candidate({'elasticsearch_endpoint':CURRENT['ES_ENDPOINT']},CURRENT)
        with patch.object(settings,'_request',side_effect=self.configuration) as requests:
            result=settings.test_connection(values,CURRENT)
        self.assertEqual(len(result),5)
        self.assertEqual(len([call for call in requests.call_args_list if call.kwargs.get('protobuf')]),3)
        self.assertTrue(all('/_simulate' not in call.args[0] and '/_create/' not in call.args[0] for call in requests.call_args_list))

    def test_missing_pipeline_prevents_remaining_checks(self):
        values=settings.candidate({'elasticsearch_endpoint':CURRENT['ES_ENDPOINT']},CURRENT)
        with patch.object(settings,'_request',return_value={}) as requests:
            with self.assertRaisesRegex(settings.SettingsError,'Install the demo ingest'):settings.test_connection(values,CURRENT)
        self.assertEqual(requests.call_count,2)

    def test_existing_index_without_final_pipeline_is_rejected(self):
        def reply(url,*args,**kwargs):
            if '/_settings?' in url:return {'backing':{'settings':{'index':{}}}}
            return self.configuration(url,*args,**kwargs)
        with patch.object(settings,'_request',side_effect=reply):
            with self.assertRaisesRegex(settings.SettingsError,'Every existing demo backing index'):
                settings.check_ingestion(CURRENT)

    def test_template_must_protect_dedicated_demo_streams(self):
        def reply(url,*args,**kwargs):
            result=self.configuration(url,*args,**kwargs)
            if '/_index_template/' in url:result['index_templates'][0]['index_template']['index_patterns']=['logs-*']
            return result
        with patch.object(settings,'_request',side_effect=reply):
            with self.assertRaisesRegex(settings.SettingsError,'protected demo'):
                settings.check_ingestion(CURRENT)

    def test_failure_store_cannot_retain_originals_on_redaction_failure(self):
        def reply(url,*args,**kwargs):
            if '/_data_stream/' in url:return {'data_streams':[{'failure_store':{'enabled':True}}]}
            return self.configuration(url,*args,**kwargs)
        with patch.object(settings,'_request',side_effect=reply):
            with self.assertRaisesRegex(settings.SettingsError,'failure-store capture'):
                settings.check_ingestion(CURRENT)

if __name__=='__main__':unittest.main()
