"""Separate GCP settings; preserves prior tracker configuration and credentials."""
import copy
import json
import re
from pathlib import Path
from evidence_store import write_json,now

def validate(config):
    if config.get('review_second_provider','none') not in ('none','claude'):
        raise ValueError('review_second_provider must be none or claude.')
    for name in ('max_output_tokens','max_resources','max_pages_per_resource','max_review_batch_items','max_bytes','timeout_seconds','maximum_bytes_billed'):
        if not isinstance(config.get(name),int) or isinstance(config[name],bool) or config[name]<1:
            raise ValueError(name+' must be a positive integer.')
    for name in ('max_llm_calls','source_cache_days','model_cache_days','request_gap_seconds'):
        if not isinstance(config.get(name),(int,float)) or isinstance(config[name],bool) or config[name]<0:
            raise ValueError(name+' must be nonnegative.')
    if not isinstance(config['max_llm_calls'],int):raise ValueError('max_llm_calls must be an integer.')
    if config.get('gcp_project_env')!='GCP_ID':raise ValueError('Project comes only from the GCP_ID environment variable/Colab secret.')
    def inspect(v):
        if isinstance(v,dict):
            for k,x in v.items():
                if re.search(r'^(api_?key|password|secret|access_?token|credential|gcp_id)$',k,re.I):
                    raise ValueError('Credentials and raw project IDs must not be stored in settings.')
                inspect(x)
        elif isinstance(v,list):
            for x in v:inspect(x)
    inspect(config)
    return config

def load(project):
    path=Path(project)/'inputs/gcp_settings.json'
    if not path.exists():
        default=Path(__file__).resolve().parents[1]/'templates/gcp_settings.json'
        write_json(path,json.loads(default.read_text()))
    return validate(json.loads(path.read_text()))

def save(project,config):
    validate(config);path=Path(project)/'inputs/gcp_settings.json'
    old=json.loads(path.read_text()) if path.exists() else None
    write_json(Path(project)/'inputs/gcp_settings_history'/(now().replace(':','-')+'.json'),{'before':old,'after':copy.deepcopy(config)})
    write_json(path,config)
