"""Synthetic evidence/transport tests; no live cloud calls or production inputs."""
import copy
import importlib
import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import evidence_store as es


def settings():
    return json.loads((ROOT/'templates/gcp_settings.json').read_text())


class QueueTests(unittest.TestCase):
    modules = ('common','review_widgets','automated_review','review_workflow','gcp_review','gcp_review_queue','step_promote_ai_review','step_promote_review')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_env = os.environ.copy()
        for name in self.modules:
            sys.modules.pop(name,None)
        for name in list(os.environ):
            if name.startswith('NIH_TRACKER'):
                os.environ.pop(name,None)
        os.environ.update(NIH_TRACKER_PRODUCTION_ONLY='1',NIH_TRACKER_ENV='prod',
                          NIH_TRACKER_ROOT_PROD=self.temp.name+'/prod',NIH_TRACKER_SHARED_CACHE_ROOT=self.temp.name+'/shared')
        self.common = importlib.import_module('common')
        self.review = importlib.import_module('automated_review')
        self.widgets = importlib.import_module('review_widgets')
        self.queue = importlib.import_module('gcp_review_queue')
        self.workflow = importlib.import_module('review_workflow')
        self.cfg = settings()
        self.card = Path(self.common.CARDS)/'R1.yaml'
        self.card.write_text(yaml.safe_dump({'schema_version':'1.0','repo_uid':'R1','kind':'repository',
            'identity':{'name':'Example Resource','homepage':'https://example.org/','aliases':[]},'links':[],'blocks':{},
            'curation':{'review_state':'needs_review'}}))
        self.source = self.card_queue(12)
        self.calls = []

    def tearDown(self):
        for name in self.modules:
            sys.modules.pop(name,None)
        os.environ.clear(); os.environ.update(self.old_env)
        self.temp.cleanup()

    def card_queue(self,count):
        path = Path(self.common.RUNS)/'crosswalk/20260826T195653/crosswalk_review.csv'
        path.parent.mkdir(parents=True,exist_ok=True)
        pd.DataFrame([{'registry_name':f'Example source label {i}','matched_card':'R1','how':'fuzzy','score':'0.95'}
                      for i in range(count)]).to_csv(path,index=False)
        return path

    def identifier_queue(self):
        path = Path(self.common.RUNS)/'verify_ids/current/verify_report.csv'
        path.parent.mkdir(parents=True,exist_ok=True)
        pd.DataFrame([{'repo_uid':'R1','card_name':'Example Resource','id_type':'rrid',
                       'candidate_id':'RRID:SCR_123456','candidate_eligible':'true'}]).to_csv(path,index=False)
        return path

    def model(self,decision='DEFER',provider='gemini'):
        def generate(model,prompt,tokens,schema,config):
            self.calls.append((provider,prompt))
            manifest_text,items_text = prompt.split('\nSEALED_MANIFEST\n',1)[1].split('\nUNTRUSTED_ITEMS\n',1)
            manifest,items = json.loads(manifest_text),json.loads(items_text)
            result = {'schema':self.review.RESPONSE_SCHEMA,'bundle_id':manifest['bundle_id'],'batch_id':manifest['batch_id'],
                      'batch_manifest_sha256':es.digest(es.canonical_json(manifest)),'provider':provider,'model':model,'generated_at':es.now(),
                      'recommendations':[{'review_key':item['review_key'],'item_sha256':item['item_sha256'],
                          'decision':decision,'target':item['allowed_targets'][0] if decision=='ACCEPT' else '',
                          'alias':'','confidence':'HIGH' if decision=='ACCEPT' else 'LOW',
                          'evidence_grade':'EXACT_NAME_AND_HOST' if decision=='ACCEPT' else 'INSUFFICIENT',
                          'rationale':'SYNTHETIC RECOMMENDATION ONLY; identity evidence requires human examination.',
                          'evidence_refs':['raw_queue_row'],'external_sources':[],'warnings':[],'human_review_required':True}
                          for item in items]}
            raw = {'modelVersion':model} if provider=='gemini' else {'model':model,'stop_reason':'end_turn'}
            return result,raw,{}
        return generate

    def plan(self,count=12,dual=False):
        cfg={**self.cfg,'review_second_provider':'claude' if dual else 'none'}
        return self.queue.create_plan([self.source],cfg,max_items=count)['plan_dir']

    def approve(self,decision=None):
        row = self.review.load_recommendations().iloc[0].to_dict()
        self.review.save_approvals([{'recommendation_key':row['recommendation_key'],'review_key':row['review_key'],
            'queue_kind':row['queue_kind'],'approved':True,'approved_decision':decision or row['recommended_decision'],
            'approved_target':row['recommended_target'],'reviewer':'Synthetic human reviewer',
            'bundle_id':row['bundle_id'],'item_sha256':row['item_sha256']}])
        return row

    def run_script(self,name,*args):
        env=os.environ.copy();env['NIH_TRACKER_PROD_WRITE_CONFIRMATION']='ALLOW_PROD_CARD_WRITES'
        result=subprocess.run([sys.executable,'-B',str(ROOT/'scripts'/name),*args],env=env,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+'\n'+result.stderr)
        return result

    def test_status_and_plan_cover_132_cards_plus_one_identifier(self):
        self.source=self.card_queue(132);identifier=self.identifier_queue()
        state=self.workflow.compute_review_status(include_evidence=False)
        self.assertEqual(state['queue_pending_ai_exportable'],133)
        self.assertEqual(state['queue_pending_manual_only'],0)
        result=self.queue.preview([self.source,identifier],self.cfg,max_items=150)
        self.assertEqual(result['selected_items'],133);self.assertEqual(result['batches'],28)
        cfg={**self.cfg,'review_second_provider':'claude'}
        self.assertEqual(self.queue.preview([self.source,identifier],cfg,max_items=150)['maximum_model_calls_for_first_pass'],56)
        self.assertTrue(self.review.load_recommendations().empty)

    def test_card_export_binds_existing_uid_and_exact_label(self):
        report=self.review.export_review_bundle(self.source,provider='gemini',max_items=1,web_research=False)
        batch=next((Path(report['bundle_root'])/'batches').glob('*'))
        item=next(iter(self.review._load_batch_items(batch).values()))
        self.assertEqual(item['allowed_targets'],['R1'])
        self.assertEqual(item['queue_context']['target'],'Example source label 0')
        self.assertEqual(item['queue_context']['candidate_label'],'Example source label 0')
        self.assertEqual(item['allowed_decisions'],['ACCEPT','DEFER','REJECT'])

    def test_unmatched_card_cannot_receive_invented_target(self):
        pd.DataFrame([{'registry_name':'Unknown','matched_card':'','how':'unresolved'}]).to_csv(self.source,index=False)
        plan=self.plan(1);_,data=self.queue.load_plan(plan)
        item=next(iter(self.review._load_batch_items(Path(plan)/data['batches'][0]['directory']).values()))
        self.assertEqual(item['allowed_targets'],[])
        rec={'review_key':item['review_key'],'item_sha256':item['item_sha256'],'decision':'ACCEPT','target':'R2','alias':'made up',
             'confidence':'HIGH','evidence_grade':'NAME_SIMILARITY_ONLY','rationale':'test','evidence_refs':['missing.field'],
             'external_sources':[],'warnings':[],'human_review_required':True}
        errors=self.review._validate_recommendation(rec,item,'card')
        self.assertTrue(any('allowed_targets' in e for e in errors))
        self.assertTrue(any('alias' in e for e in errors));self.assertTrue(any('Name-only' in e for e in errors))
        self.assertTrue(any('evidence reference' in e for e in errors))

    def test_source_alias_delimiter_is_never_silently_split(self):
        pd.DataFrame([{'registry_name':'Archive; component','matched_card':'R1','how':'fuzzy'}]).to_csv(self.source,index=False)
        plan=self.plan(1)
        result=self.queue.run_plan(plan,generate=self.model('ACCEPT'))
        self.assertEqual(result['batch_states'],{'FAILED':1})
        self.assertTrue(self.review.load_recommendations().empty)

    def test_registry_rows_are_selected_exactly_and_hash_bound(self):
        registry=Path(self.common.INP)/'registry.csv'
        pd.DataFrame([{'Repository_Name':'Example source label 0','Repository_Url':'https://example.org/'},
                      {'Repository_Name':'Other','Repository_Url':'https://other.example/'}]).to_csv(registry,index=False)
        plan=self.queue.create_plan([self.source],self.cfg,max_items=1,registry_file=registry.name)['plan_dir']
        _,data=self.queue.load_plan(plan)
        item=next(iter(self.review._load_batch_items(Path(plan)/data['batches'][0]['directory']).values()))
        self.assertEqual(len(item['registry_evidence'][0]['rows']),1)
        registry.write_text(registry.read_text()+'\n')
        report=self.queue.run_plan(plan,generate=self.model())
        self.assertEqual(report['batch_states'],{'BLOCKED':1});self.assertEqual(self.calls,[])

    def test_resumption_uses_bounds_and_never_repeats_success(self):
        plan=self.plan();before=self.card.read_bytes()
        first=self.queue.run_plan(plan,max_batches=1,generate=self.model())
        self.assertEqual(first['batch_states'],{'IMPORTED':1,'PENDING':2})
        second=self.queue.run_plan(plan,max_batches=2,generate=self.model())
        self.assertTrue(second['recommendation_generation_complete'])
        self.queue.run_plan(plan,max_batches=2,generate=self.model())
        self.assertEqual(len(self.calls),3);self.assertEqual(len(self.review.load_recommendations()),12)
        self.assertEqual(self.card.read_bytes(),before);self.assertTrue(self.review.load_approvals().empty)
        self.assertEqual(self.workflow.compute_review_status(include_evidence=False)['state'],'APPROVAL_REQUIRED')
        self.assertEqual(self.queue.preview([self.source],self.cfg,max_items=20)['selected_items'],0)

    def test_failed_batch_stops_and_needs_explicit_retry(self):
        plan=self.plan();model=self.model();attempts=[]
        def sometimes(*args):
            attempts.append(1)
            if len(attempts)==2:raise RuntimeError('synthetic quota error')
            return model(*args)
        first=self.queue.run_plan(plan,max_batches=3,generate=sometimes)
        self.assertEqual(first['batch_states'],{'IMPORTED':1,'FAILED':1,'PENDING':1})
        second=self.queue.run_plan(plan,max_batches=3,generate=sometimes)
        self.assertEqual(second['batch_states'],{'IMPORTED':2,'FAILED':1})
        last=self.queue.run_plan(plan,max_batches=3,retry_failed=True,generate=sometimes)
        self.assertEqual(last['batch_states'],{'IMPORTED':3});self.assertEqual(len(attempts),4)

    def test_interrupted_receipt_recovers_existing_import_without_call(self):
        plan=self.plan(1);self.queue.run_plan(plan,generate=self.model())
        _,data=self.queue.load_plan(plan)
        self.queue._record(data,data['batches'][0],'RUNNING')
        self.queue.run_plan(plan,generate=self.model())
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.queue.status(plan)['batch_states'],{'RECOVERED':1})

    def test_interrupted_import_recovers_sealed_response_without_call(self):
        plan=self.plan(1);self.queue.run_plan(plan,generate=self.model())
        _,data=self.queue.load_plan(plan)
        self.queue._record(data,data['batches'][0],'RUNNING')
        Path(self.review.AI_RECOMMENDATIONS_CSV).unlink()
        self.queue.run_plan(plan,generate=self.model())
        self.assertEqual(len(self.calls),1);self.assertEqual(len(self.review.load_recommendations()),1)

    def test_changed_card_blocks_stale_plan_without_call(self):
        plan=self.plan(1);self.card.write_text(self.card.read_text()+'\nchanged: true\n')
        result=self.queue.run_plan(plan,generate=self.model())
        self.assertEqual(result['batch_states'],{'BLOCKED':1});self.assertEqual(self.calls,[])

    def test_plan_and_receipt_tampering_detected(self):
        plan=self.plan(1);path=Path(plan)/'plan.json';original=path.read_bytes()
        path.write_text('{}')
        with self.assertRaisesRegex(ValueError,'SHA-256'):self.queue.run_plan(plan,generate=self.model())
        path.write_bytes(original);self.queue.run_plan(plan,generate=self.model())
        _,data=self.queue.load_plan(plan)
        receipt=next((self.queue._receipt_root(data)/'receipts').glob('*.json'));receipt.write_text('{}')
        with self.assertRaisesRegex(ValueError,'receipt hash'):self.queue.status(plan)

    def test_manual_decision_after_plan_is_not_overwritten(self):
        plan=self.plan(1);kind,frame=self.widgets.normalize_review_file(self.source)
        row=frame.iloc[0].to_dict();row['review_decision']='REJECT'
        self.widgets.upsert_adjudications(kind,self.widgets.prepare_decisions(kind,[row],reviewer='Human'))
        result=self.queue.run_plan(plan,generate=self.model('ACCEPT'))
        self.assertEqual(result['batch_states'],{'HUMAN_HANDLED':1});self.assertEqual(self.calls,[])

    def test_partial_human_decision_requires_replan_remaining_rows(self):
        plan=self.plan(5);kind,frame=self.widgets.normalize_review_file(self.source)
        row=frame.iloc[0].to_dict();row['review_decision']='REJECT'
        self.widgets.upsert_adjudications(kind,self.widgets.prepare_decisions(kind,[row],reviewer='Human'))
        self.assertEqual(self.queue.run_plan(plan,generate=self.model())['batch_states'],{'BLOCKED':1})
        self.assertEqual(self.queue.preview([self.source],self.cfg,max_items=20)['selected_items'],11)

    def test_approved_accept_flows_to_existing_alias_transaction(self):
        self.source=self.card_queue(1);plan=self.plan(1);before=self.card.read_bytes()
        self.queue.run_plan(plan,generate=self.model('ACCEPT'));self.approve()
        self.run_script('step_promote_ai_review.py','--bulk')
        self.assertFalse((Path(self.common.INP)/'adjudications.csv').exists())
        self.run_script('step_promote_ai_review.py','--bulk','--apply','--confirm','PROMOTE_AI_RECOMMENDATIONS')
        rows=self.widgets.load_adjudications('card');self.assertEqual(rows.iloc[0]['verdict'],'ACCEPT_ALIAS')
        self.assertEqual(rows.iloc[0]['add_aliases'],'Example source label 0')
        self.assertEqual(self.card.read_bytes(),before)
        self.run_script('step_promote_review.py','--adjudications','adjudications.csv','--apply')
        card=yaml.safe_load(self.card.read_text())
        self.assertEqual(card['repo_uid'],'R1');self.assertEqual(card['identity']['name'],'Example Resource')
        self.assertIn('Example source label 0',str(card['identity']['aliases']))
        self.assertTrue(self.widgets.load_adjudications('card').iloc[0]['applied_at'])

    def test_defer_does_not_mark_card_candidate_resolved(self):
        self.source=self.card_queue(1);plan=self.plan(1);before=self.card.read_bytes()
        self.queue.run_plan(plan,generate=self.model());self.approve()
        self.run_script('step_promote_ai_review.py','--bulk','--apply','--confirm','PROMOTE_AI_RECOMMENDATIONS')
        state=self.workflow.compute_review_status(include_evidence=False)
        self.assertEqual(state['queue_pending_rows'],1);self.assertFalse(state['ready_for_pipeline'])
        self.assertEqual(self.card.read_bytes(),before)

    def test_guided_finalize_applies_approved_card_and_reaches_complete(self):
        self.source=self.card_queue(1);plan=self.plan(1)
        self.queue.run_plan(plan,generate=self.model('ACCEPT'));self.approve()
        self.run_script('step_review_workflow.py','--action','validate')
        self.run_script('step_review_workflow.py','--action','apply','--confirm','FINALIZE_REVIEW_WORKFLOW')
        self.assertEqual(self.workflow.compute_review_status(include_evidence=False)['state'],'COMPLETE')

    def test_new_manual_decision_blocks_old_model_approval_at_promotion(self):
        self.source=self.card_queue(1);plan=self.plan(1)
        self.queue.run_plan(plan,generate=self.model('ACCEPT'));self.approve()
        kind,frame=self.widgets.normalize_review_file(self.source)
        row=frame.iloc[0].to_dict();row['review_decision']='REJECT'
        self.widgets.upsert_adjudications(kind,self.widgets.prepare_decisions(kind,[row],reviewer='Later human'))
        self.run_script('step_promote_ai_review.py','--bulk','--apply','--confirm','PROMOTE_AI_RECOMMENDATIONS')
        self.assertEqual(self.widgets.load_adjudications('card').iloc[0]['review_decision'],'REJECT')
        self.assertTrue(self.review.load_approvals().iloc[0]['promotion_status'].startswith('quarantined'))

    def test_rejected_card_candidate_preserves_card_bytes(self):
        self.source=self.card_queue(1);plan=self.plan(1);before=self.card.read_bytes()
        self.queue.run_plan(plan,generate=self.model('REJECT'));self.approve()
        self.run_script('step_review_workflow.py','--action','apply','--confirm','FINALIZE_REVIEW_WORKFLOW')
        self.assertEqual(self.card.read_bytes(),before)
        self.assertEqual(self.workflow.compute_review_status(include_evidence=False)['state'],'COMPLETE')

    def test_stale_card_approval_is_quarantined_not_rebased(self):
        self.source=self.card_queue(1);plan=self.plan(1)
        self.queue.run_plan(plan,generate=self.model('ACCEPT'));self.approve()
        self.card.write_text(self.card.read_text()+'\nchanged: true\n');before=self.card.read_bytes()
        self.run_script('step_promote_ai_review.py','--bulk','--apply','--confirm','PROMOTE_AI_RECOMMENDATIONS')
        self.assertTrue(self.review.load_approvals().iloc[0]['promotion_status'].startswith('quarantined'))
        self.assertEqual(self.card.read_bytes(),before)

    def test_dual_review_is_blind_and_provider_disagreement_is_preserved(self):
        plan=self.plan(1,dual=True)
        result=self.queue.run_plan(plan,max_batches=2,generate={'gemini':self.model('ACCEPT'),'claude':self.model('DEFER','claude')})
        self.assertEqual(result['batch_states'],{'IMPORTED':2});self.assertEqual(len(self.calls),2)
        gemini_prompt,claude_prompt=self.calls[0][1],self.calls[1][1]
        self.assertEqual(gemini_prompt.split('\nUNTRUSTED_ITEMS\n')[1],claude_prompt.split('\nUNTRUSTED_ITEMS\n')[1])
        self.assertNotIn('SYNTHETIC RECOMMENDATION ONLY',claude_prompt)
        consensus=self.review.recommendation_consensus().iloc[0]
        self.assertEqual(consensus['provider_count'],2);self.assertEqual(consensus['consensus_status'],'DISAGREE')
        self.assertEqual(consensus['bulk_eligible'],'false')
        self.assertTrue(self.review.load_approvals().empty)

    def test_second_provider_does_not_skip_after_gemini_import(self):
        plan=self.plan(1,dual=True)
        self.queue.run_plan(plan,max_batches=1,generate=self.model())
        result=self.queue.run_plan(plan,max_batches=1,generate=self.model(provider='claude'))
        self.assertEqual(result['batch_states'],{'IMPORTED':2})
        self.assertEqual(set(self.review.load_recommendations()['provider']),{'google','anthropic'})
        self.assertEqual(self.queue.preview([self.source],{**self.cfg,'review_second_provider':'claude'},max_items=1)['selected_items'],1)
        # The first item is already covered; preview advances to the next pending row.
        self.assertEqual(self.queue.preview([self.source],{**self.cfg,'review_second_provider':'claude'},max_items=20)['selected_items'],11)

    def test_cached_independent_sources_are_frozen_with_original_identity(self):
        store=es.EvidenceStore(Path(self.common.PROJECT)/'cache/cloud_evidence')
        record=store.record(repo_uid='R1',url='https://example.org/about',body=b'Example source label 0 denotes this archive.',
            text='Example source label 0 denotes this archive.',source_kind='official_documentation',cache_permission='public_cache_permitted')
        plan=self.plan(1);_,data=self.queue.load_plan(plan)
        item=next(iter(self.review._load_batch_items(Path(plan)/data['batches'][0]['directory']).values()))
        evidence=item['source_evidence'][0]
        self.assertEqual(evidence['snapshot_id'],record['snapshot_id'])
        self.assertEqual(evidence['fetched_at'],record['fetched_at'])
        self.assertFalse(evidence['identity_verified_by_cache_lookup'])
        self.assertTrue(self.review._evidence_ref_exists(item,'source_evidence[0].text'))

    def test_single_provider_agreement_never_counts_as_two_providers(self):
        plan=self.plan(1);self.queue.run_plan(plan,generate=self.model('ACCEPT'))
        frame=self.review.load_recommendations()
        row=frame.iloc[0].to_dict();row.update(recommendation_key='another',model='another-gemini',imported_at=es.now())
        consensus=self.review.recommendation_consensus(pd.concat([frame,pd.DataFrame([row])],ignore_index=True)).iloc[0]
        self.assertEqual(consensus['provider_count'],1);self.assertEqual(consensus['bulk_eligible'],'false')

    def test_exact_model_cache_keeps_returned_provider_identity(self):
        plan=self.plan(1,dual=True);_,data=self.queue.load_plan(plan)
        batch=Path(plan)/data['batches'][1]['directory']
        first=self.queue.gcp_review.run_batch(batch,self.cfg,provider='claude',generate=self.model('DEFER','claude'))
        second=self.queue.gcp_review.run_batch(batch,self.cfg,provider='claude',generate=self.model('DEFER','claude'))
        self.assertTrue(second['cache_hit']);self.assertEqual(len(self.calls),1)
        self.assertEqual(set(self.review.load_recommendations()['provider']),{'anthropic'})

    def test_changed_registry_and_sources_do_not_reuse_prior_recommendation(self):
        plan=self.plan(1);self.queue.run_plan(plan,generate=self.model())
        registry=Path(self.common.INP)/'registry.csv'
        pd.DataFrame([{'Repository_Name':'Example source label 0','Repository_Url':'https://example.org/'}]).to_csv(registry,index=False)
        selected=self.queue.preview([self.source],self.cfg,max_items=1,registry_file=registry.name)
        self.assertEqual(selected['selection'][0]['items'][0]['review_key'],self.review.load_recommendations().iloc[0]['review_key'])

    def test_invalid_caps_and_concurrent_execution_fail_before_call(self):
        for cap in (0,-1,True,1.2):
            with self.assertRaises(ValueError):self.queue.preview([self.source],self.cfg,max_items=cap)
        plan=self.plan(1)
        with self.review.FileLock(Path(self.common.PROJECT)/'cache/gcp_review_queue.lock',purpose='synthetic competing job'):
            with self.assertRaises(Exception):self.queue.run_plan(plan,generate=self.model())
        self.assertEqual(self.calls,[])


