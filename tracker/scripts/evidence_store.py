"""Content-addressed evidence cache; optional GCS mirror and BigQuery index.

Only explicitly admitted public source material belongs here. Search-grounded
answers, credentials, human review records and controlled data are excluded.
"""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

STORE_SCHEMA = 'nih-tracker-evidence-store/v1'
ALLOWED_KINDS = {'official_documentation', 'registry_record', 'public_api', 'schema', 'public_sample'}

def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()

def canonical_json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()

def digest(value):
    return hashlib.sha256(value).hexdigest()

def write_json(path, value):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    temp.write_bytes(canonical_json(value)+b'\n'); os.replace(temp,path)

def safe_url(value):
    p=urlsplit(str(value).strip())
    if p.scheme not in ('https','http') or not p.hostname or p.username or p.password or p.port not in (None,80,443):
        raise ValueError('Public HTTP(S) URL without embedded credentials required.')
    if any(re.search(r'(?:^|[_-])(token|key|password|signature|credential)(?:$|[_-])',k,re.I)
           for k in (pair.split('=',1)[0] for pair in p.query.split('&'))):
        raise ValueError('Credential-bearing query URLs cannot enter the evidence cache.')
    return urlunsplit((p.scheme,p.netloc.lower(),p.path or '/',p.query,''))

def verified_bytes(path, sha256):
    data=Path(path).read_bytes()
    if digest(data)!=sha256: raise ValueError('Evidence SHA-256 mismatch: '+str(path))
    return data

class EvidenceStore:
    def __init__(self, root):
        self.root=Path(root)
        self.root.mkdir(parents=True,exist_ok=True)

    def blob_path(self, sha256):
        if not re.fullmatch('[a-f0-9]{64}',sha256): raise ValueError('Invalid content digest.')
        return self.root/'blobs'/sha256[:2]/sha256

    def put_blob(self, data):
        sha=digest(data); path=self.blob_path(sha); path.parent.mkdir(parents=True,exist_ok=True)
        try:
            with path.open('xb') as f: f.write(data)
        except FileExistsError: verified_bytes(path,sha)
        return sha

    def record(self, *, repo_uid, url, body, text, source_kind, cache_permission,
               final_url=None, content_type='', headers=None, fetched_at=None, parse_status='PARSED',
               parser_version='public-text/v1', source_version='', identity_binding='operator_selected',http_status=None):
        if source_kind not in ALLOWED_KINDS:
            raise ValueError('Only independent public sources can enter the evidence store; no grounded search results.')
        if cache_permission!='public_cache_permitted':
            raise ValueError('Confirm source-specific permission for durable public-source caching first.')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*',repo_uid): raise ValueError('Invalid tracker resource UID.')
        headers={str(k).lower():str(v) for k,v in (headers or {}).items()}
        if re.search(r'\b(no-store|private)\b',headers.get('cache-control',''),re.I):
            raise ValueError('Source cache-control forbids this shared durable cache.')
        url=safe_url(url); final_url=safe_url(final_url or url)
        raw_sha=self.put_blob(body); text_sha=self.put_blob(text.encode())
        source_id='urn:nih-tracker:source:'+digest(url.encode())
        # Parser revisions are separate from source content versions.
        snapshot_id='urn:nih-tracker:snapshot:'+digest(canonical_json([source_id,raw_sha,text_sha,parser_version]))
        record={'schema':STORE_SCHEMA,'repo_uid':repo_uid,'source_id':source_id,'snapshot_id':snapshot_id,
                'url':url,'final_url':final_url,'body_sha256':raw_sha,'text_sha256':text_sha,
                'source_kind':source_kind,'cache_permission':cache_permission,'identity_binding':identity_binding,
                'content_type':content_type,'parse_status':parse_status,'parser_version':parser_version,
                'http_status':http_status,
                'source_version':source_version,'fetched_at':fetched_at or now(),
                'etag':headers.get('etag',''),'last_modified':headers.get('last-modified','')}
        observation_id='urn:nih-tracker:observation:'+digest(canonical_json(record))
        record['observation_id']=observation_id
        write_json(self.root/'observations'/(observation_id.rsplit(':',1)[1]+'.json'),record)
        return record

    def text(self, record):
        return verified_bytes(self.blob_path(record['text_sha256']),record['text_sha256']).decode()

    def observations(self):
        return [json.loads(p.read_text()) for p in sorted((self.root/'observations').glob('*.json'))]

    def latest(self, repo_uid, url, max_age_days):
        if max_age_days<=0: return None
        rows=[r for r in self.observations() if r['repo_uid']==repo_uid and r['url']==url]
        if not rows: return None
        row=max(rows,key=lambda r:r['fetched_at'])
        age=dt.datetime.now(dt.timezone.utc)-dt.datetime.fromisoformat(row['fetched_at'])
        if age.total_seconds()<0 or age.total_seconds()>max_age_days*86400: return None
        verified_bytes(self.blob_path(row['body_sha256']),row['body_sha256']); self.text(row)
        return row

    def search(self, query, *, limit=20):
        terms=query.casefold().split()
        if not terms or not 1<=limit<=100: raise ValueError('Nonempty query and limit 1..100 required.')
        found=[]; seen=set()
        for r in sorted(self.observations(),key=lambda r:r['fetched_at'],reverse=True):
            key=(r['repo_uid'],r['snapshot_id'])
            if key in seen: continue
            seen.add(key); text=self.text(r)
            if all(t in (r['url']+' '+text).casefold() for t in terms):
                found.append({**r,'excerpt':text[:1200]})
                if len(found)>=limit: break
        return found

