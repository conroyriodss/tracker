"""Offline synthetic tests. No live GCP, registry or repository calls."""
import copy
import importlib
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock,patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import evidence_store as es
import evidence_collector as ec
import capability_assessment as ca
import tracker_gcp_backend as gcp
import gcp_config

def settings():
    cfg=json.loads((ROOT/'templates/gcp_settings.json').read_text())
    cfg.update(user_agent='SyntheticTest/1 (https://example.org/contact)',default_source_cache_permission='public_cache_permitted',max_resources=1)
    return cfg

def cards():return {'R1':{'repo_uid':'R1','identity':{'name':'Synthetic Repository','homepage':'https://example.org/'},'blocks':{}}}

def add(store,text='Public REST API version 1.',url='https://example.org/',**kw):
    return store.record(repo_uid='R1',url=url,body=text.encode(),text=text,source_kind='official_documentation',
                        cache_permission='public_cache_permitted',parser_version=ec.PARSER_VERSION,
                        content_type='text/html',**kw)

class StoreTests(unittest.TestCase):
    def test_same_bytes_deduplicate_with_distinct_observations(self):
        with tempfile.TemporaryDirectory() as d:
            store=es.EvidenceStore(d)
            a=add(store,fetched_at='2026-11-01T00:00:00+00:00');b=add(store,fetched_at='2026-11-02T00:00:00+00:00')
            self.assertEqual(a['snapshot_id'],b['snapshot_id']);self.assertNotEqual(a['observation_id'],b['observation_id'])
            self.assertEqual(len(list((store.root/'blobs').glob('*/*'))),1)
            self.assertEqual(len(store.search('REST API')),1)

    def test_source_and_parser_versions_are_distinct(self):
        with tempfile.TemporaryDirectory() as d:
            s=es.EvidenceStore(d);a=add(s);b=add(s,text='Changed source')
            self.assertEqual(a['source_id'],b['source_id']);self.assertNotEqual(a['snapshot_id'],b['snapshot_id'])
            c=s.record(repo_uid='R1',url=a['url'],body=b'Public REST API version 1.',text='Different parse',
                       source_kind='official_documentation',cache_permission='public_cache_permitted',parser_version='v2')
            self.assertEqual(c['body_sha256'],a['body_sha256']);self.assertNotEqual(c['snapshot_id'],a['snapshot_id'])

    def test_grounded_or_unpermitted_content_rejected_before_writes(self):
        with tempfile.TemporaryDirectory() as d:
            store=es.EvidenceStore(d)
            for kind,permission in [('google_search_grounding','public_cache_permitted'),('official_documentation','unknown')]:
                with self.assertRaises(ValueError):store.record(repo_uid='R1',url='https://example.org/',body=b'x',text='x',source_kind=kind,cache_permission=permission)
            self.assertFalse((Path(d)/'blobs').exists())

    def test_cache_control_and_credential_urls_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):add(es.EvidenceStore(d),headers={'Cache-Control':'private, no-store'})
            with self.assertRaises(ValueError):add(es.EvidenceStore(d),url='https://example.org/?access_token=never-store')

    def test_tampering_detected_on_cache_read_and_sealed_run(self):
        with tempfile.TemporaryDirectory() as d:
            s=es.EvidenceStore(Path(d)/'cache');r=add(s)
            s.blob_path(r['text_sha256']).write_text('tampered')
            with self.assertRaises(ValueError):s.text(r)
            run=Path(d)/'run';run.mkdir();(run/'data.json').write_text('{}');es.seal_run(run)
            self.assertTrue(es.verify_run(run))
            (run/'data.json').write_text('[]')
            with self.assertRaises(ValueError):es.verify_run(run)

    def test_zero_ttl_forces_refresh(self):
        with tempfile.TemporaryDirectory() as d:
            s=es.EvidenceStore(d);r=add(s)
            self.assertIsNone(s.latest('R1',r['url'],0));self.assertEqual(s.latest('R1',r['url'],30),r)

    def test_gcs_create_only_and_collision_verification(self):
        class Conflict(Exception):pass
        api=types.ModuleType('google.api_core.exceptions');api.PreconditionFailed=Conflict
        bucket=MagicMock();bucket.name='private-bucket';blob=bucket.blob.return_value;blob.generation=7
        with patch.dict(sys.modules,{'google.api_core.exceptions':api}):
            receipt=es.upload_immutable(bucket,'key',b'data')
            self.assertEqual(receipt['generation'],'7')
            self.assertEqual(blob.upload_from_string.call_args.kwargs['if_generation_match'],0)
            blob.upload_from_string.side_effect=Conflict();blob.download_as_bytes.return_value=b'wrong'
            with self.assertRaises(ValueError):es.upload_immutable(bucket,'key',b'data')

    def test_bigquery_parameters_and_cost_cap(self):
        bq=types.ModuleType('google.cloud.bigquery')
        bq.ScalarQueryParameter=lambda n,t,v:(n,t,v)
        bq.QueryJobConfig=lambda **kw:kw
        cloud=types.ModuleType('google.cloud');cloud.bigquery=bq
        client=MagicMock();client.query.return_value.result.return_value=[]
        with patch.dict(sys.modules,{'google.cloud':cloud,'google.cloud.bigquery':bq}), \
             patch.object(es,'cloud_clients',return_value=(None,client)),patch.dict(os.environ,{'GCP_ID':'test-project-123'}):
            es.search_bigquery(settings(),"'; DROP TABLE x; --",limit=3,max_bytes=12345)
        sql=client.query.call_args.args[0];cfg=client.query.call_args.kwargs['job_config']
        self.assertNotIn('DROP',sql);self.assertEqual(cfg['maximum_bytes_billed'],12345)
        self.assertIn(('limit','INT64',3),cfg['query_parameters'])

