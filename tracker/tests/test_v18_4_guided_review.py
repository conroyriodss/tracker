from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import nbformat
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

MODULES = [
    "common",
    "review_widgets",
    "automated_review",
    "review_workflow",
    "step_review_workflow",
]


def configure(root: Path):
    for name in MODULES:
        sys.modules.pop(name, None)
    for key in list(os.environ):
        if key.startswith("NIH_TRACKER_ROOT_") or key in {
            "NIH_TRACKER",
            "NIH_TRACKER_ENV",
            "NIH_TRACKER_RUN_ID",
            "NIH_TRACKER_ALLOW_PROJECT_OVERRIDE",
            "NIH_TRACKER_SHARED_CACHE_ROOT",
            "NIH_TRACKER_PROD_WRITE_CONFIRMATION",
            "NIH_TRACKER_ALLOW_NONPROD_CREDENTIALS",
            "NIH_TRACKER_RE3DATA_CACHE_READ_ONLY",
        }:
            os.environ.pop(key, None)
    os.environ["NIH_TRACKER_ENV"] = "dev"
    for name in ("dev", "staging", "prod"):
        os.environ[f"NIH_TRACKER_ROOT_{name.upper()}"] = str(root / name)
    os.environ["NIH_TRACKER_SHARED_CACHE_ROOT"] = str(root / "shared" / "cache")
    common = importlib.import_module("common")
    review = importlib.import_module("review_widgets")
    workflow = importlib.import_module("review_workflow")
    return common, review, workflow


def card(uid: str, name: str) -> dict:
    return {
        "schema_version": "1.0",
        "repo_uid": uid,
        "kind": "repository",
        "identity": {
            "name": name,
            "abbreviation": "",
            "aliases": [],
            "homepage": f"https://{uid.lower()}.example.org/",
        },
        "links": [],
        "blocks": {},
        "curation": {"review_state": "needs_review"},
    }


def crosswalk_queue(common, stamp: str, *, status: str = "STALE") -> Path:
    run_dir = Path(common.RUNS) / "build_links_preflight" / stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "crosswalk_integrity.csv"
    pd.DataFrame(
        [
            {
                "source_row_fingerprint": f"fingerprint-{stamp}",
                "crosswalk_sha256": "crosswalk-sha",
                "crosswalk_row": "1",
                "registry_file": "registry.csv",
                "registry_sha256": "registry-sha",
                "id_remap_sha256": "remap-sha",
                "csv_id": "1",
                "probe_name": "Resource One",
                "registry_name": "Resource One",
                "crosswalk_uid": "R0",
                "resolved_uid": "",
                "status": status,
                "candidate_uids": "R1",
                "candidate_names": "Resource One",
                "candidate_scores": "1.0",
                "detail": "Needs review.",
            }
        ]
    ).to_csv(path, index=False)
    return path


class GuidedReviewTests(unittest.TestCase):
    def test_empty_project_is_complete(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, _, workflow = configure(Path(temporary))
            status = workflow.compute_review_status()
            self.assertEqual(status["state"], "COMPLETE")
            self.assertTrue(status["ready_for_pipeline"])
            self.assertEqual(status["operational_pending_rows"], 0)

    def test_only_newest_queue_per_family_is_loaded(self):
        with tempfile.TemporaryDirectory() as temporary:
            common, _, workflow = configure(Path(temporary))
            common.save_card("R1", card("R1", "Resource One"))
            old = crosswalk_queue(common, "20260924T000000")
            new = crosswalk_queue(common, "20260924T010000")
            os.utime(old, (1, 1))
            os.utime(new, (2, 2))
            rows = workflow.current_queue_summary()
            crosswalk = [row for row in rows if row["family"] == "crosswalk"]
            self.assertEqual(len(crosswalk), 1)
            self.assertEqual(Path(crosswalk[0]["source_file"]), new.resolve())
            self.assertEqual(crosswalk[0]["pending_rows"], 1)

    def test_saved_decision_changes_state_to_complete(self):
        with tempfile.TemporaryDirectory() as temporary:
            common, review, workflow = configure(Path(temporary))
            common.save_card("R1", card("R1", "Resource One"))
            source = crosswalk_queue(common, "20260924T010000")
            kind, frame = review.normalize_review_file(source)
            row = frame.iloc[0].to_dict()
            row["decision"] = "DEFER"
            review.upsert_adjudications(
                kind,
                review.prepare_decisions(kind, [row], reviewer="human"),
            )
            status = workflow.compute_review_status()
            self.assertEqual(status["queue_pending_rows"], 0)
            self.assertEqual(status["state"], "READY_TO_FINALIZE")
            self.assertEqual(status["operational_adjudications"]["crosswalk"], 1)

    def test_cli_apply_complete_state_launches_no_family_subprocess(self):
        with tempfile.TemporaryDirectory() as temporary:
            common, _, _ = configure(Path(temporary))
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "step_review_workflow.py"),
                    "--action",
                    "apply",
                    "--confirm",
                    "FINALIZE_REVIEW_WORKFLOW",
                ],
                env=os.environ.copy(),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn("no subprocess was launched", result.stdout.lower())
            reports = sorted(Path(common.RUNS).glob("review_workflow/*/review_workflow_result.json"))
            self.assertTrue(reports)
            report = json.loads(reports[-1].read_text())
            self.assertEqual(report["subprocesses"], [])
            self.assertEqual(report["after"]["state"], "COMPLETE")

    def test_guided_notebook_is_short_and_hides_advanced_backlogs(self):
        notebook = nbformat.read(
            ROOT / "nih_tracker_colab_v18_review_workbench.ipynb",
            as_version=4,
        )
        self.assertEqual(len(notebook.cells), 17)
        text = "\n".join(cell.source for cell in notebook.cells)
        self.assertIn("Review → Approve → Finalize", text)
        self.assertIn("Show advanced diagnostics", text)
        self.assertNotIn("## 9. Inspect interoperability evidence gaps", text)
        for cell in notebook.cells:
            if cell.cell_type == "code":
                self.assertEqual(cell.metadata.get("cellView"), "form")
                self.assertTrue(cell.metadata.get("jupyter", {}).get("source_hidden"))

    def test_generator_and_release_include_guided_orchestrator(self):
        text = (ROOT / "build_notebooks.py").read_text()
        self.assertIn("build_guided_review_cells", text)
        self.assertTrue((SCRIPTS / "review_workflow.py").is_file())
        self.assertTrue((SCRIPTS / "step_review_workflow.py").is_file())


if __name__ == "__main__":
    unittest.main()
