"""Deterministic v19.2.3 maintenance controls; no execution or external writes."""
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

Start with **Inspect existing pins** for a bounded check of the selected source
files. It does not discover historical queues or calculate pending-row counts.
**Full inventory (slow audit)** is optional and reads the project history,
snapshots and prior reports. Stop other notebooks and background writers before
that audit. Progress reports the phase, current path, observed counts and elapsed
time every 10 seconds, including during slow reads. There is no reliable ETA.
The full report hashes files and records protected dependencies; it does not
move or remove existing project files.
Open the generated `index.html` locally; it contains internal operational paths
and must not be added to the public site.

After inspecting the selected source paths and SHA-256 values, choose **Pin
current review sources** to keep those exact queues selected during review.
Pins do not approve candidates. Changed or missing pinned bytes stop the review
dashboard. **Unpin review sources** preserves the prior selection in the audit
history; use it before intentionally selecting new runs after a pipeline refresh.
'''

CODE = '''# NIH_TRACKER_OPERATIONS_V19_2
PROJECT_ACTION = "Inspect existing pins" # @param ["Inspect existing pins", "Full inventory (slow audit)", "Pin current review sources", "Unpin review sources"]
CONFIRM_SOURCE_SELECTION_CHANGE = False # @param {type:"boolean"}
if "RUNTIME" not in globals():
    raise RuntimeError("Run the production runtime setup successfully before section 3a.")
if PROJECT_ACTION == "Full inventory (slow audit)":
    inventory_result = RUNTIME.run_script("step_project_inventory.py", ["inventory"], label="project_inventory")
    if not inventory_result.ok:
        print("Inventory is incomplete. Inspect its report and log before proceeding.")
elif PROJECT_ACTION == "Inspect existing pins":
    pin_status = RUNTIME.run_script("step_project_inventory.py", ["inspect-review"], label="inspect_review_pins")
    if not pin_status.ok:
        raise RuntimeError("Existing pins could not be verified; inspect the log before changing them.")
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

RUNTIME_CHECK = '''# NIH_TRACKER_INSTALLED_RELEASE_CHECK
globals().pop("RUNTIME", None)
import hashlib
import json
def _check_installed_release(project, expected):
    manifest_path = project / "RELEASE_MANIFEST.json"
    guidance = "Open the matching maintenance notebook, preview step 1, set APPLY_PREFILL=True and rerun step 1. After successful installation, restart the runtime and run setup again."
    if not manifest_path.is_file():
        raise RuntimeError("Installed release manifest is missing. " + guidance)
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("release") != expected:
        raise RuntimeError(f"Notebook requires {expected}; Drive has {manifest.get('release', 'unknown')}. " + guidance)
    critical = ("common.py", "notebook_runtime.py", "run_provenance.py", "code_provenance.py",
                "step_initialize_environments.py", "step_maintenance.py", "maintenance_widgets.py", "step_project_inventory.py", "operation_progress.py")
    for name in critical:
        relative = "scripts/" + name
        path = project / relative
        expected_hash = manifest.get("files", {}).get(relative, {}).get("sha256")
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
            raise RuntimeError(f"Installed code is missing or differs from its release manifest: {relative}. " + guidance)
    print(f"Installed release {expected}: {len(critical)} runtime file hashes verified.")
_check_installed_release(SCRIPTS.parent, "19.2.3")

'''


