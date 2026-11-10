from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import nbformat

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def _clear_modules():
    for name in list(sys.modules):
        if name in {"common", "automated_review", "notebook_runtime"} or name.startswith("step_"):
            sys.modules.pop(name, None)


def _configure(root: Path):
    for env in ("dev", "staging", "prod"):
        (root / env / "cards").mkdir(parents=True, exist_ok=True)
        (root / env / "inputs").mkdir(parents=True, exist_ok=True)
        (root / env / "runs").mkdir(parents=True, exist_ok=True)
    os.environ.pop("NIH_TRACKER", None)
    os.environ.pop("NIH_TRACKER_ALLOW_PROJECT_OVERRIDE", None)
    os.environ["NIH_TRACKER_ENV"] = "dev"
    os.environ["NIH_TRACKER_ROOT_DEV"] = str(root / "dev")
    os.environ["NIH_TRACKER_ROOT_STAGING"] = str(root / "staging")
    os.environ["NIH_TRACKER_ROOT_PROD"] = str(root / "prod")
    os.environ["NIH_TRACKER_SHARED_CACHE_ROOT"] = str(root / "shared")
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    _clear_modules()


class PipelineNetworkSelectionTests(unittest.TestCase):
    def test_pipeline_defaults_to_cached_only(self):
        notebook = nbformat.read(ROOT / "nih_tracker_colab_v18_pipeline.ipynb", as_version=4)
        source = "\n".join(cell.source for cell in notebook.cells)
        self.assertIn("REFRESH_EXTERNAL_EVIDENCE = False", source)
        self.assertIn("external=REFRESH_EXTERNAL_EVIDENCE", source)
        self.assertIn('["--re3data-policy", "none"]', source)
        self.assertIn('["--fairsharing-policy", "none", "--website-policy", "none"]', source)
        self.assertIn("RUNTIME.clear_pipeline_secrets()", source)
        self.assertIn("load_script_secrets=REFRESH_EXTERNAL_EVIDENCE", source)
        self.assertIn('os.environ["NIH_TRACKER_EXTERNAL_NETWORK_ENABLED"]', source)
        self.assertNotIn("SECRET_STATUS = RUNTIME.load_pipeline_secrets()", source)

    def test_review_workbench_has_restart_recovery_controls(self):
        notebook = nbformat.read(ROOT / "nih_tracker_colab_v18_review_workbench.ipynb", as_version=4)
        source = "\n".join(cell.source for cell in notebook.cells)
        self.assertIn("Restore download files after restart", source)
        self.assertIn("UPLOAD_RUN_ROOT", source)
        self.assertIn("Saved uploads", source)
        self.assertIn("Selected and saved to Drive", source)
        self.assertIn("_refresh_persisted_uploads", source)


class RuntimeSecretIsolationTests(unittest.TestCase):
    def tearDown(self):
        for name in ("FS_U", "FS_U_DEV", "NIH_TRACKER_SECRET_SOURCE_FS_U"):
            os.environ.pop(name, None)
        _clear_modules()

    def test_offline_script_does_not_load_declared_secrets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _configure(root)
            os.environ["NIH_TRACKER_SCRIPTS_ROOT"] = str(SCRIPTS)
            runtime_module = importlib.import_module("notebook_runtime")
            runtime = runtime_module.NotebookRuntime.create(
                notebook_name="offline_secret_test",
                env="dev",
                roots={env: str(root / env) for env in ("dev", "staging", "prod")},
                mount_drive=False,
                secret_provider=lambda name: "secret-user" if name == "FS_U_DEV" else None,
            )
            probe = root / "verify_external_ids.py"
            output = root / "output.json"
            probe.write_text(
                "import json, os, sys\njson.dump({'FS_U': bool(os.getenv('FS_U'))}, open(sys.argv[1], 'w'))\n",
                encoding="utf-8",
            )
            result = runtime.run_script(
                probe,
                [str(output)],
                strict_gate=False,
                load_script_secrets=False,
            )
            self.assertTrue(result.ok)
            self.assertEqual(json.loads(output.read_text()), {"FS_U": False})


class ProviderJsonRepairTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        _configure(Path(self.temporary.name))
        self.module = importlib.import_module("automated_review")

    def tearDown(self):
        self.temporary.cleanup()
        _clear_modules()

    def test_repairs_quotes_in_warning_and_markdown_url(self):
        raw = (
            '{"schema":"nih-tracker-automated-review-response/v1",'
            '"recommendations":[{"rationale":"Card identifies "Foo".",'
            '"warnings":["Generic label "Data Portal" is insufficient."],'
            '"external_sources":[{"url":"[https://example.org](https://example.org)",'
            '"title":"Example","supports":"identity"}]}]}'
        )
        repaired, events = self.module.repair_common_provider_json(raw)
        value = json.loads(repaired)
        self.assertTrue(events)
        source, source_events = self.module._repair_markdown_link_source(
            value["recommendations"][0]["external_sources"][0]
        )
        self.assertEqual(source["url"], "https://example.org")
        self.assertTrue(source_events)
        self.assertIn('"Foo"', value["recommendations"][0]["rationale"])
        self.assertIn('"Data Portal"', value["recommendations"][0]["warnings"][0])


if __name__ == "__main__":
    unittest.main()
