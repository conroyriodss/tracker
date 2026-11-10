from __future__ import annotations

import ast
import importlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

import nbformat
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

TRACKER_MODULES = [
    "common",
    "identity_hygiene",
    "notebook_runtime",
    "review_widgets",
    "step_build_frontend",
    "step_initialize_environments",
    "step_maintenance",
    "step_publish_github",
    "verify_external_ids",
]


def clear_tracker_modules() -> None:
    for name in list(sys.modules):
        if name in TRACKER_MODULES or name.startswith("step_"):
            sys.modules.pop(name, None)


def fresh_common(root: Path, env: str = "dev"):
    clear_tracker_modules()
    for key in list(os.environ):
        if key.startswith("NIH_TRACKER_ROOT_") or key.startswith(
            "NIH_TRACKER_SECRET_SOURCE_"
        ) or key in {
            "NIH_TRACKER",
            "NIH_TRACKER_ENV",
            "NIH_TRACKER_RUN_ID",
            "NIH_TRACKER_ALLOW_PROJECT_OVERRIDE",
            "NIH_TRACKER_SHARED_CACHE_ROOT",
            "NIH_TRACKER_PROD_WRITE_CONFIRMATION",
            "NIH_TRACKER_ALLOW_NONPROD_CREDENTIALS",
            "NIH_TRACKER_RE3DATA_CACHE_READ_ONLY",
            "NIH_TRACKER_NOTEBOOK_DEBUG_DIR",
            "NIH_TRACKER_SCRIPTS_ROOT",
        }:
            os.environ.pop(key, None)
    os.environ["NIH_TRACKER_ENV"] = env
    for name in ("dev", "staging", "prod"):
        os.environ[f"NIH_TRACKER_ROOT_{name.upper()}"] = str(root / name)
    os.environ["NIH_TRACKER_SHARED_CACHE_ROOT"] = str(root / "shared" / "cache")
    return importlib.import_module("common")


def card(uid: str, name: str = "Example") -> dict:
    return {
        "schema_version": "1.0",
        "repo_uid": uid,
        "kind": "repository",
        "identity": {"name": name},
        "links": [],
        "blocks": {},
        "curation": {"review_state": "needs_review"},
    }


def write_card(path: Path, uid: str, name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(card(uid, name), sort_keys=False), encoding="utf-8")