class CollectorTests(unittest.TestCase):
    def test_loading_stub_and_json_parsing(self):
        self.assertEqual(ec.parse_body(b'<script>ignore()</script>Loading, please wait...','text/html')[2],'MISSING_BROWSER_RENDERING')
        text,_,status=ec.parse_body(b'{"unit":"<kg>"}','application/json')
        self.assertEqual(status,'PARSED');self.assertEqual(json.loads(text)['unit'],'<kg>')

    def test_robots_disallow_prevents_document_request(self):
        collector=ec.PublicCollector(settings())
        with patch.object(collector,'request',return_value=(b'User-agent: *\nDisallow: /\n','https://example.org/robots.txt',{},200)) as request:
            with tempfile.TemporaryDirectory() as d:
                with self.assertRaisesRegex(ValueError,'Robots'):
                    collector.collect(es.EvidenceStore(d),'R1',{'url':'https://example.org/','source_kind':'official_documentation','cache_permission':'public_cache_permitted'})
            self.assertEqual(request.call_count,1)

    def test_unknown_permission_prevents_any_request(self):
        collector=ec.PublicCollector(settings())
        with patch.object(collector,'request') as request,tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):collector.collect(es.EvidenceStore(d),'R1',{'url':'https://example.org/','source_kind':'official_documentation'})
            request.assert_not_called()

    def test_nonpublic_dns_rejected(self):
        collector=ec.PublicCollector(settings())
        with patch.object(ec.socket,'getaddrinfo',return_value=[(None,None,None,None,('127.0.0.1',80))]):
            with self.assertRaisesRegex(ValueError,'Nonpublic'):collector.request('https://example.org/',{'example.org'})

