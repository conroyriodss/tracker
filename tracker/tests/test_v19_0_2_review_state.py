from __future__ import annotations

import importlib
import os
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
            "NIH_TRACKER_PRODUCTION_ONLY",
        }:
            os.environ.pop(key, None)
    project = root / "NIH_Tracker"
    os.environ["NIH_TRACKER_ENV"] = "prod"
    os.environ["NIH_TRACKER_PRODUCTION_ONLY"] = "1"
    os.environ["NIH_TRACKER_ROOT_PROD"] = str(project)
    os.environ["NIH_TRACKER_SHARED_CACHE_ROOT"] = str(root / "shared" / "cache")
    common = importlib.import_module("common")
    review = importlib.import_module("review_widgets")
    automated = importlib.import_module("automated_review")
    workflow = importlib.import_module("review_workflow")
    return common, review, automated, workflow


class ReviewStateHotfixTests(unittest.TestCase):
    def tearDown(self) -> None:
        for name in MODULES:
            sys.modules.pop(name, None)
        for key in list(os.environ):
            if key.startswith("NIH_TRACKER_ROOT_") or key in {
                "NIH_TRACKER",
                "NIH_TRACKER_ENV",
                "NIH_TRACKER_RUN_ID",
                "NIH_TRACKER_ALLOW_PROJECT_OVERRIDE",
                "NIH_TRACKER_SHARED_CACHE_ROOT",
                "NIH_TRACKER_PRODUCTION_ONLY",
            }:
                os.environ.pop(key, None)

    def test_current_operational_decision_suppresses_reopened_model_recommendation(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, review, _, workflow = configure(Path(temporary))
            decision = {field: "" for field in review.VERIFY_FIELDS}
            decision.update(
                {
                    "review_key": "same-item",
                    "repo_uid": "R1",
                    "card_name": "Resource One",
                    "id_type": "re3data",
                    "candidate_id": "r3d100000001",
                    "candidate_url": "https://www.re3data.org/repository/r3d100000001",
                    "decision": "REJECT",
                    "reviewer": "Richard",
                    "reviewed_at": "2026-09-29T12:00:00+00:00",
                }
            )
            review.upsert_adjudications("verify", [decision])

            recommendations = pd.DataFrame(
                [
                    {
                        "review_key": "same-item",
                        "recommendation_key": "old-rec",
                        "imported_at": "2026-09-20T12:00:00+00:00",
                    },
                    {
                        "review_key": "same-item",
                        "recommendation_key": "new-rec",
                        "imported_at": "2026-09-29T13:00:00+00:00",
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

            pending_without_decision = workflow.pending_recommendations_for_human_approval(
                recommendations,
                approvals,
                decided_review_keys=set(),
            )
            self.assertEqual(set(pending_without_decision["recommendation_key"]), {"new-rec"})

            pending = workflow.pending_recommendations_for_human_approval(
                recommendations,
                approvals,
            )
            self.assertTrue(pending.empty)
            self.assertEqual(workflow.operationally_decided_review_keys(), {"same-item"})

    def test_status_prefers_finalization_when_recommendation_is_already_decided(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, review, automated, workflow = configure(Path(temporary))
            decision = {field: "" for field in review.VERIFY_FIELDS}
            decision.update(
                {
                    "review_key": "same-item",
                    "repo_uid": "R1",
                    "card_name": "Resource One",
                    "id_type": "re3data",
                    "candidate_id": "r3d100000001",
                    "candidate_url": "https://www.re3data.org/repository/r3d100000001",
                    "decision": "REJECT",
                    "reviewer": "Richard",
                    "reviewed_at": "2026-09-29T12:00:00+00:00",
                }
            )
            review.upsert_adjudications("verify", [decision])

            rec = {field: "" for field in automated.RECOMMENDATION_FIELDS}
            rec.update(
                {
                    "recommendation_key": "new-rec",
                    "review_key": "same-item",
                    "queue_kind": "verify",
                    "imported_at": "2026-09-29T13:00:00+00:00",
                    "recommended_decision": "REJECT",
                    "human_review_required": "true",
                }
            )
            pd.DataFrame([rec], columns=automated.RECOMMENDATION_FIELDS).to_csv(
                automated.AI_RECOMMENDATIONS_CSV, index=False
            )

            status = workflow.compute_review_status(include_evidence=False)
            self.assertEqual(status["state"], "READY_TO_FINALIZE")
            self.assertEqual(
                status["recommendations"]["review_items_awaiting_human_approval"], 0
            )
            self.assertEqual(
                status["recommendations"]["recommendation_items_covered_by_current_decision"], 1
            )
            self.assertEqual(status["operational_adjudications"]["identifier"], 1)

    def test_notebook_uses_state_aware_next_action_panel(self):
        notebook = nbformat.read(
            ROOT / "nih_tracker_colab_v19_review_workbench.ipynb", as_version=4
        )
        text = "\n".join(cell.source for cell in notebook.cells)
        self.assertIn("Follow the current review action", text)
        self.assertIn("guided_next_action_html", text)
        self.assertIn("candidate_controls_box.layout.display", text)
        self.assertIn("already covered by current decisions", text)
        self.assertIn("v19.0.2", text)


if __name__ == "__main__":
    unittest.main()
