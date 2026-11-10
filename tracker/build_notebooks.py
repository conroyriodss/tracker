from __future__ import annotations

import hashlib
import textwrap
from pathlib import Path

import nbformat as nbf


ROOT = Path(__file__).resolve().parent


def md(text: str):
    return nbf.v4.new_markdown_cell(textwrap.dedent(text).strip() + "\n")


def code(text: str):
    cell = nbf.v4.new_code_cell(textwrap.dedent(text).strip() + "\n")
    cell["execution_count"] = None
    cell["outputs"] = []
    # Colab and Jupyter-compatible source folding. This is presentation only;
    # notebook code remains inspectable and downloadable.
    cell["metadata"] = {
        "cellView": "form",
        "jupyter": {"source_hidden": True},
        "colab": {"base_uri": "", "view-in-form": True},
    }
    return cell


def finalize(name: str, cells: list):
    notebook = nbf.v4.new_notebook()
    notebook["metadata"] = {
        "colab": {"provenance": []},
        "kernelspec": {"name": "python3", "display_name": "Python 3"},
        "language_info": {"name": "python"},
    }
    notebook["cells"] = cells
    for index, cell in enumerate(cells):
        digest = hashlib.sha256(cell["source"].encode("utf-8")).hexdigest()[:12]
        cell["id"] = f"cell-{index:02d}-{digest}"
    output = ROOT / name
    nbf.write(notebook, output)
    return output


SHARED_SETUP = r'''
ENVIRONMENT = "prod"
ENABLE_PROD_WRITES = False  # @param {type:"boolean"}

import os
import sys
from pathlib import Path

ROOTS = {"prod": "/content/drive/MyDrive/NIH_Tracker"}
os.environ["NIH_TRACKER_PRODUCTION_ONLY"] = "1"
os.environ["NIH_TRACKER_ROOT_PROD"] = ROOTS["prod"]
os.environ["NIH_TRACKER_ENV"] = "prod"
os.environ["NIH_TRACKER_SHARED_CACHE_ROOT"] = "/content/drive/MyDrive/NIH_Tracker_shared/cache"
os.environ.pop("NIH_TRACKER", None)
os.environ.pop("NIH_TRACKER_ALLOW_PROJECT_OVERRIDE", None)
os.environ.pop("NIH_TRACKER_ROOT_DEV", None)
os.environ.pop("NIH_TRACKER_ROOT_STAGING", None)

try:
    from google.colab import drive
    if not Path("/content/drive/MyDrive").exists():
        drive.mount("/content/drive")
except Exception:
    pass

SCRIPTS = Path(ROOTS["prod"]) / "scripts"
if not SCRIPTS.is_dir():
    raise FileNotFoundError(
        f"Missing {SCRIPTS}. Run the maintenance notebook's production release installer first."
    )
previous_scripts = globals().get("_NIH_TRACKER_PREVIOUS_SCRIPTS")
if previous_scripts and previous_scripts in sys.path:
    sys.path.remove(previous_scripts)
if str(SCRIPTS) in sys.path:
    sys.path.remove(str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS))
_NIH_TRACKER_PREVIOUS_SCRIPTS = str(SCRIPTS)
for module_name in list(sys.modules):
    if (
        module_name in {"common", "identity_hygiene", "interoperability_schema", "resource_profile_schema", "policy_context_schema", "repository_status_schema", "publisher_context_schema", "notebook_runtime", "review_widgets", "review_workflow", "automated_review", "verify_external_ids", "catalog_schema", "catalog_vocab", "catalog_claims", "catalog_graph", "catalog_resolver", "catalog_history", "catalog_exports"}
        or module_name.startswith("step_")
    ):
        sys.modules.pop(module_name, None)

from notebook_runtime import NotebookRuntime
RUNTIME = NotebookRuntime.create(
    notebook_name=NOTEBOOK_NAME,
    env="prod",
    enable_prod_writes=ENABLE_PROD_WRITES,
    roots=ROOTS,
)
common = RUNTIME.common
print(f"Ready: notebook={NOTEBOOK_NAME} | production | project={common.PROJECT}")
print(f"Debug session: {RUNTIME.debug_dir}")
'''