class NotebookStructureTests(unittest.TestCase):
    NOTEBOOKS = {
        "pipeline": ROOT / "nih_tracker_colab_v18_pipeline.ipynb",
        "review": ROOT / "nih_tracker_colab_v18_review_workbench.ipynb",
        "maintenance": ROOT / "nih_tracker_colab_v18_maintenance.ipynb",
    }

    def test_notebooks_regenerate_deterministically_and_validate(self):
        first = subprocess.run(
            [sys.executable, str(ROOT / "build_notebooks.py")],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(first.returncode, 0, msg=first.stdout + first.stderr)
        before = {name: path.read_bytes() for name, path in self.NOTEBOOKS.items()}
        second = subprocess.run(
            [sys.executable, str(ROOT / "build_notebooks.py")],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(second.returncode, 0, msg=second.stdout + second.stderr)
        after = {name: path.read_bytes() for name, path in self.NOTEBOOKS.items()}
        self.assertEqual(before, after)

        for name, path in self.NOTEBOOKS.items():
            notebook = nbformat.read(path, as_version=4)
            nbformat.validate(notebook)
            ids = [cell.get("id") for cell in notebook.cells]
            self.assertEqual(len(ids), len(set(ids)), msg=f"duplicate cell ID in {name}")
            for index, cell in enumerate(notebook.cells):
                if cell.cell_type == "code":
                    self.assertGreater(index, 0)
                    self.assertEqual(notebook.cells[index - 1].cell_type, "markdown")
                    ast.parse(cell.source)
                    self.assertEqual(cell.outputs, [])
                    self.assertIsNone(cell.execution_count)
            source = "\n".join(cell.source for cell in notebook.cells)
            self.assertNotIn('@param {type:"string"}', source)

    def test_notebook_responsibilities_are_separated(self):
        sources = {}
        for name, path in self.NOTEBOOKS.items():
            notebook = nbformat.read(path, as_version=4)
            sources[name] = "\n".join(cell.source for cell in notebook.cells)

        pipeline = sources["pipeline"]
        self.assertIn("RUNTIME.smoke_suite", pipeline)
        self.assertIn("crosswalk_preflight", pipeline)
        self.assertIn("--check-crosswalk-only", pipeline)
        self.assertLess(pipeline.index('"crosswalk_preflight"'), pipeline.index('"build_names"'))
        self.assertIn("external=REFRESH_EXTERNAL_EVIDENCE", pipeline)
        self.assertIn("REFRESH_EXTERNAL_EVIDENCE = False", pipeline)
        self.assertNotIn("step_promote_environment", pipeline)
        self.assertNotIn("NIH_Tracker_dev", pipeline)
        self.assertNotIn("NIH_Tracker_staging", pipeline)
        self.assertIn('ENVIRONMENT = "prod"', pipeline)
        self.assertIn('NIH_TRACKER_PRODUCTION_ONLY', pipeline)
        self.assertIn("step_publish_github.py", pipeline)
        self.assertIn("pipeline_success.json", pipeline)
        self.assertIn("pipeline_status.json", pipeline)
        self.assertIn("pipeline_exception.txt", pipeline)
        self.assertIn("PIPELINE STOPPED", pipeline)
        self.assertNotIn('raise RuntimeError(f"Pipeline stopped at', pipeline)
        self.assertIn("pipeline_release_provenance.json", pipeline)
        self.assertIn("pipeline_release_provenance.sha256", pipeline)
        self.assertIn("source_sha256", pipeline)
        self.assertIn("No unapplied GitHub publication plan with verified end-to-end pipeline provenance", pipeline)
        self.assertIn("RELEASE NOT ELIGIBLE", pipeline)
        self.assertIn("No promotion or GitHub publication plan was created.", pipeline)
        self.assertIn("RELEASE_ELIGIBLE = False", pipeline)
        self.assertNotIn("launch_review_workbench", pipeline)
        self.assertNotIn("step_maintenance.py", pipeline)
        self.assertNotIn("reset-cards", pipeline)
        pipeline_notebook = nbformat.read(self.NOTEBOOKS["pipeline"], as_version=4)
        final_code = [cell.source for cell in pipeline_notebook.cells if cell.cell_type == "code"][-1]
        self.assertIn("RELEASE_ACTION", final_code)
        self.assertIn("step_publish_github.py", final_code)
        self.assertIn(
            'APPLY_RELEASE = RELEASE_ACTION == "Apply the newest unapplied plan after review"',
            final_code,
        )
        self.assertNotIn('== "Apply the newest reviewed plan"', final_code)
        self.assertIn("Validate/propose (no card writes)", pipeline)

        verify_position = pipeline.index('"verify_external_ids"')
        spine_position = pipeline.index('"build_spine"')
        frontend_position = pipeline.index('"build_frontend"')
        self.assertLess(verify_position, spine_position)
        self.assertLess(spine_position, frontend_position)

        review = sources["review"]
        self.assertIn("launch_review_workbench", review)
        self.assertIn("step_apply_verify_adjudications.py", review)
        self.assertIn("step_apply_crosswalk_adjudications.py", review)
        self.assertIn("Crosswalk references", review)
        self.assertIn("reviewed nonblocking deferrals", review)
        self.assertIn("valid no-op; no unapplied crosswalk decisions", review)
        self.assertIn("step_promote_review.py", review)
        self.assertIn("AI_EXPORTABLE_FAMILIES", review)
        self.assertIn("manual_queue_widget", review)
        self.assertIn("ai_queue_widget", review)
        self.assertNotIn("NIH_Tracker_dev", review)
        self.assertNotIn("step_maintenance.py", review)
        self.assertNotIn("step_promote_environment", review)
        self.assertNotIn("step_build_spine.py", review)

        maintenance = sources["maintenance"]
        self.assertIn("step_initialize_environments.py", maintenance)
        self.assertIn("step_maintenance.py", maintenance)
        self.assertIn("step_restore_card_snapshot.py", maintenance)
        self.assertIn("step_remove_nonproduction_environments.py", maintenance)
        self.assertIn("INSTALL_PRODUCTION_RELEASE", maintenance)
        self.assertNotIn("launch_review_workbench", maintenance)
        self.assertNotIn("step_build_spine.py", maintenance)
        self.assertNotIn("step_promote_environment", maintenance)

    def test_review_uid_control_is_dropdown_not_free_text(self):
        source = (SCRIPTS / "review_widgets.py").read_text(encoding="utf-8")
        self.assertIn('repo_uid = self.widgets.Dropdown(', source)
        self.assertNotIn('repo_uid = self.widgets.Text(', source)
        self.assertIn('alias = self.widgets.Text(', source)
        self.assertIn('note = self.widgets.Textarea(', source)


class NotebookRuntimeTests(unittest.TestCase):
    def tearDown(self) -> None:
        for name in (
            "FS_U",
            "FS_U_DEV",
            "FS_U_STAGING",
            "GITHUB_TOKEN",
        ):
            os.environ.pop(name, None)
        clear_tracker_modules()

    def _runtime(self, root: Path, env: str, values: dict[str, str]):
        clear_tracker_modules()
        os.environ["NIH_TRACKER_SCRIPTS_ROOT"] = str(SCRIPTS)
        module = importlib.import_module("notebook_runtime")
        roots = {name: str(root / name) for name in ("dev", "staging", "prod")}
        runtime = module.NotebookRuntime.create(
            notebook_name=f"test_{env}",
            env=env,
            roots=roots,
            mount_drive=False,
            secret_provider=lambda name: values.get(name),
        )
        self.assertEqual(runtime.project, (root / env).resolve())
        return runtime

    def test_scoped_secret_precedence_and_cross_environment_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dev_runtime = self._runtime(root, "dev", {"FS_U_DEV": "dev-only-value"})
            status = dev_runtime.load_secrets(["FS_U"], quiet=True)
            self.assertTrue(status["FS_U"])
            self.assertEqual(os.environ["FS_U"], "dev-only-value")

            staging_runtime = self._runtime(root, "staging", {})
            status = staging_runtime.load_secrets(["FS_U"], quiet=True)
            self.assertFalse(status["FS_U"])
            self.assertNotIn("FS_U", os.environ)

    def test_child_process_receives_only_declared_managed_secrets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = self._runtime(
                root,
                "dev",
                {"FS_U_DEV": "scoped-user", "GITHUB_TOKEN_DEV": "github-secret"},
            )
            runtime.load_secrets(["FS_U", "GITHUB_TOKEN"], quiet=True)
            script_dir = root / "probe_scripts"
            script_dir.mkdir()
            output = root / "presence.json"
            plain = script_dir / "plain_probe.py"
            plain.write_text(
                "import json, os, sys\n"
                "json.dump({'FS_U': bool(os.getenv('FS_U')), 'GITHUB_TOKEN': bool(os.getenv('GITHUB_TOKEN'))}, open(sys.argv[1], 'w'))\n",
                encoding="utf-8",
            )
            result = runtime.run_script(plain, [str(output)], strict_gate=False)
            self.assertTrue(result.ok)
            self.assertEqual(json.loads(output.read_text()), {"FS_U": False, "GITHUB_TOKEN": False})

            verifier_probe = script_dir / "verify_external_ids.py"
            verifier_probe.write_text(
                "import json, os, sys\n"
                "json.dump({'FS_U': os.getenv('FS_U', ''), 'GITHUB_TOKEN': bool(os.getenv('GITHUB_TOKEN'))}, open(sys.argv[1], 'w'))\n",
                encoding="utf-8",
            )
            result = runtime.run_script(
                verifier_probe,
                [str(output)],
                required_secrets=["FS_U"],
                strict_gate=False,
            )
            self.assertTrue(result.ok)
            self.assertEqual(
                json.loads(output.read_text()),
                {"FS_U": "scoped-user", "GITHUB_TOKEN": False},
            )


class EnvironmentInitializationTests(unittest.TestCase):
    def test_prefill_preserves_existing_data_and_seals_plan(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            common = fresh_common(root, "prod")
            prod = root / "prod"
            write_card(prod / "cards" / "R1.yaml", "R1", "Production source")
            (prod / "inputs").mkdir(parents=True, exist_ok=True)
            (prod / "inputs" / "source.csv").write_text("a,b\n1,2\n", encoding="utf-8")
            (prod / "inputs" / "private_api_key.txt").write_text(
                "must-not-copy", encoding="utf-8"
            )
            (prod / "blocks").mkdir(parents=True, exist_ok=True)
            (prod / "blocks" / "identity.yaml").write_text("fields: []\n", encoding="utf-8")
            (prod / "frontend.html").write_text("<html><head></head></html>\n", encoding="utf-8")
            (prod / "runs" / "historical").mkdir(parents=True, exist_ok=True)
            (prod / "runs" / "historical" / "old.txt").write_text("old", encoding="utf-8")

            write_card(root / "dev" / "cards" / "R1.yaml", "R1", "Preserve this dev card")
            run_dir = prod / "runs" / "initialize_environments" / "test-plan"
            initializer = importlib.import_module("step_initialize_environments")
            plan_path = initializer.build_plan(
                source_env="prod",
                release_root=ROOT,
                mode="missing-only",
                run_dir=run_dir,
            )
            self.assertTrue((run_dir / initializer.PLAN_SEAL_NAME).is_file())

            original_plan = plan_path.read_bytes()
            plan = json.loads(original_plan)
            plan["entries"] = []
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "seal mismatch"):
                initializer.apply_plan(
                    plan_path, confirm="INITIALIZE_DEV_STAGING_PROD_FROM_PROD"
                )
            plan_path.write_bytes(original_plan)

            summary = initializer.apply_plan(
                plan_path, confirm="INITIALIZE_DEV_STAGING_PROD_FROM_PROD"
            )
            self.assertEqual(summary["status"], "committed")
            self.assertGreater(summary["files_written"], 0)
            self.assertEqual(
                yaml.safe_load((root / "dev" / "cards" / "R1.yaml").read_text())["identity"]["name"],
                "Preserve this dev card",
            )
            self.assertEqual(
                yaml.safe_load((root / "staging" / "cards" / "R1.yaml").read_text())["identity"]["name"],
                "Production source",
            )
            for env in ("dev", "staging"):
                self.assertEqual(
                    (root / env / "inputs" / "source.csv").read_text(encoding="utf-8"),
                    "a,b\n1,2\n",
                )
                self.assertFalse((root / env / "inputs" / "private_api_key.txt").exists())
                self.assertFalse((root / env / "runs" / "historical" / "old.txt").exists())
                self.assertTrue((root / env / "scripts" / "notebook_runtime.py").is_file())
                self.assertTrue((root / env / "tests" / "test_v13_notebooks_and_operations.py").is_file())
                self.assertTrue((root / env / "build_notebooks.py").is_file())
                self.assertTrue((root / env / "nih_tracker_colab_v18_pipeline.ipynb").is_file())
            self.assertTrue((root / "prod" / "scripts" / "notebook_runtime.py").is_file())
            self.assertTrue((run_dir / "environment_initialization_receipt.json").is_file())


class FrontendBuildTests(unittest.TestCase):
    def test_frontend_build_escapes_script_breakout_and_validates_cards(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            common = fresh_common(root, "dev")
            write_card(Path(common.CARDS) / "R1.yaml", "R1", "Example </script>")
            frontend = Path(common.PROJECT) / "frontend.html"
            frontend.write_text("<html><head><title>T</title></head><body></body></html>", encoding="utf-8")
            spine = Path(common.BUILD) / "spine.csv"
            spine.write_text("repo_uid,name\nR1,</script><script>alert(1)</script>\n", encoding="utf-8")
            output = Path(common.BUILD) / "index.html"
            builder = importlib.import_module("step_build_frontend")
            result = builder.build_frontend(
                frontend_path=frontend,
                spine_path=spine,
                output_path=output,
                cards_dir=Path(common.CARDS),
            )
            rendered = output.read_text(encoding="utf-8")
            self.assertEqual(result["cards"], 1)
            self.assertIn("window.SPINE_DATA", rendered)
            self.assertIn("window.CARDS_DATA", rendered)
            self.assertNotIn("</script><script>alert(1)</script>", rendered)
            self.assertIn("\\u003c/script\\u003e", rendered)
            self.assertEqual(len(result["output_sha256"]), 64)


class GitHubPublicationTests(unittest.TestCase):
    def test_non_docs_pages_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fresh_common(root, "prod")
            publisher = importlib.import_module("step_publish_github")
            with self.assertRaisesRegex(ValueError, "must be 'docs'"):
                publisher._validate_publish_dir("tracker")

    def test_sealed_publication_to_local_git_remote_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            common = fresh_common(root, "prod")
            output = Path(common.BUILD) / "index.html"
            output.write_text("<!doctype html><title>NIH Tracker</title>\n", encoding="utf-8")
            remote = root / "public.git"
            init = subprocess.run(
                ["git", "init", "--bare", "--initial-branch", "main", str(remote)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(init.returncode, 0, msg=init.stdout + init.stderr)

            publisher = importlib.import_module("step_publish_github")
            run_dir = Path(common.RUNS) / "publish_github" / "test-plan"
            plan_path = publisher.build_publication_plan(
                source=output,
                repository="example/nih-tracker-public",
                branch="main",
                publish_dir="docs",
                run_dir=run_dir,
            )
            first = publisher.apply_publication(
                plan_path,
                token="local-test-token",
                confirm=publisher.CONFIRMATION_TOKEN,
                remote_url=str(remote),
            )
            self.assertEqual(first["status"], "published")
            shown = subprocess.run(
                ["git", f"--git-dir={remote}", "show", "main:docs/index.html"],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(shown.returncode, 0, msg=shown.stderr)
            self.assertEqual(shown.stdout, output.read_text(encoding="utf-8"))

            second = publisher.apply_publication(
                plan_path,
                token="local-test-token",
                confirm=publisher.CONFIRMATION_TOKEN,
                remote_url=str(remote),
            )
            self.assertEqual(second["status"], "no_changes")
            self.assertEqual(first["commit"], second["commit"])

    def test_publication_rejects_loaded_secret_and_tampered_plan(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            common = fresh_common(root, "prod")
            publisher = importlib.import_module("step_publish_github")
            output = Path(common.BUILD) / "index.html"
            secret = "github-secret-value-123456"
            prior = os.environ.get("GITHUB_TOKEN")
            os.environ["GITHUB_TOKEN"] = secret
            try:
                output.write_text(f"<html>{secret}</html>", encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "contains the loaded value"):
                    publisher.build_publication_plan(
                        source=output,
                        repository="example/public",
                        branch="main",
                        publish_dir="docs",
                        run_dir=Path(common.RUNS) / "publish_github" / "secret-plan",
                    )
            finally:
                if prior is None:
                    os.environ.pop("GITHUB_TOKEN", None)
                else:
                    os.environ["GITHUB_TOKEN"] = prior

            output.write_text("<html>safe</html>", encoding="utf-8")
            run_dir = Path(common.RUNS) / "publish_github" / "tamper-plan"
            plan_path = publisher.build_publication_plan(
                source=output,
                repository="example/public",
                branch="main",
                publish_dir="docs",
                run_dir=run_dir,
            )
            plan = json.loads(plan_path.read_text())
            plan["publish_dir"] = "other"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "seal mismatch"):
                publisher.load_publication_plan(plan_path)


class MaintenanceTests(unittest.TestCase):
    def _aged_run(self, path: Path, age_seconds: int, text: str) -> None:
        path.mkdir(parents=True, exist_ok=True)
        (path / "result.txt").write_text(text, encoding="utf-8")
        timestamp = time.time() - age_seconds
        os.utime(path / "result.txt", (timestamp, timestamp))
        os.utime(path, (timestamp, timestamp))

    def test_cleanup_applies_only_unchanged_sealed_plan(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            common = fresh_common(root, "dev")
            newest = Path(common.RUNS) / "step_a" / "newest"
            old = Path(common.RUNS) / "step_a" / "old"
            self._aged_run(newest, 10, "new")
            self._aged_run(old, 500, "old")
            temp_path = Path(common.INP) / "data.tmp.abandoned"
            temp_path.write_text("temporary", encoding="utf-8")
            aged = time.time() - 3 * 3600
            os.utime(temp_path, (aged, aged))
            latest_temp = Path(common.LATEST) / "latest.tmp.abandoned"
            latest_temp.write_text("protected", encoding="utf-8")
            os.utime(latest_temp, (aged, aged))

            maintenance = importlib.import_module("step_maintenance")
            plan_path, plan = maintenance.build_cleanup_plan(
                keep_runs=1,
                prune_card_snapshots=False,
                keep_snapshots=20,
                remove_temporary=True,
                temp_older_hours=1,
            )
            candidate_paths = {row["path"] for row in plan["candidates"]}
            self.assertIn(str(old.resolve()), candidate_paths)
            self.assertIn(str(temp_path.resolve()), candidate_paths)
            self.assertNotIn(str(newest.resolve()), candidate_paths)
            self.assertNotIn(str(latest_temp.resolve()), candidate_paths)
            self.assertTrue((plan_path.parent / maintenance.CLEANUP_PLAN_SEAL).is_file())

            result = maintenance.apply_cleanup(
                plan_path, confirm=maintenance.CLEANUP_CONFIRMATION
            )
            self.assertEqual(result["status"], "committed")
            self.assertFalse(old.exists())
            self.assertFalse(temp_path.exists())
            self.assertTrue(newest.exists())
            self.assertTrue(latest_temp.exists())

    def test_cleanup_detects_drift_before_deleting_any_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            common = fresh_common(root, "dev")
            newest = Path(common.RUNS) / "step_a" / "newest"
            old = Path(common.RUNS) / "step_a" / "old"
            self._aged_run(newest, 10, "new")
            self._aged_run(old, 500, "old")
            temp_path = Path(common.INP) / "data.tmp.abandoned"
            temp_path.write_text("temporary", encoding="utf-8")
            aged = time.time() - 3 * 3600
            os.utime(temp_path, (aged, aged))

            maintenance = importlib.import_module("step_maintenance")
            plan_path, _ = maintenance.build_cleanup_plan(
                keep_runs=1,
                prune_card_snapshots=False,
                keep_snapshots=20,
                remove_temporary=True,
                temp_older_hours=1,
            )
            (old / "result.txt").write_text("changed after plan", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "no files were deleted"):
                maintenance.apply_cleanup(
                    plan_path, confirm=maintenance.CLEANUP_CONFIRMATION
                )
            self.assertTrue(old.exists())
            self.assertTrue(temp_path.exists())
            receipt = json.loads((plan_path.parent / "cleanup_receipt.json").read_text())
            self.assertEqual(receipt["status"], "preflight_failed")
            self.assertEqual(receipt["deleted_count"], 0)

    def test_archive_backed_reset_is_transactional(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            common = fresh_common(root, "dev")
            common.save_card("R1", card("R1", "Reset me"))
            maintenance = importlib.import_module("step_maintenance")
            result = maintenance.reset_cards(confirm="ARCHIVE_AND_RESET_DEV_CARDS")
            archive = Path(result["archive"])
            self.assertTrue(archive.is_file())
            self.assertFalse((Path(common.CARDS) / "R1.yaml").exists())
            with zipfile.ZipFile(archive) as handle:
                self.assertIn("R1.yaml", handle.namelist())
            manifests = [
                json.loads(path.read_text())
                for path in (Path(common.SNAPSHOTS) / "cards").glob("*/manifest.json")
            ]
            self.assertTrue(
                any(
                    row.get("step") == "maintenance_archive_and_reset"
                    and row.get("status") == "committed"
                    for row in manifests
                )
            )


if __name__ == "__main__":
    unittest.main()