def seal_run(directory):
    directory=Path(directory)
    if (directory/'manifest.json').exists(): raise ValueError('Run already sealed.')
    files={p.relative_to(directory).as_posix():digest(p.read_bytes()) for p in sorted(directory.rglob('*')) if p.is_file()}
    manifest={'schema':'nih-tracker-evidence-run-manifest/v1','files':files,'sealed_at':now()}
    write_json(directory/'manifest.json',manifest)
    return digest((directory/'manifest.json').read_bytes())

def verify_run(directory):
    directory=Path(directory).resolve(); manifest=json.loads((directory/'manifest.json').read_text())
    for name,sha in manifest['files'].items():
        path=(directory/name).resolve()
        if not path.is_relative_to(directory): raise ValueError('Run manifest path escapes directory.')
        verified_bytes(path,sha)
    return manifest

def cloud_clients(config):
    from tracker_gcp_backend import project_id
    import google.auth
    from google.cloud import storage,bigquery
    project=project_id(config)
    credentials,_=google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'],quota_project_id=project)
    return storage.Client(project=project,credentials=credentials),bigquery.Client(project=project,credentials=credentials,
                                                                                 location=config['bigquery_location'])

def table_id(config, name='source_observations'):
    from tracker_gcp_backend import project_id
    dataset=config.get('bigquery_dataset','')
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,1023}',dataset): raise ValueError('Invalid BigQuery dataset ID.')
    if name not in ('source_observations',): raise ValueError('Unmanaged table rejected.')
    return project_id(config)+'.'+dataset+'.'+name

def upload_immutable(bucket, name, data):
    """Create once. Verify exact existing bytes on a precondition conflict."""
    from google.api_core.exceptions import PreconditionFailed
    blob=bucket.blob(name); sha=digest(data)
    blob.metadata={'sha256':sha}
    try: blob.upload_from_string(data,if_generation_match=0,checksum='auto',timeout=120)
    except PreconditionFailed:
        blob.reload()
        existing=blob.download_as_bytes(if_generation_match=blob.generation,timeout=120)
        if digest(existing)!=sha: raise ValueError('Cloud object hash conflict: '+name)
    if blob.generation is None: blob.reload()
    return {'uri':'gs://'+bucket.name+'/'+name,'generation':str(blob.generation),'sha256':sha}

