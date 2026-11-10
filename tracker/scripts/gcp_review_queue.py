"""Frozen, bounded Gemini review plans with durable per-batch resume receipts.

This module writes recommendation imports only. No approval, adjudication,
card, identifier or public catalog can be changed by a queue execution.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import shutil
import uuid
from collections import Counter
from pathlib import Path

import automated_review as review
import gcp_review
from evidence_store import canonical_json, digest, now, seal_run, verify_run, write_json
from gcp_config import validate
from review_widgets import adjudication_path, load_adjudications, normalize_review_file, overlay_saved_decisions

PLAN_SCHEMA = 'nih-tracker-gcp-review-queue/v1'


def _positive(value, name, maximum):
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= maximum:
        raise ValueError(f'{name} must be an integer in 1..{maximum}.')
    return value


def _code_hashes():
    root = Path(__file__).parent
    return {name: digest((root/name).read_bytes()) for name in (
        'gcp_review_queue.py', 'gcp_review.py', 'automated_review.py',
        'review_widgets.py', 'tracker_gcp_backend.py', 'tracker_claude_backend.py')}


def _registry_path(value):
    if not str(value or '').strip():
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path(review.INP)/path
    path = path.resolve()
    if path.parent != Path(review.INP).resolve() or not path.is_file():
        raise ValueError('Choose an existing registry CSV directly inside inputs/, or leave it blank.')
    return path


def _recommended_items(provider=None):
    """Only exact, still-current source-bound recommendations suppress new work."""
    found = set()
    loaded = {}
    for row in review.load_recommendations().to_dict('records'):
        if provider and row['provider'] != review._provider_family(provider):
            continue
        if not row['validation_status'].startswith('VALID'):
            continue
        if row.get('promotion_status', '').startswith(('quarantined', 'superseded')):
            continue
        try:
            key = (row['bundle_id'], row['batch_id'])
            if key not in loaded:
                loaded[key] = review._load_batch_items(review._find_batch(*key))
            item = loaded[key][row['review_key']]
            if item['item_sha256'] == row['item_sha256'] and review._current_item_state(item)[0]:
                found.add((row['review_key'], row['item_sha256']))
        except (KeyError, ValueError, OSError):
            continue
    return found


def _cached_sources(config):
    """Bounded retrieval of admitted source excerpts by existing resource UID.

    This is a source-cache lookup, not live Google Search. No generated answer
    or human review is eligible. Dates, identity-binding limits and hashes travel
    with each excerpt into both reviewers' identical evidence packets.
    """
    from evidence_store import EvidenceStore, ALLOWED_KINDS, verified_bytes
    if not config.get('review_use_source_cache',True) or config['source_cache_days'] <= 0:
        return {}
    cache_root = Path(review.PROJECT)/'cache/cloud_evidence'
    if not cache_root.is_dir():
        return {}
    store = EvidenceStore(cache_root)
    rows = sorted(store.observations(),key=lambda r:r['fetched_at'],reverse=True)
    result, seen = {}, set()
    for record in rows:
        uid, url = record['repo_uid'], record['url']
        if (uid,url) in seen or len(result.get(uid,[])) >= 3:
            continue
        age = dt.datetime.now(dt.timezone.utc)-dt.datetime.fromisoformat(record['fetched_at'])
        if not 0 <= age.total_seconds() <= config['source_cache_days']*86400:
            continue
        if record.get('source_kind') not in ALLOWED_KINDS or record.get('cache_permission') != 'public_cache_permitted' or record.get('parse_status') != 'PARSED':
            continue
        expected = 'urn:nih-tracker:observation:'+digest(canonical_json({k:v for k,v in record.items() if k!='observation_id'}))
        if record['observation_id'] != expected:
            raise ValueError('Cached source observation identity mismatch.')
        verified_bytes(store.blob_path(record['body_sha256']),record['body_sha256'])
        full_text = store.text(record)
        excerpt = full_text[:8000]
        result.setdefault(uid,[]).append({**record,'text':excerpt,'excerpt_sha256':digest(excerpt.encode()),
                                         'excerpt_truncated':len(excerpt)<len(full_text),
                                         'identity_verified_by_cache_lookup':False})
        seen.add((uid,url))
    return result


def preview(sources, config, *, max_items=10, registry_file=''):
    """Read-only selection; max_items is a global row cap across chosen queues."""
    validate(config)
    _positive(max_items, 'max_items', 5000)
    size = _positive(config['max_review_batch_items'], 'max_review_batch_items', 50)
    registry = _registry_path(registry_file)
    cards = review.all_cards()
    cached = _cached_sources(config)
    handled = gcp_review.human_handled_keys()
    providers = ['gemini','claude'] if config.get('review_second_provider') == 'claude' else ['gemini']
    recommended = set.intersection(*[_recommended_items(provider) for provider in providers])
    selection, summaries, seen = [], [], set()
    total = 0
    for source in dict.fromkeys(str(Path(p).resolve()) for p in sources):
        path = Path(source)
        kind, base = normalize_review_file(path)
        if kind not in ('card', 'verify', 'crosswalk'):
            raise ValueError('Unsupported queue kind: '+kind)
        queue = overlay_saved_decisions(base, load_adjudications(kind, adjudication_path(kind)))
        column = 'review_decision' if kind == 'card' else 'decision'
        pending = queue.loc[queue[column].str.upper().eq('PENDING')]
        if pending['review_key'].duplicated().any():
            raise ValueError('Duplicate stable review keys in '+source+'; resolve source duplicates first.')
        chosen, existing, human, overlap, capped = [], 0, 0, 0, 0
        for row in pending.to_dict('records'):
            key = row['review_key']
            if key in seen:
                overlap += 1
                continue
            seen.add(key)
            if key in handled:
                human += 1
                continue
            if kind == 'card' and registry:
                row.update(source_registry_file=registry.name, source_registry_sha256=review.sha256_file(registry))
            evidence = cached.get(row.get('repo_uid'),[])
            item = review._build_item(kind, row, cards, path,source_evidence=evidence)
            if (key, item['item_sha256']) in recommended:
                existing += 1
                continue
            if total >= max_items:
                capped += 1
                continue
            chosen.append({'review_key': key, 'item_sha256': item['item_sha256'],'source_evidence':evidence})
            total += 1
        summaries.append({'source': source, 'kind': kind, 'pending': len(pending), 'selected': len(chosen),
                          'already_recommended': existing, 'human_handled': human,
                          'duplicate_across_queues': overlap, 'beyond_row_cap': capped})
        if chosen:
            selection.append({'source': source, 'kind': kind, 'items': chosen})
    batches = sum((len(q['items'])+size-1)//size for q in selection)
    return {'schema': PLAN_SCHEMA, 'queues': summaries, 'selection': selection, 'selected_items': total,
            'batch_size': size, 'batches': batches, 'reviewers':providers,
            'maximum_model_calls_for_first_pass': batches*len(providers),
            'maximum_output_tokens_per_call': config['max_output_tokens'],
            'registry_file': str(registry) if registry else '', 'cards_mutated': False,
            'human_approval_required': True}


def create_plan(sources, config, *, max_items=10, registry_file=''):
    selected = preview(sources, config, max_items=max_items, registry_file=registry_file)
    if not selected['selected_items']:
        raise ValueError('No new eligible rows. Review existing recommendations; human-deferred candidates need additional evidence or manual adjudication.')
    root = Path(review.RUNS)/'gcp_review_queue'
    plan_id = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'-'+uuid.uuid4().hex[:8]
    directory = root/plan_id
    directory.mkdir(parents=True, exist_ok=False)
    batches = []
    for chosen in selected['selection']:
        report = review.export_review_bundle(
            chosen['source'], provider='gcp_dual' if len(selected['reviewers'])>1 else 'gemini', web_research=False,
            batch_size=selected['batch_size'], review_keys=[v['review_key'] for v in chosen['items']],
            registry_file=selected['registry_file'] or None,
            source_evidence_by_key={v['review_key']:v['source_evidence'] for v in chosen['items']})
        expected = {v['review_key']: v['item_sha256'] for v in chosen['items']}
        for source_batch in sorted((Path(report['bundle_root'])/'batches').glob('batch_*')):
            manifest = json.loads((source_batch/'manifest.json').read_text())
            items = review._load_batch_items(source_batch)
            if any(expected.get(key) != item['item_sha256'] for key, item in items.items()):
                raise ValueError('Source changed while freezing the plan; create a new plan.')
            relative = 'batches/'+manifest['batch_id']
            shutil.copytree(source_batch, directory/relative)
            for provider in selected['reviewers']:
                batches.append({'directory': relative, 'batch_id': manifest['batch_id'],
                                'execution_id':manifest['batch_id']+'-'+provider, 'provider':provider,
                                'bundle_id': manifest['bundle_id'], 'kind': chosen['kind'],
                                'items': [{'review_key': key, 'item_sha256': item['item_sha256']} for key,item in items.items()]})
    plan = {'schema': PLAN_SCHEMA, 'plan_id': plan_id, 'created_at': now(),
            'config': copy.deepcopy(config), 'code_sha256': _code_hashes(),
            'selection_summary': {k: v for k,v in selected.items() if k != 'selection'},
            'batches': batches, 'human_approval_required': True, 'cards_mutated': False}
    write_json(directory/'plan.json', plan)
    seal_run(directory)
    return status(directory)


def load_plan(directory):
    directory = Path(directory).resolve()
    if directory.parent != (Path(review.RUNS)/'gcp_review_queue').resolve():
        raise ValueError('Select a plan from this project runs/gcp_review_queue/.')
    verify_run(directory)
    plan = json.loads((directory/'plan.json').read_text())
    if plan.get('schema') != PLAN_SCHEMA or plan.get('plan_id') != directory.name:
        raise ValueError('Queue plan identity mismatch.')
    validate(plan['config'])
    if plan['code_sha256'] != _code_hashes():
        raise ValueError('Review code changed after planning. Create a new plan with the current release.')
    for batch in plan['batches']:
        if not (directory/batch['directory']).resolve().is_relative_to(directory):
            raise ValueError('Invalid batch path.')
    return directory, plan


def _receipt_root(plan):
    return Path(review.PROJECT)/'reviews/gcp_queue_execution'/plan['plan_id']


def _receipts(plan):
    records = []
    for path in (_receipt_root(plan)/'receipts').glob('*.json'):
        data = path.read_bytes()
        if path.stem != digest(data):
            raise ValueError('Queue execution receipt hash mismatch.')
        record = json.loads(data)
        if record['plan_id'] != plan['plan_id']:
            raise ValueError('Execution receipt belongs to another plan.')
        records.append(record)
    latest = {}
    for record in sorted(records, key=lambda r:r['at']):
        latest[record['execution_id']] = record
    return latest


def _record(plan, batch, state, **details):
    value = {'schema':'nih-tracker-gcp-queue-receipt/v1', 'plan_id':plan['plan_id'],
             'batch_id':batch['batch_id'], 'execution_id':batch['execution_id'],
             'provider':batch['provider'], 'at':now(), 'status':state, **details}
    data = canonical_json(value)+b'\n'
    path = _receipt_root(plan)/'receipts'/(digest(data)+'.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as stream:
        stream.write(data)
    return value


def status(directory):
    directory, plan = load_plan(directory)
    latest = _receipts(plan)
    batches = [{**{k:v for k,v in batch.items() if k != 'items'},
                'item_count':len(batch['items']),
                'status':latest.get(batch['execution_id'],{}).get('status','PENDING'),
                'detail':latest.get(batch['execution_id'],{}).get('detail','')}
               for batch in plan['batches']]
    counts = dict(Counter(b['status'] for b in batches))
    return {'plan_dir':str(directory), 'plan_id':plan['plan_id'],
            'selection':plan['selection_summary'], 'batch_states':counts, 'batches':batches,
            'recommendation_generation_complete':all(b['status'] in ('IMPORTED','RECOVERED','HUMAN_HANDLED','ALREADY_RECOMMENDED') for b in batches),
            'review_complete':False, 'cards_mutated':False,
            'next_action':'Approve imported recommendations in the existing dashboard; unresolved DEFER rows remain for review.'}


def _imported(batch):
    wanted = {(item['review_key'],item['item_sha256']) for item in batch['items']}
    present = {(r['review_key'],r['item_sha256']) for r in review.load_recommendations().to_dict('records')
               if r['batch_id'] == batch['batch_id'] and r['bundle_id'] == batch['bundle_id']
               and r['provider'] == review._provider_family(batch['provider'])
               and r['validation_status'].startswith('VALID')}
    return wanted <= present


def _recover_response(batch, plan):
    """Recover a sealed successful response after an interrupted import, with no model call."""
    for path in sorted((Path(review.RUNS)/'gcp_review').glob('*/response.json'), reverse=True):
        try:
            result = json.loads(path.read_text())
            if result.get('batch_id') != batch['batch_id'] or result.get('bundle_id') != batch['bundle_id']:
                continue
            if review._provider_family(result.get('provider')) != review._provider_family(batch['provider']):
                continue
            verify_run(path.parent)
            summary = json.loads((path.parent/'summary.json').read_text())
            if summary.get('status') != 'CANDIDATES_REQUIRE_HUMAN_APPROVAL':
                continue
            imported = review.import_review_response(path, strict_complete_batches=True, allow_safe_json_repair=False)
            _record(plan,batch,'RECOVERED',run_dir=str(path.parent),model_call_started=False,import_result=imported)
            return True
        except (ValueError, OSError, KeyError):
            continue
    return False


def run_plan(directory, *, max_batches=2, retry_failed=False, generate=None, progress=None):
    """Each explicit invocation starts at most max_batches bounded model requests.

    Successful imports are skipped forever for this plan, independent of TTL.
    A failed/interrupted request is never automatically resubmitted. The user
    must explicitly select retry_failed; an interrupted provider call may bill again.
    """
    _positive(max_batches,'max_batches',1000)
    directory, plan = load_plan(directory)
    requests_started = 0
    lock = Path(review.PROJECT)/'cache/gcp_review_queue.lock'
    with review.FileLock(lock,purpose='bounded Gemini queue execution'):
        latest = _receipts(plan)
        covered = {provider:_recommended_items(provider) for provider in {b['provider'] for b in plan['batches']}}
        for batch in plan['batches']:
            prior = latest.get(batch['execution_id'],{})
            if prior.get('status') in ('IMPORTED','RECOVERED','HUMAN_HANDLED','ALREADY_RECOMMENDED'):
                continue
            if _imported(batch):
                _record(plan,batch,'RECOVERED',detail='Exact imported recommendation inventory already exists.',model_call_started=False)
                continue
            keys = {i['review_key'] for i in batch['items']}
            handled = keys & gcp_review.human_handled_keys()
            if handled:
                state = 'HUMAN_HANDLED' if handled == keys else 'BLOCKED'
                _record(plan,batch,state,detail='Human decisions changed; re-plan remaining rows when this batch is partial.',model_call_started=False)
                continue
            items = review._load_batch_items(directory/batch['directory'])
            stale = [reason for item in items.values() for ok,reason in [review._current_item_state(item)] if not ok]
            if stale:
                _record(plan,batch,'BLOCKED',detail='; '.join(sorted(set(stale))),model_call_started=False)
                continue
            recommended = {(i['review_key'],i['item_sha256']) for i in batch['items']} & covered[batch['provider']]
            if recommended:
                state = 'ALREADY_RECOMMENDED' if len(recommended) == len(items) else 'BLOCKED'
                _record(plan,batch,state,detail='Current recommendations already exist; re-plan remaining rows when partial.',model_call_started=False)
                continue
            if prior.get('status') in ('RUNNING','FAILED'):
                if _recover_response(batch,plan):
                    continue
                if not retry_failed:
                    continue
            elif prior.get('status') == 'BLOCKED':
                continue
            if requests_started >= max_batches:
                break
            _record(plan,batch,'RUNNING',model_call_started=True)
            requests_started += 1
            if progress:
                progress(f"Reviewing {batch['execution_id']} ({len(items)} items; model request {requests_started}/{max_batches} this click)")
            try:
                transport = generate.get(batch['provider']) if isinstance(generate,dict) else generate
                result = gcp_review.run_batch(directory/batch['directory'],plan['config'],generate=transport,provider=batch['provider'])
                if not _imported(batch):
                    raise ValueError('Expected imported recommendations are missing after batch execution.')
                _record(plan,batch,'IMPORTED',**result)
                covered[batch['provider']].update((i['review_key'],i['item_sha256']) for i in batch['items'])
            except Exception as exc:
                _record(plan,batch,'FAILED',detail=f'{type(exc).__name__}: {exc}',run_dir=getattr(exc,'run_dir',''))
                if progress:
                    progress('Stopped after a failed batch. Inspect its run; successful batches are retained.')
                break
    result = status(directory)
    result['batch_executions_started_this_invocation'] = requests_started
    return result


def archive_plan(directory, config):
    """Explicitly archive sealed plan and model runs; no human approvals/reviews."""
    from evidence_store import sync_run
    directory, plan = load_plan(directory)
    runs = {record['run_dir'] for record in _receipts(plan).values() if record.get('run_dir')}
    receipts = [sync_run(review.PROJECT, directory, config)]
    for run in sorted(runs):
        receipts.append(sync_run(review.PROJECT, run, config))
    return {'plan_id':plan['plan_id'], 'archived_runs':len(receipts),
            'manifest_hashes':[r['run_manifest_sha256'] for r in receipts]}