def extend():
    for path in sorted(ROOT.glob("nih_tracker_colab_v19_*.ipynb")):
        nb = json.loads(path.read_text())
        nb["cells"] = [c for c in nb["cells"] if not (MARKER in ''.join(c.get('source', [])) or ''.join(c.get('source', [])).startswith('## 3a. Inventory and pin'))]
        out = []
        for c in nb["cells"]:
            source = ''.join(c['source'])
            if source.startswith('# NIH Tracker'):
                for previous in ('(v19.1.1; extends v19.0.2)', '(v19.2; extends v19.1.1)', '(v19.2.1; extends v19.2)', '(v19.2.2; extends v19.2.1)'):
                    source = source.replace(previous, '(v19.2.3; extends v19.2.2)')
            if c['cell_type'] == 'code' and 'module_name in {' in source and '"run_provenance",' not in source:
                source = source.replace('"common", "identity_hygiene"', '"run_provenance", "code_provenance", "step_project_inventory", "common", "identity_hygiene"')
            if 'module_name in {' in source and '"maintenance_widgets",' not in source:
                source = source.replace('"run_provenance",', '"maintenance_widgets", "run_provenance",')
            if 'module_name in {' in source and '"operation_progress",' not in source:
                source = source.replace('"run_provenance",', '"operation_progress", "run_provenance",')
            if 'previous_scripts = globals()' in source:
                if '# NIH_TRACKER_INSTALLED_RELEASE_CHECK' in source:
                    before = source.split('# NIH_TRACKER_INSTALLED_RELEASE_CHECK', 1)[0]
                    after = source.split('previous_scripts = globals()', 1)[1]
                    source = before + RUNTIME_CHECK + 'previous_scripts = globals()' + after
                else:
                    source = source.replace('previous_scripts = globals()', RUNTIME_CHECK + 'previous_scripts = globals()')
                if 'Local logs:' not in source:
                    source += '\nprint(f"Local logs: {RUNTIME.local_debug_dir} (retained if Drive copy fails)")\n'
            if source.startswith('RUN_PIPELINE ='):
                if 'with ProgressReporter("pipeline preflight")' not in source:
                    source = source.replace('import traceback\n', 'import traceback\nfrom operation_progress import ProgressReporter\n')
                    start = source.index('        smoke = RUNTIME.smoke_suite(')
                    end = source.index('        if not smoke["passed"]:', start)
                    block = source[start:end]
                    source = (source[:start] + '        with ProgressReporter("pipeline preflight") as smoke_progress:\n'
                              + '            smoke_progress.update("mandatory smoke checks", current="inputs, cards and service checks")\n'
                              + ''.join('    ' + line if line.strip() else line for line in block.splitlines(keepends=True))
                              + source[end:])
                if 'for step_index,' not in source:
                    source = source.replace('for label, script, base_arguments, may_write_cards in PIPELINE_STEPS:',
                        'for step_index, (label, script, base_arguments, may_write_cards) in enumerate(PIPELINE_STEPS, 1):\n'
                        '            print(f"PIPELINE STEP {step_index}/{len(PIPELINE_STEPS)}: {label}", flush=True)')
                if 'except KeyboardInterrupt:' not in source:
                    source = source.replace('    except Exception as exc:', '''    except KeyboardInterrupt:
        return _pipeline_status(
            False, "interrupted", "Run interrupted. Inspect the saved termination status and diagnostics before rerunning; no release marker was created.",
            RUNTIME.run_results(),
        )
    except Exception as exc:''')
            if source.startswith('## 3. Run the standard pipeline') and '10 seconds' not in source:
                source += '\nEach pipeline step reports its position. Silent subprocesses emit elapsed-time heartbeats every 10 seconds; these are not completion estimates. An interrupted step stops the pipeline and creates no release-eligibility marker.\n'
            if source.startswith('## v19.1 GCP extension notes'):
                source = source.replace('Install this v19.1.1 package', 'Install the matching v19.2.3 package')
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
            if 'Run this block only when installing' in source:
                for previous in ('v19.0.2', 'v19.2.1', 'v19.2.2'):
                    source = source.replace('installing ' + previous, 'installing v19.2.3')
            if 'Production release installer was not found.' in source:
                for previous in ('v19.0.2', 'v19.2.1', 'v19.2.2'):
                    source = source.replace(previous + ' bundle', 'v19.2.3 bundle')
            if 'bundle_candidates = []' in source:
                source = source.replace('parent.glob("*v19*bundle*.zip")', 'parent.glob("nih_tracker_v19_2_3_bundle.zip")')
                for previous in ('nih_tracker_v19_2_bundle.zip', 'nih_tracker_v19_2_1_bundle.zip', 'nih_tracker_v19_2_2_bundle.zip'):
                    source = source.replace(previous, 'nih_tracker_v19_2_3_bundle.zip')
                for previous in ('19.2', '19.2.1', '19.2.2'):
                    source = source.replace(f'EXPECTED_RELEASE_VERSION = "{previous}"', 'EXPECTED_RELEASE_VERSION = "19.2.3"')
                source = source.replace('    sys.executable,\n    str(INITIALIZER),', '    sys.executable, "-u",\n    str(INITIALIZER),')
                source = source.replace('\nlog_dir = PRODUCTION_ROOT / "runs" / "bootstrap_notebook"',
                    '\npersistent_log_dir = PRODUCTION_ROOT / "runs" / "bootstrap_notebook"\nlog_dir = Path("/content/nih_tracker_install_logs")')
                source = source.replace('        bufsize=1,\n    )', '        bufsize=1,\n        start_new_session=True,\n    )')
                source = source.replace('''    with process.stdout:
        for line in process.stdout:
            print(line, end="")
            handle.write(line)
            handle.flush()
    return_code = process.wait()''', '''    try:
        with process.stdout:
            for line in process.stdout:
                print(line, end="", flush=True)
                handle.write(line)
                handle.flush()
        return_code = process.wait()
    except BaseException:
        import signal
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        print(f"Installer stopped; local log retained at {log_path}. Inspect before retrying writes.")
        raise''')
                if 'copy completed installer diagnostics' not in source:
                    source = source.replace('print(f"Return code: {return_code}")', '''# copy completed installer diagnostics once, keeping local copies on failure
try:
    persistent_log_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(log_path, persistent_log_dir / log_path.name)
    shutil.copyfile(result_path, persistent_log_dir / result_path.name)
    print(f"Saved installer diagnostics: {persistent_log_dir / result_path.name}")
except OSError as exc:
    print(f"Drive log copy failed: {exc}. Local diagnostics remain in {log_dir}.")
print(f"Return code: {return_code}")''')
                if 'PREVIEW COMPLETE: release code has NOT been installed' not in source:
                    source += '''
if not APPLY_PREFILL:
    print("PREVIEW COMPLETE: release code has NOT been installed. Review the plan, set APPLY_PREFILL=True, and rerun this cell before continuing.")
else:
    if PREFILL_MODE == "Create missing files":
        print("MISSING-FILE INSTALL COMPLETE: existing files were preserved. Run setup; if the release check fails, use Refresh release code.")
    else:
        print("INSTALLATION COMPLETE: restart the runtime, then run setup. You do not need to reinstall after restarting.")
'''
                if 'EXPECTED_RELEASE_VERSION' not in source:
                    source = source.replace('_validate_release_inventory(RELEASE_ROOT)', '''_validate_release_inventory(RELEASE_ROOT)
import json
EXPECTED_RELEASE_VERSION = "19.2.3"
installed_manifest = json.loads((RELEASE_ROOT / "RELEASE_MANIFEST.json").read_text())
if installed_manifest.get("release") != EXPECTED_RELEASE_VERSION:
    raise RuntimeError("Upload nih_tracker_v19_2_3_bundle.zip to /content; an older release cannot be installed by this notebook.")''')
            c['source'] = source.splitlines(keepends=True)
            out.append(c)
            if 'status_result = RUNTIME.run_script(' in source and '"step_maintenance.py"' in source:
                out += [cell('markdown', INTRO), cell('code', CODE)]
        nb['cells'] = out
        path.write_text(json.dumps(nb, indent=2) + '\n')


if __name__ == '__main__':
    extend()