def initialize_cloud(config):
    """Explicit call only; never creates buckets or changes retention policies."""
    from google.cloud import bigquery
    storage,bq=cloud_clients(config); bucket=storage.get_bucket(config['gcs_bucket'])
    if not bucket.iam_configuration.uniform_bucket_level_access_enabled:
        raise ValueError('Use a bucket with uniform bucket-level access.')
    if bucket.iam_configuration.public_access_prevention!='enforced':
        raise ValueError('Enforce public access prevention on the evidence bucket.')
    dataset=bigquery.Dataset(table_id(config).rsplit('.',1)[0]); dataset.location=config['bigquery_location']
    from google.api_core.exceptions import NotFound
    try:actual=bq.get_dataset(dataset.reference)
    except NotFound:actual=bq.create_dataset(dataset,exists_ok=True)
    if actual.location.lower()!=config['bigquery_location'].lower(): raise ValueError('Dataset location mismatch.')
    fields=[bigquery.SchemaField(n,'STRING') for n in
            ('observation_id','snapshot_id','source_id','repo_uid','url','final_url','body_sha256','text_sha256',
             'source_kind','source_version','parser_version','parse_status','gcs_uri','gcs_generation','text')]
    fields += [bigquery.SchemaField('fetched_at','TIMESTAMP')]
    table=bigquery.Table(table_id(config),schema=fields)
    table.time_partitioning=bigquery.TimePartitioning(field='fetched_at')
    table.clustering_fields=['repo_uid','source_kind']
    actual_table=bq.create_table(table,exists_ok=True)
    existing={field.name:field.field_type for field in actual_table.schema}
    if any(existing.get(field.name)!=field.field_type for field in fields):raise ValueError('Existing source-index schema is incompatible.')
    return {'bucket':bucket.name,'dataset':actual.dataset_id,'location':actual.location}

def smoke_cloud(config):
    from google.cloud import bigquery
    from tracker_gcp_backend import smoke_test
    storage,bq=cloud_clients(config)
    bucket=storage.get_bucket(config['gcs_bucket'])
    rows=list(bq.query('SELECT 1 AS access_ok',job_config=bigquery.QueryJobConfig(maximum_bytes_billed=1000000)).result(timeout=60))
    if len(rows)!=1 or rows[0]['access_ok']!=1:raise ValueError('BigQuery access test failed.')
    return {'gemini':smoke_test(config),'bigquery':'PASS','gcs_bucket_read':'PASS',
            'uniform_bucket_access':bucket.iam_configuration.uniform_bucket_level_access_enabled,
            'public_access_prevention':bucket.iam_configuration.public_access_prevention,
            'note':'This tests access, not full extraction or durable cloud writes.'}

def sync_sources(store, config):
    """Upload only this typed source store, never recursively upload the project."""
    from google.cloud import bigquery
    storage,bq=cloud_clients(config);bucket=storage.get_bucket(config['gcs_bucket'])
    # Do not silently publish source snapshots through an existing public bucket.
    if not bucket.iam_configuration.uniform_bucket_level_access_enabled or bucket.iam_configuration.public_access_prevention!='enforced':
        raise ValueError('Private evidence bucket controls are required; run cloud preflight.')
    uploads={}; rows=[]
    for record in store.observations():
        if record['source_kind'] not in ALLOWED_KINDS or record['cache_permission']!='public_cache_permitted':
            raise ValueError('Unapproved cache record.')
        for field in ('body_sha256','text_sha256'):
            sha=record[field]
            if sha not in uploads:
                uploads[sha]=upload_immutable(bucket,'nih-tracker/evidence/v1/blobs/'+sha[:2]+'/'+sha,
                                              verified_bytes(store.blob_path(sha),sha))
        metadata=upload_immutable(bucket,'nih-tracker/evidence/v1/observations/'+record['observation_id'].rsplit(':',1)[1]+'.json',
                                  canonical_json(record)+b'\n')
        row={k:record.get(k,'') for k in ('observation_id','snapshot_id','source_id','repo_uid','url','final_url','body_sha256',
                                         'text_sha256','source_kind','source_version','parser_version','parse_status','fetched_at')}
        row.update(gcs_uri=metadata['uri'],gcs_generation=metadata['generation'],text=store.text(record))
        rows.append(row)
    if not rows:return {'source_rows':0,'objects':0}
    # Content-derived job IDs make retries of the exact load idempotent. Distinct
    # observation inventories may overlap; search de-duplicates by observation ID.
    job_id='nih_tracker_sources_'+digest(canonical_json([table_id(config),config['bigquery_location'],rows]))
    from google.api_core.exceptions import Conflict
    try:
        job=bq.load_table_from_json(rows,table_id(config),job_id=job_id,
                                   job_config=bigquery.LoadJobConfig(write_disposition='WRITE_APPEND'))
    except Conflict:job=bq.get_job(job_id,location=config['bigquery_location'])
    job.result(timeout=180)
    receipt={'source_rows':len(rows),'objects':len(uploads)+len(rows),'load_job_id':job_id,
             'synced_at':now(),'objects_by_sha256':uploads}
    write_json(store.root/'sync_receipts'/(job_id+'.json'),receipt)
    return receipt

