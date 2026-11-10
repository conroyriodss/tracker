"""Opt-in Gemini execution of the tracker's existing sealed review batches.

Same strict import and named human approval as the file-exchange path. This
module cannot promote recommendations or mutate cards. No web grounding here.
"""
from __future__ import annotations
import copy
import datetime as dt
import json
import uuid
from pathlib import Path
import jsonschema
import automated_review as review
import tracker_gcp_backend as backend
import tracker_claude_backend as claude_backend
from evidence_store import canonical_json, digest, now, write_json, seal_run

def wire_schema(schema):
    """Keep the SDK's supported schema subset; full local schema still validates."""
    if isinstance(schema,list): return [wire_schema(x) for x in schema]
    if not isinstance(schema,dict): return schema
    return {k:wire_schema(v) for k,v in schema.items() if k not in ('allOf','if','then','else','$schema')}

def human_handled_keys():
    from review_workflow import operationally_decided_review_keys
    keys = operationally_decided_review_keys()
    for row in review.load_approvals().to_dict('records'):
        if review._truthy(row.get('approved')) and not row.get('promotion_status','').startswith(('quarantined','superseded')):
            keys.add(row['review_key'])
    return keys

def assert_human_pending(items):
    if human_handled_keys().intersection(item['review_key'] for item in items):
        raise ValueError('Human approval or adjudication already exists for a batch item; refresh the queue plan.')