pipeline_cells = [
    md(
        """
        # NIH Tracker pipeline notebook (v19.0.2)

        This notebook performs one standard tracker refresh. It contains no review workbench and no destructive maintenance controls. Compile, input, lock, and card-parse checks always run. External API/service smoke checks and live evidence refreshes run only when the explicit external-network option is selected. A scope-aware crosswalk preflight runs before normal card mutations, and idempotent transformation steps treat zero required changes as success. Every subprocess streams to the notebook and is also retained under `runs/notebook_debug/pipeline/`.

        The tracker now has one operational environment: **production**. The final cell creates or applies a provenance-bound GitHub publication plan.
        """
    ),
    md(
        """
        ## 1. Start the production runtime

        Run this cell first. The project root is fixed at `/content/drive/MyDrive/NIH_Tracker`; there is no environment selector. Production card writes remain disabled unless the acknowledgement box is checked.
        """
    ),
    code('NOTEBOOK_NAME = "pipeline"\n' + SHARED_SETUP),
    md(
        """
        ## 2. Verify runtime state

        This cell records local runtime diagnostics only. API credentials are not loaded here. They are loaded just-in-time only when **Refresh external API/web evidence** is selected in the pipeline cell.
        """
    ),
    code(
        r'''
SECRET_STATUS = {}
DIAGNOSTICS = RUNTIME.show_diagnostics()
print("External API credentials have not been loaded. The default pipeline is offline/cached-only.")
'''
    ),
    md(
        """
        ## 3. Run the standard pipeline

        Choose **Validate/propose (no card writes)** to produce run artifacts, proposals, diagnostics, and a rebuilt preview without changing cards. Choose **Apply approved changes** to permit each step's guarded `--apply` path. The sequence is intentionally fixed; review decisions are handled in the review notebook.

        Checking `RUN_PIPELINE` always runs the local smoke suite first. The external-service smoke test is skipped unless the external-evidence checkbox is selected. A scope-aware crosswalk referential-integrity preflight then runs before the first normal card-mutating step. Rows without `registry_name` are reported as non-applicable because this join does not consume them. An unreviewed stale target remains blocking; a receipted `DEFER` is withheld from the join and carried as a nonblocking advisory. Structural conflicts and malformed in-scope rows still block. No candidate is accepted automatically.

        The fixed sequence includes interoperability, resource-profile, repository-status, publisher-policy, and policy-context builds, followed by a deterministic knowledge-graph compiler and task-oriented public site build. The interoperability step normalizes API evidence, endpoint and documentation links, standards, PID systems, and resource-specific participant GUID systems. The resource-profile step uses exact linked FAIRsharing/re3data records plus structured homepage metadata to add descriptions, resource and data-asset types, subject areas, formats, and organizational stewardship evidence. The repository-status step separates operational availability from broad, limited, paused, or closed submission intake using BMIC plus reviewed resource-specific overrides. The publisher-policy step records mandatory, recommended, preferred, fallback, example, and directory-referral relationships without collapsing them into one endorsement claim. The policy-context step uses pinned NIH CADR and dbGaP study snapshots and keeps official CADR listing, dbGaP External Data Source evidence, and public-study text associations analytically separate. It does not infer physical data residency or EDS status from study text. The v19 catalog emits typed nodes, relationships, evidence-bearing claims, controlled vocabularies, change history, and static JSON/JSONL/CSV/JSON-LD exports. Public completeness or maturity scores are not produced. Expected gate failures stop cleanly with a persisted status record instead of a notebook traceback. Valid no-op transforms pass.
        """
    ),
    code(
        r'''
RUN_PIPELINE = False  # @param {type:"boolean"}
PIPELINE_MODE = "Validate/propose (no card writes)"  # @param ["Validate/propose (no card writes)", "Apply approved changes"]
REFRESH_EXTERNAL_EVIDENCE = False  # @param {type:"boolean"}
# False (default): no SciCrunch, FAIRsharing, DataCite, re3data, or repository-homepage requests.
# True: run live service smoke checks, identifier verification/search, and cache refreshes.

from pathlib import Path
import datetime as dt
import traceback
import pandas as pd
from IPython.display import display

APPLY_CHANGES = PIPELINE_MODE == "Apply approved changes"
NETWORK_MODE = "live_external_refresh" if REFRESH_EXTERNAL_EVIDENCE else "offline_cached_only"
os.environ["NIH_TRACKER_EXTERNAL_NETWORK_ENABLED"] = "1" if REFRESH_EXTERNAL_EVIDENCE else "0"
PIPELINE_SUCCESS_PATH = RUNTIME.debug_dir / "pipeline_success.json"
PIPELINE_STATUS_PATH = RUNTIME.debug_dir / "pipeline_status.json"
PIPELINE_EXCEPTION_PATH = RUNTIME.debug_dir / "pipeline_exception.txt"
REQUIRED_INPUTS = [
    "BMIC_download.csv",
    "Tools467results.csv",
    "crosswalk.csv",
    "final_ultimate_enhanced_repos.csv",
    "repo_api_results_v4_230_strict_endpoints.csv",
    "FAIRSHARING_EXPORT.csv",
]

def _build_pipeline_steps():
    rrid_args = ["--tools", "Tools467results.csv"]
    fairsharing_args = ["--export", "FAIRSHARING_EXPORT.csv"]
    interoperability_args = ["--probe", "repo_api_results_v4_230_strict_endpoints.csv"]
    resource_profile_args = ["--fairsharing-export", "FAIRSHARING_EXPORT.csv"]
    if REFRESH_EXTERNAL_EVIDENCE:
        rrid_args.append("--verify")
        fairsharing_args.append("--verify")
        interoperability_args.extend(["--re3data-policy", "missing"])
        resource_profile_args.extend(
            ["--fairsharing-policy", "missing", "--website-policy", "missing"]
        )
    else:
        interoperability_args.extend(["--re3data-policy", "none"])
        resource_profile_args.extend(
            ["--fairsharing-policy", "none", "--website-policy", "none"]
        )

    steps = [
        (
            "crosswalk_preflight",
            "step_build_links.py",
            ["--check-crosswalk-only"],
            False,
        ),
        ("build_names", "step_build_names.py", [], True),
        ("clean_aliases", "step_clean_aliases.py", [], True),
        (
            "build_links",
            "step_build_links.py",
            ["--registry", "final_ultimate_enhanced_repos.csv"],
            True,
        ),
        ("build_rrids", "step_build_rrids.py", rrid_args, True),
        ("build_fairsharing", "step_build_fairsharing.py", fairsharing_args, True),
        (
            "reconcile_ids",
            "step_reconcile_ids.py",
            ["--probe", "repo_api_results_v4_230_strict_endpoints.csv"],
            True,
        ),
    ]
    if REFRESH_EXTERNAL_EVIDENCE:
        steps.append(
            (
                "verify_external_ids",
                "verify_external_ids.py",
                ["--mode", "search", "--only-missing", "--verbose"],
                True,
            )
        )
    steps.extend(
        [
            (
                "build_interoperability",
                "step_build_interoperability.py",
                interoperability_args,
                True,
            ),
            (
                "build_resource_profiles",
                "step_build_resource_profiles.py",
                resource_profile_args,
                True,
            ),
            (
                "build_repository_status",
                "step_build_repository_status.py",
                ["--bmic", "BMIC_download.csv"],
                True,
            ),
            ("build_policy_context", "step_build_policy_context.py", [], True),
            ("build_publisher_context", "step_build_publisher_context.py", [], True),
            ("bmic_diff", "step_bmic_diff.py", ["--bmic", "BMIC_download.csv"], False),
            (
                "refresh_plan",
                "step_refresh_plan.py",
                ["--blocks", "identity,bmic,repository_status,resource_profile,interoperability,policy_context,publisher_context,pid_culture", "--stale-months", "12"],
                False,
            ),
            (
                "build_spine",
                "step_build_spine.py",
                ["--blocks", "identity,bmic,repository_status,resource_profile,interoperability,policy_context,publisher_context,pid_culture"],
                False,
            ),
            ("build_catalog", "step_build_catalog.py", [], False),
            ("build_frontend", "step_build_frontend.py", [], False),
        ]
    )
    return steps


PIPELINE_STEPS = _build_pipeline_steps()


def _pipeline_status(ok, stage, message, results=None, **extra):
    record = {
        "schema": "nih-tracker-pipeline-status/v1",
        "session_id": RUNTIME.session_id,
        "environment": common.ENV,
        "project": str(Path(common.PROJECT).resolve()),
        "pipeline_mode": PIPELINE_MODE,
        "network_mode": NETWORK_MODE,
        "external_network_enabled": bool(REFRESH_EXTERNAL_EVIDENCE),
        "ok": bool(ok),
        "stage": str(stage),
        "message": str(message),
        "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "results": list(results or []),
        **extra,
    }
    common.atomic_write_json(PIPELINE_STATUS_PATH, record)
    banner = "PIPELINE COMPLETE" if ok else "PIPELINE STOPPED"
    print("\n" + "=" * 100)
    print(f"{banner} | stage={stage} | env={common.ENV}")
    print(message)
    print(f"Status record: {PIPELINE_STATUS_PATH}")
    if not ok:
        print("No release-eligibility marker was created.")
    print("=" * 100)
    return record


def _execute_pipeline():
    if not RUN_PIPELINE:
        print("Pipeline not started. Check RUN_PIPELINE when the selected environment is ready.")
        return {
            "ok": False,
            "stage": "not_started",
            "message": "RUN_PIPELINE is not checked.",
            "results": [],
        }

    if PIPELINE_SUCCESS_PATH.exists():
        PIPELINE_SUCCESS_PATH.unlink()
    if PIPELINE_EXCEPTION_PATH.exists():
        PIPELINE_EXCEPTION_PATH.unlink()

    results = []
    try:
        if APPLY_CHANGES and common.ENV == "prod" and not ENABLE_PROD_WRITES:
            return _pipeline_status(
                False,
                "configuration",
                "Apply mode in production requires ENABLE_PROD_WRITES in the setup cell.",
                results,
            )

        if REFRESH_EXTERNAL_EVIDENCE:
            RUNTIME.load_pipeline_secrets()
            print("External evidence refresh: ENABLED. Live API/web requests are permitted for this run.")
        else:
            RUNTIME.clear_pipeline_secrets()
            print("External evidence refresh: DISABLED. This run uses local inputs and existing caches only.")
        smoke = RUNTIME.smoke_suite(
            required_inputs=REQUIRED_INPUTS,
            required_project_files=[
                "frontend.html",
                "reference/nih_cadr_registry.csv",
                "reference/dbGaP_Studies.csv",
                "reference/dbgap_eds_definition.json",
                "reference/re3data_confirmed_mappings.csv",
                "reference/publisher_policy_sources.csv",
                "reference/publisher_repository_mentions.csv",
                "reference/repository_status_overrides.csv",
                "reference/catalog_vocabularies_v1.json",
                "schemas/catalog_node.schema.json",
                "schemas/catalog_edge.schema.json",
                "schemas/catalog_claim.schema.json",
                "schemas/data_asset.schema.json",
            ],
            external=REFRESH_EXTERNAL_EVIDENCE,
        )
        if not smoke["passed"]:
            return _pipeline_status(
                False,
                "smoke_suite",
                f"Mandatory smoke suite failed; inspect {RUNTIME.debug_dir / 'smoke_report.json'}.",
                results,
                smoke_report=str(RUNTIME.debug_dir / "smoke_report.json"),
            )

        if not common.all_cards():
            if not APPLY_CHANGES:
                return _pipeline_status(
                    False,
                    "materialize_cards",
                    "No cards are present. Seed the environment in the maintenance notebook "
                    "or run this pipeline in apply mode for initial materialization.",
                    results,
                )
            materialize = RUNTIME.run_script(
                "step_materialize_cards.py",
                ["--bmic", "BMIC_download.csv", "--attach", "interoperability"],
                label="materialize_cards",
                writes_cards=True,
            )
            results.append(materialize.to_dict())
            if not materialize.ok:
                return _pipeline_status(
                    False,
                    "materialize_cards",
                    f"Materialization failed; inspect {materialize.log_path}",
                    results,
                    failing_log=str(materialize.log_path),
                )

        for label, script, base_arguments, may_write_cards in PIPELINE_STEPS:
            arguments = list(base_arguments)
            writes_cards = bool(APPLY_CHANGES and may_write_cards)
            if writes_cards:
                arguments.append("--apply")
            result = RUNTIME.run_script(
                script,
                arguments,
                label=label,
                writes_cards=writes_cards,
                load_script_secrets=REFRESH_EXTERNAL_EVIDENCE,
            )
            results.append(result.to_dict())
            if not result.ok:
                guidance = (
                    "Open the latest crosswalk_integrity.csv in the review workbench, "
                    "record and apply evidence-based replacements/remaps or reviewed DEFER "
                    "dispositions, then rerun the full pipeline."
                    if label == "crosswalk_preflight"
                    else "Inspect the subprocess log and its run artifacts before rerunning."
                )
                return _pipeline_status(
                    False,
                    label,
                    f"Step failed or its sanity gate did not pass. {guidance}",
                    results,
                    failing_log=str(result.log_path),
                    failing_result=str(result.result_path),
                )

        build_path = Path(common.BUILD) / "index.html"
        build_sha256 = common.sha256_file(build_path)
        if not build_sha256:
            return _pipeline_status(
                False,
                "release_artifact",
                f"Pipeline steps passed but the release artifact is missing or unreadable: {build_path}",
                results,
            )

        success_record = {
            "schema": "nih-tracker-pipeline-success/v1",
            "session_id": RUNTIME.session_id,
            "environment": common.ENV,
            "project": str(Path(common.PROJECT).resolve()),
            "pipeline_mode": PIPELINE_MODE,
            "network_mode": NETWORK_MODE,
            "external_network_enabled": bool(REFRESH_EXTERNAL_EVIDENCE),
            "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "build_path": str(build_path.resolve()),
            "build_sha256": build_sha256,
            "steps": [row["label"] for row in results],
            "results": results,
        }
        common.atomic_write_json(PIPELINE_SUCCESS_PATH, success_record)
        status = _pipeline_status(
            True,
            "complete",
            "All subprocess and ledger gates passed.",
            results,
            release_eligibility_marker=str(PIPELINE_SUCCESS_PATH),
            build_path=str(build_path.resolve()),
            build_sha256=build_sha256,
        )
        print(f"Release eligibility marker: {PIPELINE_SUCCESS_PATH}")
        print(f"Built index SHA-256: {build_sha256}")
        return status
    except Exception as exc:
        common.atomic_write_text(PIPELINE_EXCEPTION_PATH, traceback.format_exc())
        return _pipeline_status(
            False,
            "unexpected_exception",
            f"{type(exc).__name__}: {exc}",
            results,
            traceback_file=str(PIPELINE_EXCEPTION_PATH),
        )


PIPELINE_STATUS = _execute_pipeline()
if PIPELINE_STATUS.get("results"):
    result_frame = pd.DataFrame(PIPELINE_STATUS["results"])
    columns = [
        column
        for column in [
            "label",
            "ok",
            "returncode",
            "gate_passed",
            "duration_seconds",
            "log_path",
        ]
        if column in result_frame.columns
    ]
    display(result_frame[columns])
RUNTIME.write_session_summary()
'''
    ),
    md(
        """
        ## 4. Inspect the evidence produced by this session

        This cell shows recent ledger rows, unresolved issues ordered by severity and recurrence, and the persisted debug result records. It does not rerun any pipeline step.
        """
    ),
    code(
        r'''
import pandas as pd
from IPython.display import display

recent_ledger = RUNTIME.recent_ledger(20)
open_issues = RUNTIME.open_issues(250)
run_results = RUNTIME.run_results()

print(f"Debug directory: {RUNTIME.debug_dir}")
print(f"Recent ledger rows: {len(recent_ledger)}")
if recent_ledger:
    display(pd.DataFrame(recent_ledger))
print(f"Open issues: {len(open_issues)}")
if open_issues:
    display(pd.DataFrame(open_issues))
print(f"Subprocesses in this notebook session: {len(run_results)}")
if run_results:
    display(pd.DataFrame(run_results))
RUNTIME.write_session_summary()
'''
    ),
    md(
        """
        ## 5. Publish production

        This is the final operational cell. **Create a sealed release plan** is enabled only after the fixed production pipeline completes successfully in the current session and captures hash-pinned bytes plus pipeline provenance for review. If the session is not release-eligible, the cell prints the blocking pipeline stage and exits without a traceback. **Apply the newest unapplied plan after review** selects only a plan carrying verified pipeline provenance; when none exists, it writes `release_status.json`, explains that a plan must be created first, and exits without changing GitHub.

        Production GitHub planning reads `GITHUB_REPOSITORY`; publication also requires `GITHUB_TOKEN`. Set `GITHUB_PUBLISH_DIR` to `docs`; GitHub Pages must be configured for the selected branch and `/docs`. Optional identity settings are `GITHUB_COMMIT_NAME` and `GITHUB_COMMIT_EMAIL`.
        """
    ),
    code(
        r'''
RELEASE_ACTION = "Create a sealed release plan"  # @param ["Create a sealed release plan", "Apply the newest unapplied plan after review"]

import datetime as dt
import hashlib
import importlib
import json
from pathlib import Path
import pandas as pd
from IPython.display import display

APPLY_RELEASE = RELEASE_ACTION == "Apply the newest unapplied plan after review"
PIPELINE_SUCCESS_PATH = RUNTIME.debug_dir / "pipeline_success.json"
RELEASE_STATUS_PATH = RUNTIME.debug_dir / "release_status.json"
RELEASE_PROVENANCE_NAME = "pipeline_release_provenance.json"
RELEASE_PROVENANCE_SEAL = "pipeline_release_provenance.sha256"


def _canonical_sha256(value):
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_current_pipeline_success():
    if not PIPELINE_SUCCESS_PATH.is_file():
        raise RuntimeError(
            "Release planning is disabled because this notebook session has no successful "
            "end-to-end pipeline marker. Run the standard pipeline to completion first."
        )
    success = json.loads(PIPELINE_SUCCESS_PATH.read_text(encoding="utf-8"))
    if success.get("schema") != "nih-tracker-pipeline-success/v1":
        raise RuntimeError("Unsupported pipeline success marker schema.")
    if success.get("session_id") != RUNTIME.session_id:
        raise RuntimeError("Pipeline success marker belongs to a different notebook session.")
    if success.get("environment") != common.ENV:
        raise RuntimeError("Pipeline success marker belongs to a different environment.")
    if Path(success.get("project", "")).resolve() != Path(common.PROJECT).resolve():
        raise RuntimeError("Pipeline success marker belongs to a different project root.")
    expected_steps = [label for label, _, _, _ in PIPELINE_STEPS]
    observed_steps = success.get("steps")
    permitted_sequences = [expected_steps, ["materialize_cards", *expected_steps]]
    if observed_steps not in permitted_sequences:
        raise RuntimeError(
            "Pipeline success marker does not contain the complete fixed pipeline sequence."
        )
    build_path = Path(success.get("build_path", "")).resolve()
    expected_build = (Path(common.BUILD) / "index.html").resolve()
    if build_path != expected_build:
        raise RuntimeError("Pipeline success marker points to an unexpected build artifact.")
    current_hash = common.sha256_file(build_path)
    if not current_hash or current_hash != success.get("build_sha256"):
        raise RuntimeError(
            "build/index.html changed after the successful pipeline run. Rerun the full "
            "pipeline before creating a release plan."
        )
    return success


def _seal_release_provenance(plan_path, success):
    plan_path = Path(plan_path).resolve()
    record = {
        "schema": "nih-tracker-pipeline-release-provenance/v1",
        "environment": common.ENV,
        "project": str(Path(common.PROJECT).resolve()),
        "release_plan": str(plan_path),
        "release_plan_sha256": common.sha256_file(plan_path),
        "pipeline_success": success,
        "pipeline_success_sha256": _canonical_sha256(success),
    }
    provenance_path = plan_path.parent / RELEASE_PROVENANCE_NAME
    common.atomic_write_json(provenance_path, record)
    seal_path = plan_path.parent / RELEASE_PROVENANCE_SEAL
    common.atomic_write_text(seal_path, common.sha256_file(provenance_path) + "\n")
    print(f"Release provenance: {provenance_path}")
    print(f"Release provenance seal: {seal_path}")


def _verify_release_provenance(plan_path):
    plan_path = Path(plan_path).resolve()
    provenance_path = plan_path.parent / RELEASE_PROVENANCE_NAME
    seal_path = plan_path.parent / RELEASE_PROVENANCE_SEAL
    if not provenance_path.is_file() or not seal_path.is_file():
        return False, "missing pipeline release provenance or provenance seal"
    try:
        expected_provenance_hash = seal_path.read_text(encoding="utf-8").strip()
        if expected_provenance_hash != common.sha256_file(provenance_path):
            return False, "pipeline release provenance seal mismatch"
        record = json.loads(provenance_path.read_text(encoding="utf-8"))
        success = record.get("pipeline_success", {})
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        expected_steps = [label for label, _, _, _ in PIPELINE_STEPS]
        observed_steps = success.get("steps")
        permitted_sequences = [expected_steps, ["materialize_cards", *expected_steps]]
        result_rows = success.get("results", [])
        result_labels = [row.get("label") for row in result_rows]
        result_ok = bool(result_rows) and all(row.get("ok") is True for row in result_rows)
        checks = [
            record.get("schema") == "nih-tracker-pipeline-release-provenance/v1",
            record.get("environment") == common.ENV,
            Path(record.get("project", "")).resolve() == Path(common.PROJECT).resolve(),
            Path(record.get("release_plan", "")).resolve() == plan_path,
            record.get("release_plan_sha256") == common.sha256_file(plan_path),
            record.get("pipeline_success_sha256") == _canonical_sha256(success),
            success.get("schema") == "nih-tracker-pipeline-success/v1",
            success.get("environment") == common.ENV,
            Path(success.get("project", "")).resolve() == Path(common.PROJECT).resolve(),
            observed_steps in permitted_sequences,
            result_labels == observed_steps,
            result_ok,
        ]
        if plan.get("schema") == "nih-tracker-github-publication/v1":
            checks.extend(
                [
                    Path(plan.get("source", "")).resolve()
                    == Path(success.get("build_path", "")).resolve(),
                    plan.get("source_sha256") == success.get("build_sha256"),
                ]
            )
        if not all(checks):
            return False, "pipeline release provenance failed hash, scope, sequence, or build validation"
    except Exception as exc:
        return False, f"invalid pipeline release provenance: {exc}"
    return True, ""


def _find_newest_eligible(candidates, applied_marker):
    rejected = []
    for candidate in candidates:
        applied_path = candidate.parent / applied_marker
        if applied_path.exists():
            rejected.append(
                {
                    "plan": str(candidate),
                    "reason": f"already applied ({applied_path.name})",
                }
            )
            continue
        eligible, reason = _verify_release_provenance(candidate)
        if eligible:
            return candidate, rejected
        rejected.append({"plan": str(candidate), "reason": reason})
    return None, rejected


def _write_release_status(state, message, *, details=None, rejected=None, plan_path=None):
    record = {
        "schema": "nih-tracker-notebook-release-status/v1",
        "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "session_id": RUNTIME.session_id,
        "environment": common.ENV,
        "project": str(Path(common.PROJECT).resolve()),
        "requested_action": RELEASE_ACTION,
        "state": state,
        "message": message,
        "plan_path": str(Path(plan_path).resolve()) if plan_path else "",
        "details": details or {},
        "rejected_candidates": rejected or [],
    }
    common.atomic_write_json(RELEASE_STATUS_PATH, record)
    return RELEASE_STATUS_PATH


def _print_release_block(heading, message, *, next_steps, details=None, rejected=None):
    status_path = _write_release_status(
        heading.lower().replace(" ", "_"),
        message,
        details=details,
        rejected=rejected,
    )
    print("\n" + "=" * 100)
    print(heading)
    print(message)
    if details:
        for key, value in details.items():
            if value not in (None, "", [], {}):
                print(f"{key}: {value}")
    if rejected:
        print("Rejected or unavailable plans:")
        for item in rejected[:8]:
            print(f"  - {item['plan']}: {item['reason']}")
        if len(rejected) > 8:
            print(f"  ... {len(rejected) - 8} additional candidate(s) recorded in {status_path}")
    print("Next steps:")
    for index, step in enumerate(next_steps, start=1):
        print(f"  {index}. {step}")
    print(f"Status record: {status_path}")
    print("No repository or environment was changed.")
    print("=" * 100)


pipeline_success = None
RELEASE_ELIGIBLE = True
if not APPLY_RELEASE:
    try:
        pipeline_success = _load_current_pipeline_success()
    except Exception as exc:
        RELEASE_ELIGIBLE = False
        status_path = RUNTIME.debug_dir / "pipeline_status.json"
        pipeline_status = {}
        if status_path.is_file():
            try:
                pipeline_status = json.loads(status_path.read_text(encoding="utf-8"))
            except Exception:
                pipeline_status = {}
        details = {
            "pipeline_stage": pipeline_status.get("stage", "") if pipeline_status else "",
            "pipeline_status": pipeline_status.get("message", "") if pipeline_status else "",
            "pipeline_status_record": str(status_path) if status_path.is_file() else "",
        }
        _print_release_block(
            "RELEASE NOT ELIGIBLE",
            str(exc),
            details=details,
            next_steps=[
                "Run the standard pipeline to completion in this notebook session.",
                "Confirm that pipeline_success.json was created and build/index.html is unchanged.",
                "Run this cell with 'Create a sealed release plan'.",
            ],
        )
        print("No promotion or GitHub publication plan was created.")

if RELEASE_ELIGIBLE:
    if not APPLY_RELEASE:
        RUNTIME.load_secrets(
            [
                "GITHUB_REPOSITORY",
                "GITHUB_BRANCH",
                "GITHUB_PUBLISH_DIR",
                "GITHUB_COMMIT_NAME",
                "GITHUB_COMMIT_EMAIL",
            ],
            required=["GITHUB_REPOSITORY"],
            quiet=False,
        )
        before = set(
            (Path(common.RUNS) / "publish_github").glob("*/publication_plan.json")
        )
        result = RUNTIME.run_script(
            "step_publish_github.py",
            [],
            label="publish_production_to_github",
            required_secrets=["GITHUB_REPOSITORY"],
        )
        if not result.ok:
            raise RuntimeError(f"GitHub publication planning failed; inspect {result.log_path}")
        after = set(
            (Path(common.RUNS) / "publish_github").glob("*/publication_plan.json")
        )
        created = sorted(
            after.difference(before),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if len(created) != 1:
            raise RuntimeError(
                f"Expected exactly one new publication plan; found {len(created)}."
            )
        plan_path = created[0]
        _seal_release_provenance(plan_path, pipeline_success)
        print("Publication plan created. Review it before applying.")
        _write_release_status(
            "plan_created",
            "Created sealed GitHub publication plan.",
            plan_path=plan_path,
        )
    else:
        plans = sorted(
            (Path(common.RUNS) / "publish_github").glob("*/publication_plan.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        plan_path, rejected = _find_newest_eligible(plans, "publication_receipt.json")
        if plan_path is None:
            _print_release_block(
                "NO ELIGIBLE RELEASE PLAN",
                "No unapplied GitHub publication plan with verified end-to-end pipeline provenance was found. "
                "The older plans listed below predate the provenance control and cannot be applied.",
                rejected=rejected,
                next_steps=[
                    "Run the fixed production pipeline to completion if the current build is not already release-eligible.",
                    "Set RELEASE_ACTION to 'Create a sealed release plan' and run this cell once.",
                    "Review publication_plan.json, pipeline_release_provenance.json, and both SHA-256 seals.",
                    "Set RELEASE_ACTION to 'Apply the newest unapplied plan after review' and rerun.",
                ],
            )
        else:
            RUNTIME.load_secrets(
                [
                    "GITHUB_TOKEN",
                    "GITHUB_REPOSITORY",
                    "GITHUB_BRANCH",
                    "GITHUB_PUBLISH_DIR",
                    "GITHUB_COMMIT_NAME",
                    "GITHUB_COMMIT_EMAIL",
                ],
                required=["GITHUB_TOKEN"],
                quiet=False,
            )
            print(f"Selected verified publication plan: {plan_path}")
            result = RUNTIME.run_script(
                "step_publish_github.py",
                [
                    "--plan",
                    str(plan_path),
                    "--apply",
                    "--confirm",
                    "PUBLISH_PROD_TO_GITHUB",
                ],
                label="publish_production_to_github",
                required_secrets=["GITHUB_TOKEN"],
            )
            if not result.ok:
                raise RuntimeError(f"GitHub publication failed; inspect {result.log_path}")
            _write_release_status(
                "publication_applied",
                "Applied verified GitHub publication plan.",
                plan_path=plan_path,
                details={"debug_log": str(result.log_path)},
            )

RUNTIME.write_session_summary()
'''
    ),
]