def search_bigquery(config, query, limit=20, max_bytes=100_000_000):
    from google.cloud import bigquery
    if not query.strip() or not 1<=limit<=100 or max_bytes<1: raise ValueError('Invalid search bounds.')
    _,client=cloud_clients(config)
    sql=('SELECT observation_id,snapshot_id,source_id,repo_uid,url,fetched_at,gcs_uri,gcs_generation, '
         'SUBSTR(text,1,1200) AS excerpt FROM `'+table_id(config)+'` '
         'WHERE STRPOS(LOWER(text),LOWER(@query))>0 '
         'QUALIFY ROW_NUMBER() OVER (PARTITION BY observation_id ORDER BY fetched_at DESC)=1 '
         'ORDER BY fetched_at DESC LIMIT @limit')
    job_config=bigquery.QueryJobConfig(maximum_bytes_billed=max_bytes,
        query_parameters=[bigquery.ScalarQueryParameter('query','STRING',query),
                          bigquery.ScalarQueryParameter('limit','INT64',limit)])
    return [dict(row) for row in client.query(sql,job_config=job_config).result(timeout=120)]

def sync_run(project, run, config):
    """Explicit immutable evidence-run archive; no recursive Drive/project sync."""
    project=Path(project).resolve();run=Path(run).resolve()
    parents=[project/'runs/capability_assessment',project/'runs/gcp_review',project/'runs/gcp_review_queue']
    if not any(run.parent==p for p in parents):raise ValueError('Only sealed assessment/GCP review runs may be archived.')
    manifest=verify_run(run);manifest_sha=digest((run/'manifest.json').read_bytes())
    storage,_=cloud_clients(config);bucket=storage.get_bucket(config['gcs_bucket'])
    if not bucket.iam_configuration.uniform_bucket_level_access_enabled or bucket.iam_configuration.public_access_prevention!='enforced':
        raise ValueError('Private evidence bucket controls required.')
    uploaded={}
    for name in [*manifest['files'],'manifest.json']:
        uploaded[name]=upload_immutable(bucket,'nih-tracker/runs/v1/'+manifest_sha+'/'+name,(run/name).read_bytes())
    receipt={'run_manifest_sha256':manifest_sha,'run_directory':str(run),'objects':uploaded,'archived_at':now()}
    write_json(project/'cache/cloud_evidence/run_sync_receipts'/(manifest_sha+'.json'),receipt)
    return receipt

def restore_observation(store,config,uri,generation):
    """Restore a chosen generation from a BigQuery hit, verifying content hashes."""
    prefix='gs://'+config['gcs_bucket']+'/nih-tracker/evidence/v1/observations/'
    if not uri.startswith(prefix) or not re.fullmatch(r'[a-f0-9]{64}\.json',uri[len(prefix):]):
        raise ValueError('Unexpected source-object URI.')
    storage,_=cloud_clients(config);bucket=storage.bucket(config['gcs_bucket'])
    data=bucket.blob(uri.split('/',3)[3],generation=int(generation)).download_as_bytes(if_generation_match=int(generation))
    record=json.loads(data)
    if record['source_kind'] not in ALLOWED_KINDS or record['cache_permission']!='public_cache_permitted':raise ValueError('Unapproved source object.')
    expected=record.pop('observation_id')
    if expected!='urn:nih-tracker:observation:'+digest(canonical_json(record)):raise ValueError('Observation hash mismatch.')
    record['observation_id']=expected
    if uri.rsplit('/',1)[1]!=expected.rsplit(':',1)[1]+'.json':raise ValueError('Observation object-name mismatch.')
    for field in ('body_sha256','text_sha256'):
        sha=record[field];store.blob_path(sha)  # Validate before using the key.
        body=bucket.blob('nih-tracker/evidence/v1/blobs/'+sha[:2]+'/'+sha).download_as_bytes()
        if digest(body)!=sha:raise ValueError('Cloud evidence content changed.')
        store.put_blob(body)
    write_json(store.root/'observations'/(expected.rsplit(':',1)[1]+'.json'),record)
    return record