class AssessmentTests(unittest.TestCase):
    def test_22_criteria_cover_all_domains_without_score(self):
        self.assertEqual(len(ca.CRITERIA),22);self.assertEqual({v[0] for v in ca.CRITERIA.values()},{'A','B','C','D'})
        plan=ca.make_plan(cards(),settings());self.assertEqual(plan['selection'][0]['repo_uid'],'R1')
        self.assertNotIn('score',plan)

    def test_missing_sources_not_claimed_absent(self):
        with tempfile.TemporaryDirectory() as d:
            original=copy.deepcopy(cards());result=ca.assess(d,original,settings())
            self.assertEqual(original,cards());self.assertEqual(result['model_calls'],0)
            rows=json.loads((Path(result['run_dir'])/'findings.json').read_text())
            self.assertTrue(all(r['status']=='NOT_ASSESSED' for r in rows));self.assertTrue(es.verify_run(result['run_dir']))

    def test_quote_matching_rejects_hallucinated_source_and_quote(self):
        with tempfile.TemporaryDirectory() as d:
            s=es.EvidenceStore(d);r=add(s)
            base={'criterion_id':'D4','value':'API','scope':'documented','caveat':'','quote':'Public REST API version 1.','snapshot_id':r['snapshot_id']}
            accepted,rejected=ca.validate_candidates({'claims':[base,{**base,'snapshot_id':'invented'},{**base,'quote':'unsupported'}]},[r],s)
            self.assertEqual(len(accepted),1);self.assertEqual(len(rejected),2)
            self.assertTrue(accepted[0]['human_review_required'])

    def test_model_cache_reuses_validated_response_and_preserves_source_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            store=es.EvidenceStore(Path(d)/'cache/cloud_evidence');r=add(store)
            candidate={'claims':[{'criterion_id':'D4','value':'REST API','scope':'public documentation','caveat':'not a protocol test',
                                  'quote':'Public REST API version 1.','snapshot_id':r['snapshot_id']}]}
            fake=MagicMock(return_value=(candidate,{'modelVersion':'mock-only'},{}))
            first=ca.assess(d,cards(),settings(),use_gemini=True,generate=fake)
            second=ca.assess(d,cards(),settings(),use_gemini=True,generate=fake)
            self.assertEqual(fake.call_count,1);self.assertEqual(second['model_calls'],0)
            self.assertEqual(first['candidate_claims'],1)
            self.assertEqual((Path(first['run_dir'])/'blobs'/r['body_sha256']).read_bytes(),b'Public REST API version 1.')

    def test_cached_homepage_retains_documentation_link_scope(self):
        with tempfile.TemporaryDirectory() as d:
            s=es.EvidenceStore(Path(d)/'cache/cloud_evidence')
            add(s,text='<html>Public API <a href="/api/docs">Documentation</a></html>')
            add(s,url='https://example.org/api/docs',text='API details')
            result=ca.assess(d,cards(),settings())
            self.assertEqual(len(json.loads((Path(result['run_dir'])/'sources.json').read_text())),2)

    def test_truncation_preserved_and_not_repaired(self):
        with tempfile.TemporaryDirectory() as d:
            add(es.EvidenceStore(Path(d)/'cache/cloud_evidence'))
            raw={'candidates':[{'finishReason':'MAX_TOKENS'}],'usageMetadata':{'thoughtsTokenCount':3359}}
            fake=MagicMock(side_effect=gcp.GeminiResponseError('truncated',raw,{}))
            result=ca.assess(d,cards(),settings(),use_gemini=True,generate=fake)
            self.assertEqual(result['candidate_claims'],0)
            events=json.loads((Path(result['run_dir'])/'events.json').read_text())
            self.assertTrue(any(e.get('finish_reason')=='MAX_TOKENS' for e in events))

    def test_selected_schema_instance_validation_and_unpinned_reference_gap(self):
        with tempfile.TemporaryDirectory() as d:
            s=es.EvidenceStore(d)
            schema=add(s,url='https://example.org/schema',text='{"type":"integer"}')
            sample=add(s,url='https://example.org/sample',text='"not integer"')
            resource={'pid_examples':[],'schema_tests':[{'schema_url':schema['url'],'instance_url':sample['url']}]}
            self.assertEqual(ca.technical_tests(resource,[schema,sample],s)[0]['result'],'TEST_FAILED')
            remote=add(s,url=schema['url'],text='{"$ref":"https://example.org/unknown"}')
            self.assertEqual(ca.technical_tests(resource,[remote,sample],s)[0]['result'],'NOT_ASSESSED')

    def test_human_decisions_separate_and_gaps_cannot_be_accepted(self):
        with tempfile.TemporaryDirectory() as d:
            result=ca.assess(d,cards(),settings());run=Path(result['run_dir'])
            original=(run/'manifest.json').read_bytes();row=json.loads((run/'findings.json').read_text())[0]
            with self.assertRaises(ValueError):ca.save_human_decision(run,row['finding_id'],'ACCEPT_DOCUMENTED_CLAIM','Reviewer','reason',d)
            path=ca.save_human_decision(run,row['finding_id'],'DEFER','Reviewer','Need evidence',d)
            self.assertTrue(path.is_file());self.assertEqual((run/'manifest.json').read_bytes(),original)

    def test_private_settings_and_invalid_caps_are_rejected(self):
        for change in ({'api_key':'never-store'},{'max_llm_calls':-1},{'max_resources':0}):
            with self.assertRaises(ValueError):gcp_config.validate({**settings(),**change})

    def test_original_snapshots_reused_with_original_hash_and_date(self):
        import base64
        from tracker_cache_bridge import import_existing
        with tempfile.TemporaryDirectory() as d:
            project=Path(d)/'project';shared=Path(d)/'shared';shared.mkdir()
            body=b'<repository><repositoryName>Synthetic</repositoryName></repository>'
            row={'api_endpoint':'https://www.re3data.org/api/v40/repository/r3d123',
                 'raw_xml_base64':base64.b64encode(body).decode(),'response_sha256':es.digest(body),
                 'fetched_at':'2026-01-01T00:00:00+00:00','http_status':200}
            es.write_json(shared/'re3data_interoperability_cache.json',{'records':{'r3d123':row}})
            card=cards();card['R1']['links']=[{'id_type':'re3data','id_value':'r3d123'}]
            report,cfg=import_existing(project,shared,card,settings())
            self.assertEqual(len(report['imported']),1)
            obs=es.EvidenceStore(project/'cache/cloud_evidence').observations()[0]
            self.assertEqual(obs['body_sha256'],es.digest(body));self.assertEqual(obs['fetched_at'],row['fetched_at'])
            self.assertEqual(cfg['resources']['R1']['sources'][0]['url'],row['api_endpoint'])
            row['response_sha256']='0'*64;es.write_json(shared/'re3data_interoperability_cache.json',{'records':{'r3d123':row}})
            report,_=import_existing(project,shared,card,settings())
            self.assertEqual(len(report['imported']),0);self.assertTrue(report['skipped'])

    def test_nlm_and_registry_test_does_not_claim_general_registration(self):
        with tempfile.TemporaryDirectory() as d:
            s=es.EvidenceStore(d);record=s.record(repo_uid='R1',url='https://registry.example.org/id/1',body=b'{"id":"exact"}',text='{"id":"exact"}',
                source_kind='registry_record',cache_permission='public_cache_permitted')
            resource={'pid_examples':[],'schema_tests':[],'registry_checks':[{'url':record['url'],'criterion_id':'B7','json_pointer':'/id','expected_value':'exact'}]}
            row=ca.technical_tests(resource,[record],s)[0]
            self.assertEqual(row['result'],'EXACT_RECORD_VALUE_MATCH');self.assertIn('applicability',row['limitation'])

    def test_pid_resolution_requires_recorded_http_status_and_date(self):
        with tempfile.TemporaryDirectory() as d:
            s=es.EvidenceStore(d);record=add(s)
            resource={'pid_examples':[record['url']],'schema_tests':[]}
            self.assertEqual(ca.technical_tests(resource,[record],s)[0]['result'],'NOT_ASSESSED')
            record=add(s,http_status=200)
            row=ca.technical_tests(resource,[record],s)[0]
            self.assertEqual(row['result'],'OBSERVED_HTTP_SUCCESS');self.assertEqual(row['observed_at'],record['fetched_at'])

