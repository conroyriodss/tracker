"""Admit exact retained tracker snapshots without rewriting their observation date.

Only caches linked by exact current-card identifiers/homepages are considered.
Normalized registry extracts without retained response bytes are not relabeled
as raw evidence. Source-specific cache admission remains explicit.
"""
import base64
import copy
import datetime as dt
import json
from pathlib import Path
from evidence_store import EvidenceStore,digest
from evidence_collector import parse_body,PARSER_VERSION
from capability_assessment import make_plan

def import_existing(project,shared_cache,cards,config):
    store=EvidenceStore(Path(project)/'cache/cloud_evidence');shared=Path(shared_cache).resolve()
    updated=copy.deepcopy(config);report={'imported':[],'skipped':[]}
    caches={}
    for name in ('re3data_interoperability_cache.json','fairsharing_resource_profile_cache.json','website_resource_profile_cache.json'):
        path=shared/name
        value=json.loads(path.read_text()) if path.is_file() else {}
        records=value.get('records',{})
        caches[name]=records if isinstance(records,dict) else {}
    for selected in make_plan(cards,config)['selection']:
        uid=selected['repo_uid'];card=cards[uid]
        permission=config.get('resources',{}).get(uid,{}).get('cache_permission',config['default_source_cache_permission'])
        if permission!='public_cache_permitted':
            report['skipped'].append({'repo_uid':uid,'reason':'cache_permission_not_established'});continue
        candidates=[]
        for link in card.get('links',[]) or []:
            kind=str(link.get('id_type','')).lower();value=str(link.get('id_value','')).strip()
            if kind=='re3data' and value.startswith('r3d'):
                row=caches['re3data_interoperability_cache.json'].get(value)
                if row:candidates.append(('registry_record',row,row.get('api_endpoint',''),'application/xml','re3data_interoperability_cache.json'))
            elif kind in ('fairsharing','fs') and value.lower().startswith('10.25504/'):
                row=caches['fairsharing_resource_profile_cache.json'].get(value.casefold())
                if row:candidates.append(('registry_record',row,row.get('endpoint',''),'application/json','fairsharing_resource_profile_cache.json'))
        homepage=selected['homepage'];row=caches['website_resource_profile_cache.json'].get(homepage)
        if row:candidates.append(('official_documentation',row,homepage,'text/html','website_resource_profile_cache.json'))
        for kind,row,url,media,origin in candidates:
            try:
                if not url or not row.get('response_sha256') or not row.get('fetched_at'):raise ValueError('Missing original URL/hash/date.')
                timestamp=dt.datetime.fromisoformat(row['fetched_at'].replace('Z','+00:00'))
                if timestamp.tzinfo is None:raise ValueError('Original observation timezone is missing.')
                if row.get('raw_xml_base64'):body=base64.b64decode(row['raw_xml_base64'],validate=True)
                elif row.get('snapshot_path'):
                    path=Path(row['snapshot_path'])
                    if not path.is_absolute():path=shared/path
                    path=path.resolve()
                    if not path.is_relative_to(shared):raise ValueError('Snapshot path is outside the selected shared-cache root.')
                    body=path.read_bytes()
                else:raise ValueError('Missing original source bytes; normalized record is not a raw snapshot.')
                if digest(body)!=row['response_sha256']:raise ValueError('Legacy snapshot SHA-256 mismatch.')
                text,_,status=parse_body(body,media)
                rec=store.record(repo_uid=uid,url=url,body=body,text=text,source_kind=kind,cache_permission=permission,
                    fetched_at=row['fetched_at'],content_type=media,parser_version=PARSER_VERSION,parse_status=status,
                    identity_binding='exact_current_tracker_card_link',source_version='legacy-cache:'+origin,http_status=row.get('http_status'))
                entries=updated.setdefault('resources',{}).setdefault(uid,{}).setdefault('sources',[])
                if not any(s.get('url')==url for s in entries):
                    entries.append({'url':url,'source_kind':kind,'cache_permission':permission,
                                    'identity_binding':'exact_current_tracker_card_link'})
                report['imported'].append({'repo_uid':uid,'snapshot_id':rec['snapshot_id'],'source':origin,'fetched_at':rec['fetched_at']})
            except Exception as e:report['skipped'].append({'repo_uid':uid,'source':origin,'reason':str(e)})
    return report,updated