def build_guided_review_cells():
    return [
        md(
            """
            # NIH Tracker guided review workbench (v19.0.2)

            The normal workflow is now **Review → Approve → Finalize**. The notebook shows only the next material action and skips decision families that have no pending work. Evidence-gap inventories and historical queues are available under **Advanced diagnostics**; they are not mixed into the operational review path and do not block publication.

            Model output remains a recommendation. A named human reviewer must approve a recommendation before it can enter an operational adjudication file. Existing card, identifier, and **Crosswalk references** controls, locking, snapshots, and rollback safeguards remain in force.
            """
        ),
        md(
            """
            ## 1. Start the workbench

            The workbench is production-only. Provide the reviewer identity through the `NIH_TRACKER_REVIEWER` Colab secret. Enable production writes only for the final **Apply all reviewed decisions** action.
            """
        ),
        code('NOTEBOOK_NAME = "review_workbench"\n' + SHARED_SETUP),
        md(
            """
            ### Reviewer identity and guided controls

            This cell loads the named reviewer and initializes the compact guided controls. No provider API is called.
            """
        ),
        code(
            r'''
import json
import os
from pathlib import Path

import ipywidgets as widgets
import pandas as pd
from IPython.display import HTML, clear_output, display

from automated_review import (
    EXPORT_RUN_ROOT,
    UPLOAD_RUN_ROOT,
    launch_ai_recommendation_triage,
    load_approvals,
    load_recommendations,
)
from review_widgets import launch_review_workbench
from review_workflow import (
    compute_review_status,
    pending_recommendations_for_human_approval,
)

RUNTIME.load_secrets(["NIH_TRACKER_REVIEWER"], quiet=True)
REVIEWER = os.getenv("NIH_TRACKER_REVIEWER", "").strip()
print("Reviewer identity:", REVIEWER if REVIEWER else "MISSING")
if not REVIEWER:
    print("Add NIH_TRACKER_REVIEWER to Colab secrets before saving approvals or decisions.")
'''
        ),
        md(
            """
            ## 2. Review dashboard

            Refresh this dashboard after importing recommendations, saving approvals, or finalizing decisions. A green **COMPLETE** state means the review notebook is finished and the main pipeline is the next step. Evidence backlogs are reported separately and do not change this state. Queue labels use `pending | decided`; when all rows are decided, the dashboard states **No pending rows.**
            """
        ),
        code(
            r'''
review_status_output = widgets.Output()
review_refresh_button = widgets.Button(
    description="Refresh review status",
    button_style="info",
    icon="refresh",
)
GUIDED_REVIEW_STATUS = None


def _status_color(state):
    return {
        "COMPLETE": "#137333",
        "REVIEW_REQUIRED": "#b06000",
        "APPROVAL_REQUIRED": "#b06000",
        "READY_TO_FINALIZE": "#8a3ffc",
    }.get(state, "#5f6368")


def refresh_guided_status(_button=None):
    global GUIDED_REVIEW_STATUS
    GUIDED_REVIEW_STATUS = compute_review_status(include_evidence=True)
    status = GUIDED_REVIEW_STATUS
    rec = status["recommendations"]
    ops = status["operational_adjudications"]
    queue_rows = "".join(
        f"<tr><td>{item['label']}</td><td>{item['pending_rows']}</td>"
        f"<td>{item['decided_rows']}</td><td><code>{item['run']}</code></td></tr>"
        for item in status["queues"]
    ) or "<tr><td colspan='4'>No current review queue found.</td></tr>"
    evidence = ", ".join(
        f"{item['domain']}: {item['rows']}"
        for item in status.get("non_gating_evidence_backlogs", [])
    ) or "none"
    covered = int(
        rec.get("recommendation_items_covered_by_current_decision", 0) or 0
    )
    html = f"""
    <div style='border:1px solid #dadce0;border-radius:8px;padding:14px'>
      <div style='font-size:20px;font-weight:700;color:{_status_color(status['state'])}'>
        {status['state']}
      </div>
      <div style='margin:6px 0 12px 0'>{status['next_action']}</div>
      <table style='border-collapse:collapse'>
        <tr><th style='text-align:left;padding-right:24px'>Imported items awaiting approval</th><td>{rec['review_items_awaiting_human_approval']}</td></tr>
        <tr><th style='text-align:left;padding-right:24px'>Later recommendations already covered by current decisions</th><td>{covered}</td></tr>
        <tr><th style='text-align:left;padding-right:24px'>Approved recommendations awaiting promotion</th><td>{rec['approved_recommendations_pending_promotion']}</td></tr>
        <tr><th style='text-align:left;padding-right:24px'>Identifier adjudications awaiting application</th><td>{ops['identifier']}</td></tr>
        <tr><th style='text-align:left;padding-right:24px'>Crosswalk adjudications awaiting application</th><td>{ops['crosswalk']}</td></tr>
        <tr><th style='text-align:left;padding-right:24px'>Card/name adjudications awaiting application</th><td>{ops['card_name']}</td></tr>
      </table>
      <h4 style='margin-bottom:4px'>Current queues</h4>
      <table style='border-collapse:collapse;width:100%'>
        <tr><th align='left'>Family</th><th align='left'>Pending</th><th align='left'>Decided</th><th align='left'>Source</th></tr>
        {queue_rows}
      </table>
      <div style='margin-top:10px;color:#5f6368'>Non-gating evidence backlogs: {evidence}</div>
    </div>
    """
    with review_status_output:
        clear_output(wait=True)
        display(HTML(html))
    callback = globals().get("_refresh_guided_controls")
    if callable(callback):
        callback()
    return status


review_refresh_button.on_click(refresh_guided_status)
display(widgets.VBox([review_refresh_button, review_status_output]))
refresh_guided_status()
'''
        ),
        md(
            """
            ## 3. Follow the current review action

            This panel is state-aware. It shows recommendation approval when imported items need a human decision, candidate-queue controls when new rows need review, and a direct instruction to continue to finalization when only operational adjudications remain. Empty queue dropdowns are hidden.
            """
        ),
        code(
            r'''
import datetime as dt
import shutil

manual_queue_widget = widgets.Dropdown(
    options=[],
    description="Manual queue",
    layout=widgets.Layout(width="1050px"),
)
ai_queue_widget = widgets.Dropdown(
    options=[],
    description="AI batch queue",
    layout=widgets.Layout(width="1050px"),
)
review_method_widget = widgets.Dropdown(
    options=[("ChatGPT web package", "chatgpt"), ("Perplexity web package", "perplexity")],
    value="chatgpt",
    description="Package",
    layout=widgets.Layout(width="380px"),
)
batch_size_widget = widgets.Dropdown(
    options=[10, 15, 20, 25, 40, 50],
    value=20,
    description="Items/batch",
    layout=widgets.Layout(width="240px"),
)
open_manual_button = widgets.Button(
    description="Open manual review",
    icon="edit",
    button_style="info",
)
export_button = widgets.Button(
    description="Create review batches",
    icon="archive",
    button_style="primary",
)
download_choice = widgets.Dropdown(
    options=[],
    description="Batch",
    disabled=True,
    layout=widgets.Layout(width="760px"),
)
download_button = widgets.Button(
    description="Download selected batch",
    icon="download",
    disabled=True,
)
saved_export_widget = widgets.Dropdown(
    options=[],
    description="Saved export",
    layout=widgets.Layout(width="760px"),
)
restore_export_button = widgets.Button(
    description="Restore download files after restart",
    icon="refresh",
)
review_action_output = widgets.Output()
guided_next_action_html = widgets.HTML()
open_triage_button = widgets.Button(
    description="Open recommendations for approval",
    icon="tasks",
    button_style="success",
)

AI_EXPORTABLE_FAMILIES = {"identifier", "rrid", "fairsharing", "crosswalk", "card"}


def _queue_options(*, ai_exportable_only=False):
    if not GUIDED_REVIEW_STATUS:
        return []
    rows = []
    for item in GUIDED_REVIEW_STATUS["queues"]:
        if int(item["pending_rows"]) <= 0:
            continue
        if ai_exportable_only and item["family"] not in AI_EXPORTABLE_FAMILIES:
            continue
        rows.append((
            f"{item['label']} | {item['pending_rows']} pending | {item['run']}",
            item["source_file"],
        ))
    return rows


def _set_widget_options(widget, options):
    previous = widget.value
    widget.options = options
    widget.disabled = not bool(options)
    values = {value for _, value in options}
    if previous in values:
        widget.value = previous
    elif options:
        widget.value = options[0][1]


def _refresh_guided_controls():
    manual_options = _queue_options(ai_exportable_only=False)
    ai_options = _queue_options(ai_exportable_only=True)
    _set_widget_options(manual_queue_widget, manual_options)
    _set_widget_options(ai_queue_widget, ai_options)
    open_manual_button.disabled = not bool(manual_options) or not bool(REVIEWER)
    export_button.disabled = not bool(ai_options)

    status = GUIDED_REVIEW_STATUS or {}
    state = status.get("state", "")
    rec = status.get("recommendations", {})
    pending_approval = int(rec.get("review_items_awaiting_human_approval", 0) or 0)
    pending_finalize = (
        int(rec.get("approved_recommendations_pending_promotion", 0) or 0)
        + int(status.get("operational_pending_rows", 0) or 0)
    )

    open_triage_button.disabled = pending_approval == 0 or not bool(REVIEWER)
    open_triage_button.description = (
        f"Open {pending_approval} recommendation"
        f"{'s' if pending_approval != 1 else ''} for approval"
    )
    open_triage_button.layout.display = "" if pending_approval else "none"

    show_candidate_controls = state == "REVIEW_REQUIRED" and bool(
        manual_options or ai_options
    )
    if "candidate_controls_box" in globals():
        candidate_controls_box.layout.display = (
            "" if show_candidate_controls else "none"
        )

    if state == "APPROVAL_REQUIRED":
        guided_next_action_html.value = (
            "<div style='border-left:4px solid #b06000;padding:10px 12px'>"
            f"<b>Next:</b> inspect and approve {pending_approval} imported "
            "recommendation item(s). Candidate queues are already decided, so "
            "no queue dropdown is shown.</div>"
        )
    elif state == "READY_TO_FINALIZE":
        guided_next_action_html.value = (
            "<div style='border-left:4px solid #8a3ffc;padding:10px 12px'>"
            f"<b>Next:</b> continue to Finalize all reviewed decisions. "
            f"{pending_finalize} reviewed decision row(s) remain.</div>"
        )
    elif state == "REVIEW_REQUIRED":
        guided_next_action_html.value = (
            "<div style='border-left:4px solid #b06000;padding:10px 12px'>"
            "<b>Next:</b> review the pending queue shown below.</div>"
        )
    elif state == "COMPLETE":
        guided_next_action_html.value = (
            "<div style='border-left:4px solid #137333;padding:10px 12px'>"
            "<b>Review complete.</b> Continue to the main pipeline.</div>"
        )
    else:
        guided_next_action_html.value = ""

    if "finalize_button" in globals():
        finalize_button.disabled = pending_approval > 0 or pending_finalize == 0


def _open_pending_triage(_button):
    with review_action_output:
        clear_output(wait=True)
        if not REVIEWER:
            print("Reviewer identity is missing.")
            return
        recommendations = load_recommendations()
        if recommendations.empty:
            print("No imported recommendations were found.")
            return
        approvals = load_approvals()
        pending = pending_recommendations_for_human_approval(
            recommendations, approvals
        )
        if pending.empty:
            print(
                "No imported review item requires approval. "
                "A current human adjudication may already cover the item."
            )
            refresh_guided_status()
            return
        print(f"Opening {pending['review_key'].nunique()} review item(s) awaiting approval.")
        globals()["GUIDED_AI_TRIAGE"] = launch_ai_recommendation_triage(
            recommendations=pending,
            reviewer=REVIEWER,
            page_size=10,
        )


def _open_manual(_button):
    with review_action_output:
        clear_output(wait=True)
        if not manual_queue_widget.value:
            print("No pending manual-review queue is selected.")
            return
        globals()["GUIDED_MANUAL_REVIEW"] = launch_review_workbench(
            manual_queue_widget.value,
            reviewer=REVIEWER,
            page_size=10,
            include_decided=False,
        )


def _saved_export_reports():
    return sorted(
        EXPORT_RUN_ROOT.glob("*/export_report.json"),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )


def _refresh_saved_exports(select_path=None):
    options = []
    for path in _saved_export_reports():
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if int(report.get("item_count", 0) or 0) <= 0:
            continue
        options.append((
            f"{report.get('bundle_id', path.parent.name)} | {report.get('item_count', 0)} items",
            str(path),
        ))
    saved_export_widget.options = options
    saved_export_widget.disabled = not bool(options)
    restore_export_button.disabled = not bool(options)
    if select_path and str(select_path) in {value for _, value in options}:
        saved_export_widget.value = str(select_path)


def _restore_export(_button):
    with review_action_output:
        clear_output(wait=True)
        if not saved_export_widget.value:
            print("No saved export is selected.")
            return
        report = _activate_downloads(saved_export_widget.value)
        print(f"Restored {report.get('batch_count', 0)} batch(es) from Drive.")


def _new_export_report(previous_paths):
    reports = sorted(
        EXPORT_RUN_ROOT.glob("*/export_report.json"),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    return next((path for path in reports if str(path.resolve()) not in previous_paths), None)


def _activate_downloads(report_path):
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    options = []
    for entry in report.get("download_files", []):
        path = Path(entry.get("path", ""))
        if path.is_file() and "All batches" not in entry.get("label", ""):
            options.append((f"{entry['label']} | {path.stat().st_size / 1024:.0f} KiB", str(path)))
    if not options:
        master = Path(report.get("master_zip", ""))
        if master.is_file():
            options = [(f"Complete package | {master.stat().st_size / 1024:.0f} KiB", str(master))]
    download_choice.options = options
    download_choice.disabled = not bool(options)
    download_button.disabled = not bool(options)
    if options:
        download_choice.value = options[0][1]
    return report


def _export_batches(_button):
    with review_action_output:
        clear_output(wait=True)
        if not ai_queue_widget.value:
            print("No pending card/name, identifier or crosswalk queue is available for AI batch export.")
            return
        before = {str(path.resolve()) for path in EXPORT_RUN_ROOT.glob("*/export_report.json")}
        result = RUNTIME.run_script(
            "step_export_ai_review.py",
            [
                "--source", str(ai_queue_widget.value),
                "--provider", review_method_widget.value,
                "--batch-size", str(batch_size_widget.value),
            ],
            label="export_guided_review_package",
            writes_cards=False,
        )
        if not result.ok:
            print(f"Export failed; inspect {result.log_path}")
            return
        report_path = _new_export_report(before)
        if report_path is None:
            print("No new package was created. The selected queue may already be fully decided.")
            refresh_guided_status()
            return
        report = _activate_downloads(report_path)
        _refresh_saved_exports(report_path)
        print(f"Created {report.get('batch_count', 0)} batch(es) for {report.get('item_count', 0)} item(s).")
        print(f"Durable export: {report_path.parent}")
        print("Download one batch, review it in the selected web service, then import the returned JSON below.")


def _download_batch(_button):
    path = Path(download_choice.value or "")
    if not path.is_file():
        with review_action_output:
            print(f"Missing batch file: {path}")
        return
    try:
        from google.colab import files
        files.download(str(path))
    except Exception as exc:
        with review_action_output:
            print(f"Browser download could not start: {exc}")
            print(f"Use the Colab Files pane: {path}")


open_triage_button.on_click(_open_pending_triage)
open_manual_button.on_click(_open_manual)
export_button.on_click(_export_batches)
download_button.on_click(_download_batch)
restore_export_button.on_click(_restore_export)
_refresh_saved_exports()

candidate_controls_box = widgets.VBox(
    [
        widgets.HTML("<b>Manual review</b> — all pending queue families"),
        widgets.HBox([manual_queue_widget, open_manual_button]),
        widgets.HTML(
            "<b>File-exchange batches</b> — card/name, identifier and crosswalk candidates"
        ),
        ai_queue_widget,
        widgets.HBox([review_method_widget, batch_size_widget, export_button]),
        widgets.HBox([saved_export_widget, restore_export_button]),
        widgets.HBox([download_choice, download_button]),
    ]
)

display(
    widgets.VBox(
        [
            guided_next_action_html,
            open_triage_button,
            candidate_controls_box,
            review_action_output,
        ]
    )
)
_refresh_guided_controls()
'''
        ),
        md(
            """
            ## 4. Import returned files when needed

            Use this section only after completing a ChatGPT/Perplexity web batch. **Import returned files** saves selected JSON/JSONL/ZIP files to Drive before validation and runs the strict source/hash/target checks. The state-aware panel above then exposes the human-approval action. Importing does not change cards.
            """
        ),
        code(
            r'''
import_button = widgets.Button(
    description="Import returned files",
    icon="upload",
    button_style="primary",
)
saved_upload_widget = widgets.Dropdown(
    options=[],
    description="Saved uploads",
    layout=widgets.Layout(width="760px"),
)
refresh_uploads_button = widgets.Button(
    description="Refresh saved uploads",
    icon="refresh",
)
import_saved_button = widgets.Button(
    description="Import selected saved upload",
    icon="check",
)
import_triage_output = widgets.Output()


def _refresh_persisted_uploads(_button=None, select_path=None):
    options = []
    if UPLOAD_RUN_ROOT.is_dir():
        for directory in sorted(
            (path for path in UPLOAD_RUN_ROOT.iterdir() if path.is_dir()),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        ):
            files_found = [
                path for path in directory.iterdir()
                if path.is_file() and path.suffix.lower() in {".json", ".jsonl", ".zip"}
            ]
            if files_found:
                options.append((f"{directory.name} | {len(files_found)} file(s)", str(directory)))
    saved_upload_widget.options = options
    saved_upload_widget.disabled = not bool(options)
    import_saved_button.disabled = not bool(options)
    if select_path and str(select_path) in {value for _, value in options}:
        saved_upload_widget.value = str(select_path)


def _persist_uploads(uploaded):
    import hashlib
    signature = hashlib.sha256()
    for name, payload in sorted(uploaded.items()):
        signature.update(Path(name).name.encode("utf-8"))
        signature.update(hashlib.sha256(payload).digest())
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = UPLOAD_RUN_ROOT / f"{stamp}-{signature.hexdigest()[:12]}"
    destination.mkdir(parents=True, exist_ok=False)
    paths = []
    for name, payload in uploaded.items():
        path = destination / Path(name).name
        common.atomic_write_bytes(path, payload)
        paths.append(path)
    return destination, paths


def _import_paths(paths, destination):
    failures = 0
    for index, path in enumerate(paths, start=1):
        print(f"[{index}/{len(paths)}] {path.name}")
        result = RUNTIME.run_script(
            "step_import_ai_review.py",
            ["--response", str(path), "--require-complete-batches"],
            label=f"import_guided_review_{path.name}",
            writes_cards=False,
        )
        failures += int(not result.ok)
    if failures:
        print(f"{failures} file(s) failed validation. Originals remain at {destination}.")
    else:
        print("All selected files passed validation and entered the recommendation quarantine.")
    refresh_guided_status()


def _import_saved(_button):
    with import_triage_output:
        clear_output(wait=True)
        if not saved_upload_widget.value:
            print("No saved upload set is selected.")
            return
        destination = Path(saved_upload_widget.value)
        paths = sorted(
            path for path in destination.iterdir()
            if path.is_file() and path.suffix.lower() in {".json", ".jsonl", ".zip"}
        )
        _import_paths(paths, destination)


def _import_returned(_button):
    with import_triage_output:
        clear_output(wait=True)
        try:
            from google.colab import files
            selected = files.upload()
        except Exception as exc:
            print(f"Browser upload is unavailable: {exc}")
            return
        if not selected:
            print("No file selected.")
            return
        uploaded = {name: bytes(payload) for name, payload in selected.items()}
        destination, paths = _persist_uploads(uploaded)
        print(f"Selected and saved to Drive: {destination}")
        _refresh_persisted_uploads(select_path=destination)
        _import_paths(paths, destination)



import_button.on_click(_import_returned)
refresh_uploads_button.on_click(_refresh_persisted_uploads)
import_saved_button.on_click(_import_saved)
_refresh_persisted_uploads()
display(
    widgets.VBox(
        [
            widgets.HBox([import_button]),
            widgets.HBox([saved_upload_widget, refresh_uploads_button, import_saved_button]),
            import_triage_output,
        ]
    )
)
_refresh_guided_controls()
'''
        ),
        md(
            """
            ## 5. Finalize all reviewed decisions

            This is the only normal application control. **Validate** previews every pending reviewed family. **Apply all** promotes approved AI recommendations, revalidates the materialized operational decisions, and applies only nonempty families. Empty card/name, crosswalk, and identifier families are skipped rather than launched as repeated no-op subprocesses.
            """
        ),
        code(
            r'''
finalize_action_widget = widgets.ToggleButtons(
    options=[("Validate", "validate"), ("Apply all", "apply")],
    value="validate",
    description="Action",
)
finalize_button = widgets.Button(
    description="Run guided finalization",
    button_style="warning",
    icon="check-circle",
)
finalize_output = widgets.Output()


def _run_finalize(_button):
    finalize_button.disabled = True
    try:
      with finalize_output:
        clear_output(wait=True)
        status = GUIDED_REVIEW_STATUS or {}
        pending_approval = int(status.get("recommendations", {}).get("review_items_awaiting_human_approval", 0) or 0)
        if pending_approval:
            print(f"Finalization is disabled: {pending_approval} review item(s) still require human approval.")
            return
        action = finalize_action_widget.value
        if action == "apply" and common.ENV == "prod" and not ENABLE_PROD_WRITES:
            print("Production application requires ENABLE_PROD_WRITES in the setup cell.")
            return
        args = ["--action", action]
        if action == "apply":
            args.extend(["--confirm", "FINALIZE_REVIEW_WORKFLOW"])
        result = RUNTIME.run_script(
            "step_review_workflow.py",
            args,
            label=f"guided_review_{action}",
            writes_cards=action == "apply",
        )
        if result.ok:
            print("Guided review workflow completed.")
        else:
            print(f"Guided review workflow stopped; inspect {result.log_path}")
        refresh_guided_status()
    finally:
        _refresh_guided_controls()


finalize_button.on_click(_run_finalize)
display(widgets.VBox([finalize_action_widget, finalize_button, finalize_output]))
_refresh_guided_controls()
'''
        ),
        md(
            """
            ## 6. Advanced diagnostics (optional)

            Open this section only for historical queues, targeted family-level troubleshooting, or evidence-curation planning. Evidence gaps are descriptive backlogs—not adjudication queues—and are summarized without automatically rendering thousand-row tables. The current files are `interoperability_gaps.csv` with `templates/interoperability_curated_template.csv`, and `resource_profile_gaps.csv` with `templates/resource_profile_curated_template.csv`.

The former **Bulk finalize all human-approved AI recommendations** / **Promote and apply all approved AI recommendations** sequence is now the single guided finalization action. Internally, guarded family scripts remain `step_apply_verify_adjudications.py` (using `--bulk-safe`), `step_apply_crosswalk_adjudications.py`, and `step_promote_review.py`. Crosswalk `DEFER` rows remain reviewed nonblocking deferrals; an empty family remains a valid no-op; no unapplied crosswalk decisions are processed.
            """
        ),
        code(
            r'''
advanced_checkbox = widgets.Checkbox(
    value=False,
    description="Show advanced diagnostics",
    indent=False,
)
advanced_domain_widget = widgets.Dropdown(
    options=[("Interoperability gaps", "interoperability"), ("Resource-profile gaps", "resource_profile")],
    description="Evidence",
    disabled=True,
    layout=widgets.Layout(width="440px"),
)
advanced_rows_widget = widgets.Dropdown(
    options=[25, 50, 100, 200],
    value=50,
    description="Max rows",
    disabled=True,
)
advanced_open_button = widgets.Button(
    description="Open selected evidence report",
    icon="table",
    disabled=True,
)
advanced_output = widgets.Output()


def _toggle_advanced(change=None):
    enabled = bool(advanced_checkbox.value)
    advanced_domain_widget.disabled = not enabled
    advanced_rows_widget.disabled = not enabled
    advanced_open_button.disabled = not enabled
    with advanced_output:
        clear_output(wait=True)
        if not enabled:
            print("Advanced diagnostics are hidden.")
            return
        status = compute_review_status(include_evidence=True)
        print("Current queue inventory:")
        display(pd.DataFrame(status["queues"]))
        print("Non-gating evidence backlog summary:")
        display(pd.DataFrame(status.get("non_gating_evidence_backlogs", [])))
        print("Family-specific CLI scripts remain available under scripts/ for targeted forensic use.")


def _open_evidence(_button):
    with advanced_output:
        domain = advanced_domain_widget.value
        status = compute_review_status(include_evidence=True)
        match = next(
            (item for item in status.get("non_gating_evidence_backlogs", []) if item["domain"] == domain),
            None,
        )
        if not match or not match.get("source_file"):
            print(f"No current {domain} evidence report was found.")
            return
        path = Path(match["source_file"])
        frame = pd.read_csv(path, dtype=str, keep_default_na=False).fillna("")
        if "status" in frame.columns:
            frame = frame.loc[~frame["status"].str.lower().isin(["resolved", "accepted", "not_applicable"])]
        limit = int(advanced_rows_widget.value)
        print(f"{path} | showing {min(limit, len(frame))} of {len(frame)} unresolved row(s)")
        display(frame.head(limit))


advanced_checkbox.observe(_toggle_advanced, names="value")
advanced_open_button.on_click(_open_evidence)
display(
    widgets.VBox(
        [
            advanced_checkbox,
            widgets.HBox([advanced_domain_widget, advanced_rows_widget]),
            advanced_open_button,
            advanced_output,
        ]
    )
)
_toggle_advanced()
'''
        ),
        md(
            """
            ## 7. Session audit

            This read-only summary lists only subprocesses actually launched during the guided session. When the dashboard is complete, proceed to the main pipeline notebook.
            """
        ),
        code(
            r'''
print(f"Debug directory: {RUNTIME.debug_dir}")
if RUNTIME.run_results():
    display(pd.DataFrame(RUNTIME.run_results()))
RUNTIME.write_session_summary()
refresh_guided_status()
'''
        ),
    ]