class ClaudeTransportTests(unittest.TestCase):
    def fake_modules(self,raw,status=200):
        google=types.ModuleType('google');google.__path__=[]
        auth=types.ModuleType('google.auth');auth.__path__=[];google.auth=auth
        auth.default=MagicMock(return_value=('adc',None))
        transport=types.ModuleType('google.auth.transport');transport.__path__=[]
        requests=types.ModuleType('google.auth.transport.requests')
        session=MagicMock();session.post.return_value.status_code=status;session.post.return_value.json.return_value=raw
        requests.AuthorizedSession=MagicMock(return_value=session)
        return {'google':google,'google.auth':auth,'google.auth.transport':transport,'google.auth.transport.requests':requests},session

    def test_adc_structured_output_and_no_web_or_secret_transport(self):
        import tracker_claude_backend as backend
        modules,session=self.fake_modules({'stop_reason':'end_turn','model':backend.DEFAULT_MODEL,'content':[{'type':'text','text':'{"ready":true}'}]})
        with patch.dict(sys.modules,modules),patch.dict(os.environ,{'GCP_ID':'synthetic-project'}):
            self.assertEqual(backend.smoke_test(settings())['status'],'PASS')
        args,kw=session.post.call_args
        self.assertIn('/publishers/anthropic/models/claude-sonnet-5-5:rawPredict',args[0])
        self.assertIn('output_config',kw['json']);self.assertNotIn('tools',kw['json'])
        self.assertEqual(kw['json']['thinking'],{'type':'between_tools'})
        self.assertFalse(kw['allow_redirects']);session.close.assert_called_once()

    def test_truncated_claude_output_is_not_imported_or_repaired(self):
        import tracker_claude_backend as backend
        modules,session=self.fake_modules({'stop_reason':'max_tokens','content':[{'type':'text','text':'{"ready":'}]})
        with patch.dict(sys.modules,modules),patch.dict(os.environ,{'GCP_ID':'synthetic-project'}):
            with self.assertRaises(backend.ClaudeResponseError):backend.smoke_test(settings())
        self.assertEqual(session.post.call_count,1)

    def test_permission_failure_is_explicit_and_not_retried(self):
        import tracker_claude_backend as backend
        modules,session=self.fake_modules({'error':{'message':'synthetic permission error'}},403)
        with patch.dict(sys.modules,modules),patch.dict(os.environ,{'GCP_ID':'synthetic-project'}):
            with self.assertRaisesRegex(backend.ClaudeResponseError,'HTTP 403'):backend.smoke_test(settings())
        self.assertEqual(session.post.call_count,1)


if __name__=='__main__':
    unittest.main()
