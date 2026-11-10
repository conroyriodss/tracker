"""Deterministic extension of v19 notebooks; no network, credentials or execution."""
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parent
MARKER='NIH_TRACKER_GCP_EXTENSION_V19_1'

def cell(kind,source):
    value={'cell_type':kind,'id':'gcp-'+hashlib.sha256(source.encode()).hexdigest()[:12],
           'metadata':{},'source':source.splitlines(keepends=True)}
    if kind=='code':value.update(execution_count=None,outputs=[],metadata={'cellView':'form','jupyter':{'source_hidden':True}})
    return value

INTRO='''## Optional GCP evidence and assessment (v19.1.1)

This extends the v19.0.2 guided workflow. Enable it after installing this release.
Gemini recommendations, with an optional independent Claude assessment, enter
the existing **Review → Approve → Finalize** flow. Card/name candidates are now
included. The Gemini review tab has a resumable queue plan; see
`GCP_SEMIAUTOMATED_REVIEW.md` for the 132-card plus one-identifier backlog.
The A–D panel creates a separate evidence inventory and named review record;
it does not automatically publish claims or change cards.

Add `GCP_ID` to Colab Secrets and enable notebook access. The project uses Google
sign-in (ADC), not a Gemini API key. `NIH_TRACKER_REVIEWER` remains the reviewer
identity. Configure the bucket, BigQuery dataset, collector contact and approved
source scope in the Settings tab. Start with five resources.

Cloud writes, collection, Gemini calls and interactive Google Search each require
their own explicit action. Durable cache search uses independently captured sources.
Google-grounded research results are not evidence-cache inputs or crawler seeds.
'''

SETUP='''# NIH_TRACKER_GCP_EXTENSION_V19_1
ENABLE_GCP_TOOLS = False # @param {type:"boolean"}
if ENABLE_GCP_TOOLS:
    import subprocess
    import sys
    import os
    from pathlib import Path
    requirement = Path(common.PROJECT) / 'requirements-gcp.txt'
    if not requirement.is_file():
        raise FileNotFoundError('Install the v19.1.1 release with the maintenance notebook first.')
    subprocess.run([sys.executable, '-m', 'pip', 'install', '-q', '-r', str(requirement)], check=True)
    from importlib import metadata
    restarted_modules = []
    for module_name, package_name in [('pandas','pandas'), ('yaml','PyYAML'), ('requests','requests'),
                                      ('nbformat','nbformat'), ('ipywidgets','ipywidgets')]:
        loaded = sys.modules.get(module_name)
        if loaded is not None and getattr(loaded, '__version__', metadata.version(package_name)) != metadata.version(package_name):
            restarted_modules.append(package_name)
    if restarted_modules:
        raise RuntimeError('Dependencies changed (' + ', '.join(restarted_modules) + '). Restart the Colab runtime and rerun the notebook from the top.')
    from google.colab import auth, userdata
    try:
        os.environ['GCP_ID'] = userdata.get('GCP_ID').strip()
    except Exception:
        raise ValueError('Add GCP_ID to Colab Secrets and enable notebook access.') from None
    from gcp_config import load
    import tracker_gcp_backend
    tracker_gcp_backend.project_id(load(common.PROJECT))
    auth.authenticate_user()
    from gcp_workbench import launch
    GCP_PANEL = launch(common.PROJECT, reviewer=os.getenv('NIH_TRACKER_REVIEWER',''),
                       on_recommendations=globals().get('refresh_guided_status'))
    display(GCP_PANEL)
else:
    print('Optional GCP tools disabled; existing guided review remains available.')
'''

def extend():
    paths=[]
    for name in ('nih_tracker_colab_v19_review_workbench.ipynb','nih_tracker_colab_v19_pipeline.ipynb','nih_tracker_colab_v19_maintenance.ipynb'):
        path=ROOT/name;n=json.loads(path.read_text())
        # Upgrade/rerun is idempotent; do not remove existing workflow cells.
        n['cells']=[c for c in n['cells'] if not (MARKER in ''.join(c.get('source',[])) or
                    ''.join(c.get('source',[])).startswith('## Optional GCP evidence and assessment') or
                    ''.join(c.get('source',[])).startswith('## v19.1 GCP extension notes'))]
        for c in n['cells']:
            source=''.join(c.get('source',[]))
            if c['cell_type']=='markdown' and source.startswith('# NIH Tracker'):
                source=source.replace('(v19.0.2)','(v19.1.1; extends v19.0.2)').replace('(v19.1; extends v19.0.2)','(v19.1.1; extends v19.0.2)')
            if c['cell_type']=='code' and 'module_name in {' in source and '"tracker_gcp_backend",' not in source:
                source=source.replace('"common", "identity_hygiene"','"tracker_gcp_backend", "evidence_store", "evidence_collector", "capability_assessment", "gcp_review", "gcp_workbench", "gcp_config", "tracker_cache_bridge", "common", "identity_hygiene"')
            if c['cell_type']=='code' and 'module_name in {' in source and '"gcp_review_queue",' not in source:
                source=source.replace('"common", "identity_hygiene"','"gcp_review_queue", "tracker_claude_backend", "common", "identity_hygiene"')
            source=source.replace('AI_EXPORTABLE_FAMILIES = {"identifier", "rrid", "fairsharing", "crosswalk"}',
                                  'AI_EXPORTABLE_FAMILIES = {"identifier", "rrid", "fairsharing", "crosswalk", "card"}')
            source=source.replace('No pending identifier or crosswalk queue is available for AI batch export.',
                                  'No pending card/name, identifier or crosswalk queue is available for AI batch export.')
            source=source.replace('<b>ChatGPT/Perplexity batches</b> — identifiers and crosswalks only',
                                  '<b>File-exchange batches</b> — card/name, identifier and crosswalk candidates')
            c['source']=source.splitlines(keepends=True)
        n.setdefault('metadata',{}).setdefault('colab',{})['private_outputs']=True
        if 'review_workbench' in name:n['cells'] += [cell('markdown',INTRO),cell('code',SETUP)]
        else:n['cells'].append(cell('markdown','''## v19.1 GCP extension notes

Install this v19.1.1 package using the existing production release installer.
The new `requirements-gcp.txt`, source-cache modules, assessment rubric and review
panel are release-managed; existing cards, inputs, snapshots, caches and reviews
are preserved. Use the review workbench's optional GCP panel for execution.
The catalog build remains offline. No cloud resource or recurring job is created
by this notebook automatically. See `GCP_INTEGRATION_AND_ASSESSMENT.md`.
'''))
        path.write_text(json.dumps(n,indent=2)+'\n');paths.append(path)
    return paths

if __name__=='__main__':
    for path in extend():print(path.name)