review_cells = build_guided_review_cells()


maintenance_cells = [
    md(
        """
        # NIH Tracker maintenance notebook (v19.0.2)

        This production-only notebook contains the low-frequency operational controls intentionally excluded from the pipeline and review notebooks: release installation or refresh, reference updates, lock handling, bounded cleanup, transaction restoration, and archive-backed reset.

        Destructive operations are preview-first and require checkboxes. Exact CLI confirmation tokens are generated internally rather than entered into free-text fields.
        """
    ),
    md(
        """
        ## 1. Install or refresh the production release

        Run this block only when installing v19.0.2 or repairing release code. It updates only `/content/drive/MyDrive/NIH_Tracker`; it does not scan or recreate dev/staging. Release-managed scripts, notebooks, references, templates, and documentation may be updated, while cards, inputs, runs, snapshots, builds, locks, caches, and credentials are preserved.

        **Create missing files** never overwrites an existing release file. **Refresh release code** replaces only changed release-managed files.
        """
    ),
    code(
        r'''
PREFILL_MODE = "Refresh release code"  # @param ["Refresh release code", "Create missing files"]
APPLY_PREFILL = False  # @param {type:"boolean"}

import datetime as dt
import hashlib
import os
import shutil
import subprocess
import sys
import uuid
import zipfile
from pathlib import Path

PRODUCTION_ROOT = Path("/content/drive/MyDrive/NIH_Tracker")
try:
    from google.colab import drive
    if not Path("/content/drive/MyDrive").exists():
        drive.mount("/content/drive")
except Exception:
    pass

os.environ["NIH_TRACKER_PRODUCTION_ONLY"] = "1"
os.environ["NIH_TRACKER_ROOT_PROD"] = str(PRODUCTION_ROOT)
os.environ["NIH_TRACKER_ENV"] = "prod"
os.environ["NIH_TRACKER_SHARED_CACHE_ROOT"] = "/content/drive/MyDrive/NIH_Tracker_shared/cache"
os.environ.pop("NIH_TRACKER", None)
os.environ.pop("NIH_TRACKER_ROOT_DEV", None)
os.environ.pop("NIH_TRACKER_ROOT_STAGING", None)
os.environ.pop("NIH_TRACKER_ALLOW_PROJECT_OVERRIDE", None)


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_release_inventory(release_root):
    sums_path = release_root / "SHA256SUMS"
    if not sums_path.is_file():
        raise FileNotFoundError(f"Missing release inventory: {sums_path}")
    checked = 0
    for raw_line in sums_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        expected, relative_text = line.split(maxsplit=1)
        relative = Path(relative_text.lstrip("*").strip())
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError(f"Unsafe SHA256SUMS path: {relative}")
        target = (release_root / relative).resolve()
        target.relative_to(release_root.resolve())
        if not target.is_file() or _sha256(target) != expected:
            raise RuntimeError(f"Release inventory mismatch: {relative}")
        checked += 1
    if not checked:
        raise RuntimeError("SHA256SUMS contains no verifiable files.")
    print(f"Verified {checked} release file hash(es) from {sums_path}.")


def _safe_extract_release(bundle):
    bundle = bundle.resolve()
    bundle_sha = _sha256(bundle)
    extraction_parent = Path("/content/nih_tracker_v19_bootstrap")
    extraction_parent.mkdir(parents=True, exist_ok=True)
    destination = extraction_parent / bundle_sha[:16]
    marker = destination / ".bundle_sha256"
    if marker.is_file() and marker.read_text(encoding="utf-8").strip() == bundle_sha:
        return destination
    temporary = extraction_parent / f".extract-{uuid.uuid4().hex}"
    temporary.mkdir(parents=True, exist_ok=False)
    total_size = 0
    file_count = 0
    try:
        with zipfile.ZipFile(bundle) as archive:
            for member in archive.infolist():
                relative = Path(member.filename)
                if relative.is_absolute() or ".." in relative.parts:
                    raise RuntimeError(f"Unsafe ZIP member: {member.filename!r}")
                unix_type = (member.external_attr >> 16) & 0o170000
                if unix_type == 0o120000:
                    raise RuntimeError(f"Symlink ZIP member is not allowed: {member.filename!r}")
                target = (temporary / relative).resolve()
                target.relative_to(temporary.resolve())
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                file_count += 1
                total_size += int(member.file_size)
                if file_count > 5000 or total_size > 500 * 1024 * 1024:
                    raise RuntimeError("Release ZIP exceeds the safe extraction limit.")
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
        if destination.exists():
            shutil.rmtree(destination)
        os.replace(temporary, destination)
        marker.write_text(bundle_sha + "\n", encoding="utf-8")
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    print(f"Safely extracted release bundle: {bundle} -> {destination}")
    return destination


bundle_candidates = []
for parent in (Path("/content"), Path("/content/drive/MyDrive")):
    if parent.is_dir():
        bundle_candidates.extend(parent.glob("*v19*bundle*.zip"))
newest_bundle = next(
    iter(sorted(
        {path.resolve() for path in bundle_candidates if path.is_file()},
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )),
    None,
)
release_roots = []
if newest_bundle:
    release_roots.append(_safe_extract_release(newest_bundle))
release_roots.extend([
    Path("/content/NIH_Tracker_release_v19"),
    Path("/content/drive/MyDrive/NIH_Tracker_release_v19"),
    Path.cwd(),
])
script_candidates = []
for release_root in release_roots:
    release_root = release_root.expanduser().resolve()
    direct = release_root / "scripts" / "step_initialize_environments.py"
    if direct.is_file():
        script_candidates.append(direct)
    if release_root.is_dir():
        script_candidates.extend(release_root.glob("*/scripts/step_initialize_environments.py"))
script_candidates.append(PRODUCTION_ROOT / "scripts" / "step_initialize_environments.py")
INITIALIZER = next((path.resolve() for path in script_candidates if path.is_file()), None)
if INITIALIZER is None:
    raise FileNotFoundError(
        "Production release installer was not found. Upload/extract the v19.0.2 bundle to /content."
    )
RELEASE_ROOT = INITIALIZER.parents[1]
_validate_release_inventory(RELEASE_ROOT)

mode = "missing-only" if PREFILL_MODE == "Create missing files" else "refresh-code"
command = [
    sys.executable,
    str(INITIALIZER),
    "--source-env", "prod",
    "--mode", mode,
    "--release-root", str(RELEASE_ROOT),
]
if APPLY_PREFILL:
    command.extend(["--apply", "--confirm", "INSTALL_PRODUCTION_RELEASE"])
log_dir = PRODUCTION_ROOT / "runs" / "bootstrap_notebook"
log_dir.mkdir(parents=True, exist_ok=True)
stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S%f")
log_path = log_dir / f"{stamp}.log"
result_path = log_dir / f"{stamp}.result.json"
print(f"Installer: {INITIALIZER}")
print(f"Release root: {RELEASE_ROOT}")
print(f"Debug log: {log_path}")
print("$ " + " ".join(command))
started = dt.datetime.now(dt.timezone.utc)
with log_path.open("w", encoding="utf-8") as handle:
    process = subprocess.Popen(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=os.environ.copy(),
        bufsize=1,
    )
    assert process.stdout is not None
    with process.stdout:
        for line in process.stdout:
            print(line, end="")
            handle.write(line)
            handle.flush()
    return_code = process.wait()
finished = dt.datetime.now(dt.timezone.utc)
result_path.write_text(
    __import__("json").dumps(
        {
            "schema": "nih-tracker-bootstrap-notebook-result/v2",
            "captured_at": finished.isoformat(timespec="seconds"),
            "environment": "prod",
            "mode": mode,
            "apply": APPLY_PREFILL,
            "initializer": str(INITIALIZER),
            "release_root": str(RELEASE_ROOT),
            "command": command,
            "returncode": return_code,
            "duration_seconds": round((finished - started).total_seconds(), 3),
            "log": str(log_path),
        },
        indent=2,
    ) + "\n",
    encoding="utf-8",
)
print(f"Return code: {return_code}")
print(f"Result record: {result_path}")
if return_code != 0:
    raise RuntimeError(f"Production release installation failed; inspect {log_path}")
'''
    ),
    md(
        """
        ## 2. Start production maintenance

        After the release block has installed the scripts, start the production runtime. Card restoration, reset, and reference updates require the production-write acknowledgement where indicated.
        """
    ),
    code('NOTEBOOK_NAME = "maintenance"\n' + SHARED_SETUP),
    md(
        """
        ## 3. Inspect storage, locks, and debug state

        This records a structured runtime diagnostic and a maintenance status report before any destructive operation.
        """
    ),
    code(
        r'''
DIAGNOSTICS = RUNTIME.show_diagnostics()
status_result = RUNTIME.run_script(
    "step_maintenance.py",
    ["status"],
    label="maintenance_status",
)
if not status_result.ok:
    raise RuntimeError(f"Maintenance status failed; inspect {status_result.log_path}")
'''
    ),
    md(
        """
        ## 4. Refresh BMIC, dbGaP, or publisher-policy source information

        Select one bounded update. For BMIC, upload a CSV whose filename contains `BMIC` to `/content`; when no upload is present, the updater validates the existing environment snapshot and reports a no-op. The dbGaP updater follows the official FHIR `ResearchStudy` catalogue and honors the response charset recorded by the service. Publisher refresh permits a bounded partial update: successfully fetched pages are pinned, while up to two transiently failed source rows retain their prior fields unchanged and are listed for retry. No updater changes semantic publisher-to-repository assertions automatically.

        Run once without **Apply** to inspect the structured report. Enable **Apply** only after reviewing the candidate, failed URLs, row counts, and source hashes.
        """
    ),
    code(
        r'''
REFERENCE_UPDATE = "No update"  # @param ["No update", "Validate/update BMIC snapshot", "Refresh dbGaP FHIR snapshot", "Refresh publisher policy source snapshots"]
APPLY_REFERENCE_UPDATE = False  # @param {type:"boolean"}

from pathlib import Path
import json
import pandas as pd
from IPython.display import display

if REFERENCE_UPDATE == "No update":
    print("No reference update selected.")
else:
    if APPLY_REFERENCE_UPDATE and common.ENV == "prod" and not ENABLE_PROD_WRITES:
        raise RuntimeError("Production reference updates require ENABLE_PROD_WRITES in setup.")
    if REFERENCE_UPDATE == "Validate/update BMIC snapshot":
        script = "step_update_bmic_info.py"
        arguments = []  # auto-detect /content upload; fall back to current environment snapshot
        label = "update_bmic_info"
        step_folder = "update_bmic_info"
        report_files = ["update_report.json", "update_error.json"]
    elif REFERENCE_UPDATE == "Refresh dbGaP FHIR snapshot":
        script = "step_update_dbgap_info.py"
        arguments = ["--mode", "fhir"]
        label = "update_dbgap_info"
        step_folder = "update_dbgap_info"
        report_files = ["update_report.json", "update_error.json"]
    else:
        script = "step_update_publisher_info.py"
        arguments = ["--continue-on-error", "--max-fetch-errors", "2"]
        label = "update_publisher_info"
        step_folder = "update_publisher_info"
        report_files = [
            "coverage.json",
            "publisher_source_drift_report.csv",
            "publisher_policy_sources.proposed.csv",
        ]
    if APPLY_REFERENCE_UPDATE:
        arguments.append("--apply")
    update_result = RUNTIME.run_script(
        script,
        arguments,
        label=label,
        writes_cards=bool(APPLY_REFERENCE_UPDATE),
    )
    print(
        ("Reference update completed." if update_result.ok else "Reference update stopped safely.")
        + f" Debug log: {update_result.log_path}"
    )

    step_root = Path(common.RUNS) / step_folder
    run_dirs = sorted(
        [path for path in step_root.iterdir() if path.is_dir()],
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    ) if step_root.is_dir() else []
    if run_dirs:
        latest_update_dir = run_dirs[0]
        print(f"Structured update directory: {latest_update_dir}")
        for report_name in report_files:
            report_path = latest_update_dir / report_name
            if not report_path.is_file():
                continue
            print()
            print(f"--- {report_name} ---")
            if report_path.suffix.casefold() == ".json":
                print(json.dumps(json.loads(report_path.read_text(encoding="utf-8")), indent=2))
            else:
                report_frame = pd.read_csv(report_path, dtype=str, keep_default_na=False)
                display(report_frame.head(100))
'''
    ),
    md(
        """
        ## 5. Inspect or remove an abandoned card lock

        Lock age alone is not proof that a writer has stopped. Removal is permitted only after independently confirming that no notebook or process is writing cards.
        """
    ),
    code(
        r'''
LOCK_ACTION = "Inspect only"  # @param ["Inspect only", "Remove confirmed abandoned lock"]
CONFIRM_NO_ACTIVE_WRITER = False  # @param {type:"boolean"}

arguments = ["remove-lock"]
if LOCK_ACTION == "Remove confirmed abandoned lock":
    if not CONFIRM_NO_ACTIVE_WRITER:
        raise RuntimeError(
            "Check CONFIRM_NO_ACTIVE_WRITER only after independently confirming all writers stopped."
        )
    arguments.extend(["--apply", "--confirm", "REMOVE_ABANDONED_CARD_LOCK"])
lock_result = RUNTIME.run_script(
    "step_maintenance.py",
    arguments,
    label="card_lock_maintenance",
)
if not lock_result.ok:
    raise RuntimeError(f"Lock maintenance failed; inspect {lock_result.log_path}")
'''
    ),
    md(
        """
        ## 6. Preview or apply bounded cleanup

        First create and inspect a sealed cleanup plan. A later execution can apply only that exact plan; the deletion set is not recalculated during apply. Cleanup keeps the newest run folders per step and can remove old temporary files. Card transaction snapshots are preserved unless their separate checkbox is selected. `latest/`, active debug output, and lock files are never deleted.
        """
    ),
    code(
        r'''
CLEANUP_ACTION = "Create a sealed cleanup plan"  # @param ["Create a sealed cleanup plan", "Apply newest unapplied plan after review"]
KEEP_RUNS_PER_STEP = 5  # @param [3, 5, 10, 20]
REMOVE_TEMPORARY_FILES = True  # @param {type:"boolean"}
PRUNE_CARD_SNAPSHOTS = False  # @param {type:"boolean"}
KEEP_CARD_SNAPSHOTS = 20  # @param [10, 20, 50, 100]

if CLEANUP_ACTION == "Create a sealed cleanup plan":
    cleanup_arguments = [
        "cleanup",
        "--keep-runs",
        str(KEEP_RUNS_PER_STEP),
        "--keep-snapshots",
        str(KEEP_CARD_SNAPSHOTS),
        "--temp-older-hours",
        "24",
    ]
    if REMOVE_TEMPORARY_FILES:
        cleanup_arguments.append("--remove-temporary")
    if PRUNE_CARD_SNAPSHOTS:
        cleanup_arguments.append("--prune-card-snapshots")
else:
    plans = sorted(
        (Path(common.RUNS) / "maintenance").glob("*/cleanup_plan.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    unapplied = [
        path for path in plans if not (path.parent / "cleanup_receipt.json").exists()
    ]
    if not unapplied:
        raise FileNotFoundError(
            "No unapplied sealed cleanup plan was found. Create and review one first."
        )
    cleanup_arguments = [
        "cleanup",
        "--plan",
        str(unapplied[0]),
        "--apply",
        "--confirm",
        "APPLY_MAINTENANCE_CLEANUP",
    ]

cleanup_result = RUNTIME.run_script(
    "step_maintenance.py",
    cleanup_arguments,
    label="bounded_cleanup",
)
if not cleanup_result.ok:
    raise RuntimeError(f"Cleanup failed; inspect {cleanup_result.log_path}")
'''
    ),
    md(
        """
        ## 7. Restore a committed card transaction

        Select a committed transaction from the dropdown. Preview verifies that current files still match the recorded after-images. Applying restoration creates a new transaction and will not overwrite legitimate later changes.
        """
    ),
    code(
        r'''
import json
from pathlib import Path
import ipywidgets as widgets
from IPython.display import display, clear_output

manifest_options = []
for manifest_path in sorted(
    (Path(common.SNAPSHOTS) / "cards").glob("*/manifest.json"),
    key=lambda path: path.stat().st_mtime,
    reverse=True,
):
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        continue
    if manifest.get("status") != "committed":
        continue
    label = (
        f"{manifest.get('completed_at') or manifest.get('started_at')} | "
        f"{manifest.get('step')} | {manifest.get('transaction_id')} | "
        f"{len(manifest.get('touched', []))} file(s)"
    )
    manifest_options.append((label, str(manifest_path)))

manifest_widget = widgets.Dropdown(
    options=manifest_options,
    description="Transaction",
    layout=widgets.Layout(width="1100px"),
)
apply_restore_widget = widgets.Checkbox(
    value=False,
    description="Apply verified restoration",
    indent=False,
)
restore_button = widgets.Button(
    description="Preview/restore selected transaction",
    button_style="warning",
    icon="undo",
    disabled=not bool(manifest_options),
)
restore_output = widgets.Output()


def _restore(_button):
    with restore_output:
        clear_output(wait=True)
        if not manifest_widget.value:
            print("No committed transaction is available.")
            return
        manifest_path = Path(manifest_widget.value)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        arguments = ["--manifest", str(manifest_path)]
        writes_cards = bool(apply_restore_widget.value)
        if writes_cards:
            if common.ENV == "prod" and not ENABLE_PROD_WRITES:
                print("Production restoration requires ENABLE_PROD_WRITES in setup.")
                return
            arguments.extend(
                [
                    "--apply",
                    "--confirm",
                    f"RESTORE_{manifest['transaction_id']}",
                ]
            )
        result = RUNTIME.run_script(
            "step_restore_card_snapshot.py",
            arguments,
            label="restore_card_transaction",
            writes_cards=writes_cards,
        )
        print(f"Result: {'PASS' if result.ok else 'FAIL'} | {result.log_path}")

restore_button.on_click(_restore)
display(
    widgets.VBox(
        [manifest_widget, apply_restore_widget, restore_button, restore_output]
    )
)
'''
    ),
    md(
        """
        ## 8. Archive and reset all cards

        Reset first creates a collision-resistant ZIP archive, then deletes card files inside one rollback-capable transaction. Select the reset action and check the confirmation box only when a full environment rebuild is intended.
        """
    ),
    code(
        r'''
RESET_ACTION = "Do not reset"  # @param ["Do not reset", "Archive and reset all cards"]
CONFIRM_RESET = False  # @param {type:"boolean"}

reset_arguments = ["reset-cards"]
writes_cards = False
if RESET_ACTION == "Archive and reset all cards":
    if not CONFIRM_RESET:
        print("Reset preview only. Check CONFIRM_RESET to perform the archived reset.")
    else:
        if common.ENV == "prod" and not ENABLE_PROD_WRITES:
            raise RuntimeError(
                "Production reset requires ENABLE_PROD_WRITES in setup."
            )
        reset_arguments.extend(
            [
                "--apply",
                "--confirm",
                f"ARCHIVE_AND_RESET_{common.ENV.upper()}_CARDS",
            ]
        )
        writes_cards = True
reset_result = RUNTIME.run_script(
    "step_maintenance.py",
    reset_arguments,
    label="archive_and_reset_cards",
    writes_cards=writes_cards,
)
if not reset_result.ok:
    raise RuntimeError(f"Reset operation failed; inspect {reset_result.log_path}")
RUNTIME.write_session_summary()
'''
    ),
    md(
        """
        ## 9. Persist the maintenance audit summary

        This final read-only cell consolidates operation results, recent ledger entries, and the current diagnostic state into the notebook debug directory.
        """
    ),
    code(
        r'''
import pandas as pd
from IPython.display import display

RUNTIME.show_diagnostics()
if RUNTIME.run_results():
    display(pd.DataFrame(RUNTIME.run_results()))
if RUNTIME.recent_ledger(20):
    display(pd.DataFrame(RUNTIME.recent_ledger(20)))
RUNTIME.write_session_summary()
'''
    ),
]


def main() -> list[Path]:
    outputs = [
        finalize("nih_tracker_colab_v19_pipeline.ipynb", pipeline_cells),
        finalize("nih_tracker_colab_v19_review_workbench.ipynb", review_cells),
        finalize("nih_tracker_colab_v19_maintenance.ipynb", maintenance_cells),
    ]
    from extend_gcp_notebooks import extend
    extend()
    for output in outputs:
        print(output)
    return outputs


if __name__ == "__main__":
    main()