class GeminiTests(unittest.TestCase):
    def test_adc_only_requires_gcp_id(self):
        with patch.dict(os.environ,{'GEMINI_API_KEY':'fake'},clear=True):
            with self.assertRaisesRegex(ValueError,'GCP_ID'):gcp.project_id(settings())

    def test_request_uses_thinking_and_schema_and_rejects_truncation(self):
        client=MagicMock();raw={'candidates':[{'finishReason':'MAX_TOKENS'}]}
        client.models.generate_content.return_value.model_dump.return_value=raw
        with patch.object(gcp,'create_client',return_value=client):
            with self.assertRaises(gcp.GeminiResponseError):gcp.extract(gcp.DEFAULT_MODEL,'synthetic',16384,ca.SCHEMA,settings())
        self.assertEqual(client.models.generate_content.call_count,1)
        sent=client.models.generate_content.call_args.kwargs['config']
        self.assertEqual(sent['thinking_config'],{'thinking_level':'LOW'});self.assertNotIn('tools',sent)

class ReviewIntegrationTests(unittest.TestCase):
    modules=('common','review_widgets','automated_review','review_workflow','gcp_review')
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.original_env=os.environ.copy()
        for name in self.modules:sys.modules.pop(name,None)
        for name in list(os.environ):
            if name.startswith('NIH_TRACKER'):os.environ.pop(name,None)
        os.environ.update(NIH_TRACKER_PRODUCTION_ONLY='1',NIH_TRACKER_ENV='prod',
                          NIH_TRACKER_ROOT_PROD=self.temp.name+'/prod',NIH_TRACKER_SHARED_CACHE_ROOT=self.temp.name+'/shared')
        self.common=importlib.import_module('common');self.review=importlib.import_module('automated_review')
        self.runner=importlib.import_module('gcp_review')
        import yaml
        self.card=Path(self.common.CARDS)/'R1.yaml';self.card.write_text(yaml.safe_dump(cards()['R1']))
        queue=Path(self.common.RUNS)/'verify_ids/test/verify_report.csv';queue.parent.mkdir(parents=True)
        import pandas as pd
        pd.DataFrame([{'repo_uid':'R1','card_name':'Synthetic Repository','id_type':'rrid',
                       'candidate_id':'RRID:SCR_123456','candidate_url':'https://scicrunch.org/resolver/SCR_123456',
                       'candidate_name':'Synthetic Repository','candidate_confidence':'0.5',
                       'candidate_source':'synthetic','candidate_evidence_kind':'name','candidate_eligible':'true'}]).to_csv(queue,index=False)
        report=self.review.export_review_bundle(queue,provider='gemini',web_research=False,max_items=1,batch_size=1)
        self.batch=next((Path(report['bundle_root'])/'batches').glob('batch_*'))
        self.response=json.loads((self.batch/'response_template.json').read_text())
        self.response.update(provider='gemini',model='mock',generated_at=es.now())
        self.response['recommendations'][0]['rationale']='Synthetic example; insufficient evidence.'
        self.fake=MagicMock(return_value=(self.response,{'modelVersion':'mock-verified-transport'},{}))
    def tearDown(self):
        for name in self.modules:sys.modules.pop(name,None)
        os.environ.clear();os.environ.update(self.original_env);self.temp.cleanup()
    def test_gemini_imports_into_existing_human_approval_workflow(self):
        before=self.card.read_bytes();result=self.runner.run_batch(self.batch,settings(),generate=self.fake)
        self.assertEqual(result['import']['status'],'PASS');self.assertEqual(self.card.read_bytes(),before)
        self.assertEqual(set(self.review.load_recommendations()['provider']),{'google'})
        self.assertTrue(self.review.load_approvals().empty)
        workflow=importlib.import_module('review_workflow')
        self.assertEqual(workflow.compute_review_status(include_evidence=False)['state'],'APPROVAL_REQUIRED')
        self.assertTrue(es.verify_run(result['run_dir']))
    def test_repeated_batch_uses_cache_without_second_model_call(self):
        self.runner.run_batch(self.batch,settings(),generate=self.fake)
        second=self.runner.run_batch(self.batch,settings(),generate=self.fake)
        self.assertTrue(second['cache_hit']);self.assertEqual(self.fake.call_count,1)
    def test_changed_card_blocks_execution(self):
        self.card.write_text(self.card.read_text()+'\nchanged: true\n')
        with self.assertRaisesRegex(ValueError,'Stale'):self.runner.run_batch(self.batch,settings(),generate=self.fake)
        self.fake.assert_not_called()
    def test_incomplete_batch_does_not_import_or_modify_cards(self):
        self.response['recommendations']=[];before=self.card.read_bytes()
        with self.assertRaisesRegex(ValueError,'coverage'):self.runner.run_batch(self.batch,settings(),generate=self.fake)
        self.assertTrue(self.review.load_recommendations().empty);self.assertEqual(self.card.read_bytes(),before)
    def test_supplied_evidence_runner_rejects_invented_web_sources(self):
        self.response['recommendations'][0]['external_sources']=[{'url':'https://invented.example/','title':'Unconsulted','supports':'anything'}]
        with self.assertRaisesRegex(ValueError,'external sources'):self.runner.run_batch(self.batch,settings(),generate=self.fake)
        self.assertTrue(self.review.load_recommendations().empty)

if __name__=='__main__':unittest.main()
