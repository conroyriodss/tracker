from __future__ import annotations

import ast
import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import nbformat


ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "nih_tracker_colab_v18_pipeline.ipynb"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


class ReleaseCellNoPlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        notebook = nbformat.read(PIPELINE, as_version=4)
        cls.final_code = [
            cell.source for cell in notebook.cells if cell.cell_type == "code"
        ][-1]
        cls.tree = ast.parse(cls.final_code)

    def test_expected_no_plan_state_is_not_implemented_as_exception(self):
        self.assertIn("NO ELIGIBLE RELEASE PLAN", self.final_code)
        self.assertIn("release_status.json", self.final_code)
        self.assertIn("No repository or environment was changed.", self.final_code)
        self.assertNotIn("raise FileNotFoundError", self.final_code)
        self.assertNotIn("def _newest_eligible", self.final_code)
        self.assertIn("def _find_newest_eligible", self.final_code)

    def test_plan_finder_returns_structured_empty_result(self):
        function_node = next(
            node
            for node in self.tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_find_newest_eligible"
        )
        module = ast.Module(body=[function_node], type_ignores=[])
        ast.fix_missing_locations(module)
        namespace = {
            "_verify_release_provenance": lambda candidate: (
                False,
                "missing pipeline release provenance or provenance seal",
            )
        }
        exec(compile(module, "<release-cell-test>", "exec"), namespace)
        plan, rejected = namespace["_find_newest_eligible"]([], "receipt.json")
        self.assertIsNone(plan)
        self.assertEqual(rejected, [])

    def test_apply_with_only_legacy_plans_exits_normally_and_does_not_publish(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runs = root / "runs"
            debug = root / "debug"
            old_plan = runs / "publish_github" / "legacy" / "publication_plan.json"
            old_plan.parent.mkdir(parents=True)
            old_plan.write_text("{}\n", encoding="utf-8")
            debug.mkdir(parents=True)

            class Common:
                ENV = "prod"
                PROJECT = root
                RUNS = runs
                BUILD = root / "build"

                @staticmethod
                def sha256_file(path):
                    path = Path(path)
                    return _sha256(path) if path.is_file() else ""

                @staticmethod
                def atomic_write_json(path, value):
                    path = Path(path)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(
                        json.dumps(value, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )

                @staticmethod
                def atomic_write_text(path, value):
                    path = Path(path)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(value, encoding="utf-8")

            class Runtime:
                session_id = "test-session"
                debug_dir = debug

                def __init__(self):
                    self.secret_loads = 0
                    self.script_runs = 0
                    self.summary_writes = 0

                def load_secrets(self, *args, **kwargs):
                    self.secret_loads += 1
                    raise AssertionError("Secrets must not be loaded without an eligible plan")

                def run_script(self, *args, **kwargs):
                    self.script_runs += 1
                    raise AssertionError("Publisher must not run without an eligible plan")

                def write_session_summary(self):
                    self.summary_writes += 1
                    return debug / "session_summary.json"

            runtime = Runtime()
            code = self.final_code.replace(
                'RELEASE_ACTION = "Create a sealed release plan"',
                'RELEASE_ACTION = "Apply the newest unapplied plan after review"',
                1,
            )
            namespace = {
                "RUNTIME": runtime,
                "common": Common,
                "PIPELINE_STEPS": [],
                "ENABLE_PROD_WRITES": False,
            }
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exec(compile(code, "<pipeline-release-cell>", "exec"), namespace)

            rendered = output.getvalue()
            self.assertIn("NO ELIGIBLE RELEASE PLAN", rendered)
            self.assertIn("No repository or environment was changed.", rendered)
            self.assertNotIn("Traceback", rendered)
            self.assertEqual(runtime.secret_loads, 0)
            self.assertEqual(runtime.script_runs, 0)
            self.assertEqual(runtime.summary_writes, 1)

            status_path = debug / "release_status.json"
            self.assertTrue(status_path.is_file())
            status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(status["state"], "no_eligible_release_plan")
            self.assertEqual(len(status["rejected_candidates"]), 1)
            self.assertIn(
                "missing pipeline release provenance",
                status["rejected_candidates"][0]["reason"],
            )


if __name__ == "__main__":
    unittest.main()
