"""Deterministic v19.2.1 maintenance controls; no execution or external writes."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MARKER = "NIH_TRACKER_OPERATIONS_V19_2"


def cell(kind, source):
    result = {"cell_type": kind, "id": "ops-" + hashlib.sha256(source.encode()).hexdigest()[:12],
              "source": source.splitlines(keepends=True), "metadata": {}}
    if kind == "code":
        result.update(outputs=[], execution_count=None, metadata={"cellView": "form", "jupyter": {"source_hidden": True}})
    return result


INTRO = '''## 3a. Inventory and pin review sources

Run **Inventory only** first. Stop other notebooks and background writers before
capture. The report hashes existing files, identifies selected review sources,
and records protected dependencies. It does not move or remove project files.
Open the generated `index.html` locally; it contains internal operational paths
and must not be added to the public site.

After inspecting the selected source paths and SHA-256 values, choose **Pin
current review sources** to keep those exact queues selected during review.
Pins do not approve candidates. Changed or missing pinned bytes stop the review
dashboard. **Unpin review sources** preserves the prior selection in the audit
history; use it before intentionally selecting new runs after a pipeline refresh.
'''

CODE = '''# NIH_TRACKER_OPERATIONS_V19_2
PROJECT_ACTION = "Inventory only" # @param ["Inventory only", "Pin current review sources", "Unpin review sources"]
CONFIRM_SOURCE_SELECTION_CHANGE = False # @param {type:"boolean"}
if PROJECT_ACTION == "Inventory only":
    inventory_result = RUNTIME.run_script("step_project_inventory.py", ["inventory"], label="project_inventory")
    if not inventory_result.ok:
        print("Inventory is incomplete. Inspect its report and log before proceeding.")
elif CONFIRM_SOURCE_SELECTION_CHANGE:
    action = "pin-review" if PROJECT_ACTION.startswith("Pin") else "unpin-review"
    token = "PIN_REVIEW_SOURCES" if action == "pin-review" else "UNPIN_REVIEW_SOURCES"
    selection_result = RUNTIME.run_script("step_project_inventory.py", [action, "--confirm", token], label="review_source_selection")
    if not selection_result.ok:
        raise RuntimeError("Review source selection failed; inspect the log.")
else:
    print("Inspect the inventory, then acknowledge the selected source change.")
'''

CLEANUP = '''# NIH_TRACKER_CLEANUP_CONTROLS_V19_2_1
KEEP_RUNS_PER_STEP = 5 # @param [3, 5, 10, 20]
REMOVE_TEMPORARY_FILES = False # @param {type:"boolean"}
from maintenance_widgets import launch_cleanup_controls
cleanup_controls = launch_cleanup_controls(
    RUNTIME, keep_runs=KEEP_RUNS_PER_STEP, remove_temporary=REMOVE_TEMPORARY_FILES
)
'''


def extend():
    for path in sorted(ROOT.glob("nih_tracker_colab_v19_*.ipynb")):
        nb = json.loads(path.read_text())
        nb["cells"] = [c for c in nb["cells"] if not (MARKER in ''.join(c.get('source', [])) or ''.join(c.get('source', [])).startswith('## 3a. Inventory and pin'))]
        out = []
        for c in nb["cells"]:
            source = ''.join(c['source'])
            if source.startswith('# NIH Tracker'):
                source = source.replace('(v19.1.1; extends v19.0.2)', '(v19.2.1; extends v19.2)')
                source = source.replace('(v19.2; extends v19.1.1)', '(v19.2.1; extends v19.2)')
            if c['cell_type'] == 'code' and 'module_name in {' in source and '"run_provenance",' not in source:
                source = source.replace('"common", "identity_hygiene"', '"run_provenance", "code_provenance", "step_project_inventory", "common", "identity_hygiene"')
            if 'module_name in {' in source and '"maintenance_widgets",' not in source:
                source = source.replace('"run_provenance",', '"maintenance_widgets", "run_provenance",')
            if source.startswith('CLEANUP_ACTION =') or source.startswith('# NIH_TRACKER_CLEANUP_CONTROLS_'):
                source = CLEANUP
            if source.startswith('## 6. Preview or apply bounded cleanup'):
                source = '''## 6. Preview or apply bounded cleanup

Run the cell to list saved, unapplied sealed plans. No plan is selected by default.
Choose a plan and click **Inspect selected plan** to see its exact path, SHA-256,
eligible paths and protected paths. If the list is empty, click **Create plan**.
Apply is enabled only after inspection and both acknowledgments. Changed plan
bytes require fresh inspection; candidate changes are checked before deletion.

Structured run outputs,
decision dependencies and card transaction history are retained even beyond the
age/count threshold. Eligible logs and temporary paths are archived and every
archived file hash is verified before removal. Recovery ZIPs and receipts are
retained under `snapshots/maintenance/` and the plan folder. A small or empty
deletion set is expected when the ledger references historical runs.
'''
            if 'Run this block only when installing v19.0.2' in source:
                source = source.replace('installing v19.0.2', 'installing v19.2.1')
            if 'Production release installer was not found.' in source:
                source = source.replace('v19.0.2 bundle', 'v19.2.1 bundle')
            if 'bundle_candidates = []' in source:
                source = source.replace('parent.glob("*v19*bundle*.zip")', 'parent.glob("nih_tracker_v19_2_1_bundle.zip")')
                source = source.replace('nih_tracker_v19_2_bundle.zip', 'nih_tracker_v19_2_1_bundle.zip')
                source = source.replace('EXPECTED_RELEASE_VERSION = "19.2"', 'EXPECTED_RELEASE_VERSION = "19.2.1"')
                if 'EXPECTED_RELEASE_VERSION' not in source:
                    source = source.replace('_validate_release_inventory(RELEASE_ROOT)', '''_validate_release_inventory(RELEASE_ROOT)
import json
EXPECTED_RELEASE_VERSION = "19.2.1"
installed_manifest = json.loads((RELEASE_ROOT / "RELEASE_MANIFEST.json").read_text())
if installed_manifest.get("release") != EXPECTED_RELEASE_VERSION:
    raise RuntimeError("Upload nih_tracker_v19_2_1_bundle.zip to /content; an older release cannot be installed by this notebook.")''')
            c['source'] = source.splitlines(keepends=True)
            out.append(c)
            if 'status_result = RUNTIME.run_script(' in source and '"step_maintenance.py"' in source:
                out += [cell('markdown', INTRO), cell('code', CODE)]
        nb['cells'] = out
        path.write_text(json.dumps(nb, indent=2) + '\n')


if __name__ == '__main__':
    extend()