def run_batch(batch_dir, config, *, import_results=True, generate=None, provider='gemini'):
    batch_dir=Path(batch_dir).resolve()
    manifest=json.loads((batch_dir/'manifest.json').read_text())
    manifest_sha=digest(canonical_json(manifest))
    if (batch_dir/'manifest.sha256').read_text().strip()!=manifest_sha:
        raise ValueError('Batch manifest seal mismatch.')
    for name,key in [('items.jsonl','items_sha256'),('response_schema.json','response_schema_sha256')]:
        if digest((batch_dir/name).read_bytes())!=manifest[key]:raise ValueError('Batch changed: '+name)
    if provider not in ('gemini','claude'):raise ValueError('Unsupported GCP review provider.')
    if manifest['provider_hint'] not in (provider,'gcp_dual','generic'):raise ValueError('Export a compatible provider batch first.')
    if manifest.get('web_research_allowed'):raise ValueError('GCP batch execution requires a sealed supplied-evidence-only batch.')
    items=[json.loads(line) for line in (batch_dir/'items.jsonl').read_text().splitlines() if line.strip()]
    if not 1<=len(items)<=config.get('max_review_batch_items',5):raise ValueError('Batch exceeds configured item cap.')
    if len(items)!=manifest['item_count'] or len({i['review_key'] for i in items})!=len(items):
        raise ValueError('Batch item inventory mismatch.')
    for item in items:
        if item['item_sha256']!=review._item_sha(item):raise ValueError('Review item hash mismatch.')
        valid,reason=review._current_item_state(item)
        if not valid:raise ValueError('Stale review batch: '+reason)
    assert_human_pending(items)
    schema=json.loads((batch_dir/'response_schema.json').read_text())
    # Reconstruct trusted instructions; do not execute edited packet prose.
    prompt=review._prompt_text(provider=provider,kind=manifest['queue_kind'],batch_manifest_sha256=manifest_sha,
                               batch_id=manifest['batch_id'],web_research=False)
    prompt+='\nSEALED_MANIFEST\n'+json.dumps(manifest)+'\nUNTRUSTED_ITEMS\n'+json.dumps(items,ensure_ascii=False)
    adapter=backend if provider=='gemini' else claude_backend
    model=config.get('extractors',{}).get('gemini',backend.DEFAULT_MODEL) if provider=='gemini' else config.get('claude_model',claude_backend.DEFAULT_MODEL)
    from importlib import metadata
    environment={'python':__import__('sys').version,'packages':{d.metadata['Name']:d.version for d in metadata.distributions() if d.metadata.get('Name')}}
    key=digest(canonical_json({'prompt':prompt,'schema':schema,'model':model,'provider':provider,
                              'tokens':config['max_output_tokens'],'thinking':config.get('gemini_thinking_level'),
                              'location':config.get('gcp_location') if provider=='gemini' else config.get('claude_location','global'),
                              'backend_sha256':digest(Path(adapter.__file__).read_bytes()),
                              'runner_sha256':digest(Path(__file__).read_bytes()),'environment':environment}))
    cache=Path(review.PROJECT)/'cache/gcp_review'/key
    ttl=config.get('model_cache_days',7)
    cached=None
    if (cache/'result.json').exists() and ttl>0:
        record=json.loads((cache/'record.json').read_text())
        age=dt.datetime.now(dt.timezone.utc)-dt.datetime.fromisoformat(record['created_at'])
        if 0<=age.total_seconds()<=ttl*86400:
            data=(cache/'result.json').read_bytes()
            if digest(data)!=record['sha256']:raise ValueError('Model cache hash mismatch.')
            cached=json.loads(data)
    run=Path(review.RUNS)/'gcp_review'/(dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'-'+uuid.uuid4().hex[:8])
    run.mkdir(parents=True,exist_ok=False)
    write_json(run/'plan.json',{'manifest':manifest,'manifest_sha256':manifest_sha,'cache_key':key,
                              'requested_model':model,'max_output_tokens':config['max_output_tokens'],
                              'provider':provider,
                              'thinking_level':config.get('gemini_thinking_level'),'started_at':now(),
                              'hypothesis_status':'EXPLORATORY_NOT_PREREGISTERED','cards_mutated':False})
    write_json(run/'environment.json',environment)
    write_json(run/'batch_manifest.json',manifest)
    write_json(run/'items.json',items)
    if cached is not None:write_json(run/'cache_reuse.json',record)
    (run/'prompt.txt').write_text(prompt)
    write_json(run/'schema.json',schema)
    try:
        if cached is None:
            candidate,raw,request=(generate or adapter.extract)(model,prompt,config['max_output_tokens'],wire_schema(schema),config)
            diagnostic=backend.response_diagnostics(raw) if provider=='gemini' else claude_backend.diagnostics(raw)
            write_json(run/'provider_io.json',{'request':request,'response':raw,'diagnostics':diagnostic})
            candidate=copy.deepcopy(candidate)
            # Transport provenance comes from the client, never from model assertions.
            candidate['provider']=provider
            candidate['model']=(raw.get('modelVersion',raw.get('model_version')) if provider=='gemini' else raw.get('model')) or model
            candidate['generated_at']=now()
        else:candidate=cached
        jsonschema.validate(candidate,schema)
        for name,value in [('bundle_id',manifest['bundle_id']),('batch_id',manifest['batch_id']),('batch_manifest_sha256',manifest_sha)]:
            if candidate[name]!=value:raise ValueError('Model returned wrong batch identity: '+name)
        recs=candidate['recommendations'];by_key={i['review_key']:i for i in items}
        if len(recs)!=len(items) or {r['review_key'] for r in recs}!=set(by_key):raise ValueError('Incomplete recommendation coverage.')
        for row in recs:
            errors=review._validate_recommendation(row,by_key[row['review_key']],manifest['queue_kind'])
            if errors:raise ValueError('; '.join(errors))
            if row['external_sources']:raise ValueError('No external sources were consulted by this runner.')
        write_json(run/'response.json',candidate)
        if cached is None:
            write_json(cache/'result.json',candidate)
            write_json(cache/'record.json',{'created_at':now(),'sha256':digest((cache/'result.json').read_bytes()),
                                           'requested_model':model,'returned_model':candidate['model'],
                                           'provider_io_sha256':digest((run/'provider_io.json').read_bytes()),
                                           'origin_run':str(run)})
        assert_human_pending(items)
        imported=review.import_review_response(run/'response.json',strict_complete_batches=True,allow_safe_json_repair=False) if import_results else None
        write_json(run/'summary.json',{'status':'CANDIDATES_REQUIRE_HUMAN_APPROVAL','cache_hit':cached is not None,
                                      'model_calls':int(cached is None),'recommendations':len(recs),'import':imported,'cards_mutated':False})
    except Exception as exc:
        if isinstance(exc,(backend.GeminiResponseError,claude_backend.ClaudeResponseError)):
            write_json(run/'provider_io.json',{'request':exc.request,'response':exc.response,'diagnostics':exc.diagnostics})
        write_json(run/'summary.json',{'status':'FAILED','error_type':type(exc).__name__,'detail':str(exc),'cards_mutated':False})
        seal_run(run)
        exc.run_dir=str(run)
        raise
    seal_run(run)
    return {'run_dir':str(run),'cache_hit':cached is not None,'recommendations':len(recs),'import':imported}

def interactive_google_search(prompt, config):
    """Return an in-memory grounded answer for the initiating user only.

    Do not feed this result into collection, assessment, evidence storage or BQ.
    Display the complete text and associated rendered Search Suggestions.
    """
    if not prompt.strip() or len(prompt)>4000:raise ValueError('Enter a research question of 1..4000 characters.')
    client=backend.create_client(config)
    try:
        response=client.models.generate_content(model=config['extractors']['gemini'],contents=prompt,
            config={'max_output_tokens':config['max_output_tokens'],'thinking_config':{'thinking_level':'LOW'},
                    'tools':[{'google_search':{}}]})
        raw=response.model_dump(mode='json',by_alias=True,exclude_none=True)
    finally:client.close()
    candidates=raw.get('candidates') or []
    if not candidates or candidates[0].get('finishReason')!='STOP':
        raise ValueError('Interactive search did not complete; no result stored.')
    candidate=candidates[0]
    return {'text':''.join(p.get('text','') for p in candidate.get('content',{}).get('parts',[]) if not p.get('thought')),
            'grounding':candidate.get('groundingMetadata',{}),'cache_permission':'never_evidence_cache'}
