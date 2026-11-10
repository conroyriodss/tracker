from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import nbformat
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def clear_modules() -> None:
    for name in list(sys.modules):
        if name in {
            "common",
            "notebook_runtime",
            "review_workflow",
            "automated_review",
            "review_widgets",
            "step_initialize_environments",
            "step_remove_nonproduction_environments",
        } or name.startswith("step_"):
            sys.modules.pop(name, None)


class ProductionOnlyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_env = os.environ.copy()
        if str(SCRIPTS) not in sys.path:
            sys.path.insert(0, str(SCRIPTS))

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.old_env)
        clear_modules()

    def configure(self, prod: Path) -> None:
        os.environ["NIH_TRACKER_PRODUCTION_ONLY"] = "1"
        os.environ["NIH_TRACKER_ENV"] = "prod"
        os.environ["NIH_TRACKER_ROOT_PROD"] = str(prod)
        os.environ["NIH_TRACKER_SHARED_CACHE_ROOT"] = str(prod.parent / "NIH_Tracker_shared" / "cache")
        os.environ.pop("NIH_TRACKER", None)
        clear_modules()

    def test_common_exposes_only_production_in_notebook_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            prod = Path(temporary) / "NIH_Tracker"
            self.configure(prod)
            common = importlib.import_module("common")
            self.assertEqual(common.VALID_ENVS, ("prod",))
            self.assertEqual(common.ENV, "prod")
            self.assertEqual(Path(common.PROJECT), prod.resolve())
            self.assertEqual(common.configured_project_roots(), {"prod": str(prod.resolve())})
            self.assertTrue(common.environment_summary()["production_only"])

    def test_production_installer_does_not_collect_cards_or_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            prod = Path(temporary) / "NIH_Tracker"
            (prod / "cards").mkdir(parents=True)
            (prod / "inputs").mkdir(parents=True)
            (prod / "runs" / "install_release" / "plan").mkdir(parents=True)
            (prod / "cards" / "R1.yaml").write_text("repo_uid: R1\n", encoding="utf-8")
            (prod / "inputs" / "private.csv").write_text("x\n1\n", encoding="utf-8")
            self.configure(prod)
            initializer = importlib.import_module("step_initialize_environments")
            plan_path = initializer.build_plan(
                source_env="prod",
                release_root=ROOT,
                mode="refresh-code",
                run_dir=prod / "runs" / "install_release" / "plan",
            )
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertTrue(plan["production_only"])
            self.assertEqual({row["target_env"] for row in plan["entries"]}, {"prod"})
            relative_paths = {row["relative_path"] for row in plan["entries"]}
            self.assertFalse(any(path.startswith("cards/") for path in relative_paths))
            self.assertFalse(any(path.startswith("inputs/") for path in relative_paths))

    def test_nonproduction_removal_preserves_prod_and_shared(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            drive = Path(temporary)
            prod = drive / "NIH_Tracker"
            (prod / "runs").mkdir(parents=True)
            (drive / "NIH_Tracker_shared" / "cache").mkdir(parents=True)
            for name in ("NIH_Tracker_dev", "NIH_Tracker_staging"):
                target = drive / name
                target.mkdir()
                (target / "example.txt").write_text(name, encoding="utf-8")
            self.configure(prod)
            remover = importlib.import_module("step_remove_nonproduction_environments")
            plan_path = remover.create_plan(drive_root=drive)
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertEqual({row["name"] for row in plan["targets"]}, {"NIH_Tracker_dev", "NIH_Tracker_staging"})
            remover.apply_plan(plan_path, confirm=remover.CONFIRM)
            self.assertTrue(prod.is_dir())
            self.assertTrue((drive / "NIH_Tracker_shared").is_dir())
            self.assertFalse((drive / "NIH_Tracker_dev").exists())
            self.assertFalse((drive / "NIH_Tracker_staging").exists())

    def test_nonproduction_plan_is_bounded_noop_when_legacy_roots_are_absent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            drive = Path(temporary)
            prod = drive / "NIH_Tracker"
            (prod / "runs").mkdir(parents=True)
            (drive / "NIH_Tracker_shared" / "cache").mkdir(parents=True)
            # This unrelated Drive content must never be traversed or hashed by
            # the legacy-root planner.
            unrelated = drive / "Docs" / "virtual.gdoc"
            unrelated.parent.mkdir()
            unrelated.write_text("not part of the operation", encoding="utf-8")
            self.configure(prod)
            remover = importlib.import_module("step_remove_nonproduction_environments")
            captured: dict[str, object] = {}

            def capture_ledger(step, **kwargs):
                captured["step"] = step
                captured.update(kwargs)

            remover.ledger_append = capture_ledger
            plan_path = remover.create_plan(drive_root=drive)
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertFalse(plan["requires_apply"])
            self.assertEqual(plan["existing_target_count"], 0)
            self.assertEqual(captured["inputs"], {"plan": str(plan_path)})
            self.assertEqual(captured["params"], {"drive_root": str(drive.resolve())})

    def test_new_recommendation_reopens_terminal_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            prod = Path(temporary) / "NIH_Tracker"
            self.configure(prod)
            workflow = importlib.import_module("review_workflow")
            recs = pd.DataFrame(
                [
                    {
                        "review_key": "same-item",
                        "recommendation_key": "old-rec",
                        "imported_at": "2026-09-20T12:00:00+00:00",
                    },
                    {
                        "review_key": "same-item",
                        "recommendation_key": "new-rec",
                        "imported_at": "2026-09-24T18:09:00+00:00",
                    },
                ]
            )
            approvals = pd.DataFrame(
                [
                    {
                        "review_key": "same-item",
                        "recommendation_key": "old-rec",
                        "approved": "true",
                        "approved_at": "2026-09-22T12:00:00+00:00",
                        "promotion_status": "superseded:source_changed",
                    }
                ]
            )
            pending = workflow.pending_recommendations_for_human_approval(recs, approvals)
            self.assertEqual(set(pending["review_key"]), {"same-item"})
            self.assertEqual(set(pending["recommendation_key"]), {"new-rec"})

    def test_notebooks_are_production_only_and_split_manual_from_ai_export(self) -> None:
        pipeline = nbformat.read(ROOT / "nih_tracker_colab_v18_pipeline.ipynb", as_version=4)
        review = nbformat.read(ROOT / "nih_tracker_colab_v18_review_workbench.ipynb", as_version=4)
        maintenance = nbformat.read(ROOT / "nih_tracker_colab_v18_maintenance.ipynb", as_version=4)
        pipeline_text = "\n".join(cell.source for cell in pipeline.cells)
        review_text = "\n".join(cell.source for cell in review.cells)
        maintenance_text = "\n".join(cell.source for cell in maintenance.cells)
        for text in (pipeline_text, review_text):
            self.assertNotIn("NIH_Tracker_dev", text)
            self.assertNotIn("NIH_Tracker_staging", text)
            self.assertIn('NIH_TRACKER_PRODUCTION_ONLY', text)
        self.assertNotIn("step_promote_environment", pipeline_text)
        self.assertIn("AI_EXPORTABLE_FAMILIES", review_text)
        self.assertIn("manual_queue_widget", review_text)
        self.assertIn("ai_queue_widget", review_text)
        self.assertIn("step_remove_nonproduction_environments.py", maintenance_text)
        self.assertIn("DELETE_DEV_STAGING_ENVIRONMENTS", maintenance_text)
        self.assertIn("INSTALL_PRODUCTION_RELEASE", maintenance_text)


if __name__ == "__main__":
    unittest.main()
