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
    "step_promote_ai_review",
    "step_apply_verify_adjudications",
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
    automated = importlib.import_module("automated_review")
    return common, review, automated


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


def make_queue(common, rows: list[dict[str, str]], stamp: str = "20260922T000000") -> Path:
    run_dir = Path(common.RUNS) / "verify_ids" / stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "verify_report.csv"
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


def response_for_bundle(report: dict, *, decision: str = "ACCEPT") -> tuple[Path, dict]:
    bundle = Path(report["bundle_root"])
    batch = next((bundle / "batches").glob("batch_*"))
    manifest = json.loads((batch / "manifest.json").read_text())
    items = [json.loads(line) for line in (batch / "items.jsonl").read_text().splitlines() if line.strip()]
    recommendations = []
    for item in items:
        target = item["allowed_targets"][0] if decision == "ACCEPT" else ""
        recommendations.append(
            {
                "review_key": item["review_key"],
                "item_sha256": item["item_sha256"],
                "decision": decision,
                "target": target,
                "alias": "",
                "confidence": "HIGH",
                "evidence_grade": "DIRECT_IDENTIFIER" if decision == "ACCEPT" else "CONFLICTING",
                "rationale": "Human review test recommendation.",
                "evidence_refs": ["queue_context"],
                "external_sources": [],
                "warnings": [],
                "human_review_required": True,
            }
        )
    response = {
        "schema": "nih-tracker-automated-review-response/v1",
        "bundle_id": manifest["bundle_id"],
        "batch_id": manifest["batch_id"],
        "batch_manifest_sha256": (batch / "manifest.sha256").read_text().strip(),
        "provider": "chatgpt",
        "model": "test-model",
        "generated_at": "2026-09-22T00:00:00+00:00",
        "recommendations": recommendations,
    }
    path = Path(report["run_dir"]) / "response.json"
    path.write_text(json.dumps(response))
    return path, items[0]


class BulkAIReviewTests(unittest.TestCase):
    def test_bulk_promotion_rebases_unchanged_item_across_unrelated_queue_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            common, _, automated = configure(root)
            common.save_card("R1", card("R1", "One"))
            common.save_card("R2", card("R2", "Two"))
            queue = make_queue(
                common,
                [
                    {
                        "repo_uid": "R1",
                        "card_name": "One",
                        "id_type": "rrid",
                        "candidate_id": "RRID:SCR_111111",
                        "candidate_url": "https://scicrunch.org/resolver/SCR_111111",
                        "candidate_name": "One",
                        "candidate_confidence": "0.99",
                        "candidate_source": "scicrunch",
                        "candidate_evidence_kind": "exact",
                        "candidate_eligible": "true",
                    }
                ],
            )
            report = automated.export_review_bundle(queue, provider="chatgpt")
            response, item = response_for_bundle(report)
            automated.import_review_response(response, strict_complete_batches=True)
            rec = automated.load_recommendations().iloc[0]
            automated.save_approvals(
                [
                    {
                        "recommendation_key": rec["recommendation_key"],
                        "review_key": rec["review_key"],
                        "queue_kind": "verify",
                        "approved": "true",
                        "approved_decision": "ACCEPT",
                        "approved_target": "RRID:SCR_111111",
                        "reviewer": "human",
                        "bundle_id": report["bundle_id"],
                        "item_sha256": item["item_sha256"],
                    }
                ]
            )
            frame = pd.read_csv(queue, dtype=str).fillna("")
            frame.loc[len(frame)] = {
                "repo_uid": "R2",
                "card_name": "Two",
                "id_type": "rrid",
                "candidate_id": "RRID:SCR_222222",
                "candidate_url": "https://scicrunch.org/resolver/SCR_222222",
                "candidate_name": "Two",
                "candidate_confidence": "0.90",
                "candidate_source": "scicrunch",
                "candidate_evidence_kind": "candidate",
                "candidate_eligible": "true",
            }
            frame.to_csv(queue, index=False)

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "step_promote_ai_review.py"),
                    "--bulk",
                    "--apply",
                    "--confirm",
                    "PROMOTE_AI_RECOMMENDATIONS",
                ],
                env=os.environ.copy(),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            adjudications = pd.read_csv(Path(common.INP) / "verify_adjudications.csv", dtype=str).fillna("")
            self.assertEqual(len(adjudications), 1)
            approvals = automated.load_approvals()
            self.assertIn("rebased", approvals.iloc[0]["promotion_status"])
            run_dir = sorted((Path(common.RUNS) / "promote_ai_review").iterdir())[-1]
            plan = pd.read_csv(run_dir / "promotion_plan.csv", dtype=str).fillna("")
            self.assertEqual(plan.iloc[0]["validation"], "PASS_REBASED")

    def test_bulk_promotion_quarantines_changed_identity_without_blocking_valid_peer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            common, _, automated = configure(root)
            common.save_card("R1", card("R1", "One"))
            common.save_card("R2", card("R2", "Two"))
            queue = make_queue(
                common,
                [
                    {
                        "repo_uid": "R1",
                        "card_name": "One",
                        "id_type": "rrid",
                        "candidate_id": "RRID:SCR_111111",
                        "candidate_url": "https://scicrunch.org/resolver/SCR_111111",
                        "candidate_name": "One",
                        "candidate_confidence": "0.99",
                        "candidate_source": "scicrunch",
                        "candidate_evidence_kind": "exact",
                        "candidate_eligible": "true",
                    },
                    {
                        "repo_uid": "R2",
                        "card_name": "Two",
                        "id_type": "rrid",
                        "candidate_id": "RRID:SCR_222222",
                        "candidate_url": "https://scicrunch.org/resolver/SCR_222222",
                        "candidate_name": "Two",
                        "candidate_confidence": "0.99",
                        "candidate_source": "scicrunch",
                        "candidate_evidence_kind": "exact",
                        "candidate_eligible": "true",
                    },
                ],
            )
            report = automated.export_review_bundle(queue, provider="chatgpt")
            response_path, _ = response_for_bundle(report)
            automated.import_review_response(response_path, strict_complete_batches=True)
            recommendations = automated.load_recommendations()
            approvals = []
            for row in recommendations.to_dict("records"):
                approvals.append(
                    {
                        "recommendation_key": row["recommendation_key"],
                        "review_key": row["review_key"],
                        "queue_kind": "verify",
                        "approved": "true",
                        "approved_decision": "ACCEPT",
                        "approved_target": row["recommended_target"],
                        "reviewer": "human",
                        "bundle_id": report["bundle_id"],
                        "item_sha256": row["item_sha256"],
                    }
                )
            automated.save_approvals(approvals)

            changed = common.load_card("R1")
            changed["identity"]["name"] = "One Renamed"
            common.save_card("R1", changed)
            frame = pd.read_csv(queue, dtype=str).fillna("")
            frame.loc[len(frame)] = {
                "repo_uid": "R3",
                "card_name": "Three",
                "id_type": "rrid",
                "candidate_id": "RRID:SCR_333333",
                "candidate_url": "https://scicrunch.org/resolver/SCR_333333",
                "candidate_name": "Three",
                "candidate_confidence": "0.90",
                "candidate_source": "scicrunch",
                "candidate_evidence_kind": "candidate",
                "candidate_eligible": "true",
            }
            frame.to_csv(queue, index=False)

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "step_promote_ai_review.py"),
                    "--bulk",
                    "--apply",
                    "--confirm",
                    "PROMOTE_AI_RECOMMENDATIONS",
                ],
                env=os.environ.copy(),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            adjudications = pd.read_csv(Path(common.INP) / "verify_adjudications.csv", dtype=str).fillna("")
            self.assertEqual(set(adjudications["repo_uid"]), {"R2"})
            saved = automated.load_approvals().set_index("review_key")
            statuses = set(saved["promotion_status"])
            self.assertTrue(any(value.startswith("quarantined:") for value in statuses))
            self.assertTrue(any(value.startswith("promoted:") for value in statuses))

    def test_bulk_promotion_closes_accept_when_identifier_is_already_present(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            common, _, automated = configure(root)
            common.save_card("R1", card("R1", "One"))
            queue = make_queue(
                common,
                [
                    {
                        "repo_uid": "R1",
                        "card_name": "One",
                        "id_type": "re3data",
                        "candidate_id": "r3d100000001",
                        "candidate_url": "https://www.re3data.org/repository/r3d100000001",
                        "candidate_name": "One",
                        "candidate_confidence": "1.0",
                        "candidate_source": "re3data",
                        "candidate_evidence_kind": "exact",
                        "candidate_eligible": "true",
                    }
                ],
            )
            report = automated.export_review_bundle(queue, provider="chatgpt")
            response, item = response_for_bundle(report)
            automated.import_review_response(response, strict_complete_batches=True)
            rec = automated.load_recommendations().iloc[0]
            automated.save_approvals(
                [
                    {
                        "recommendation_key": rec["recommendation_key"],
                        "review_key": rec["review_key"],
                        "queue_kind": "verify",
                        "approved": "true",
                        "approved_decision": "ACCEPT",
                        "approved_target": "r3d100000001",
                        "reviewer": "human",
                        "bundle_id": report["bundle_id"],
                        "item_sha256": item["item_sha256"],
                    }
                ]
            )
            current = common.load_card("R1")
            current["links"] = [
                {
                    "id_type": "re3data",
                    "id_value": "r3d100000001",
                    "url": "https://www.re3data.org/repository/r3d100000001",
                }
            ]
            common.save_card("R1", current)
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "step_promote_ai_review.py"),
                    "--bulk",
                    "--apply",
                    "--confirm",
                    "PROMOTE_AI_RECOMMENDATIONS",
                ],
                env=os.environ.copy(),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertFalse((Path(common.INP) / "verify_adjudications.csv").exists())
            approvals = automated.load_approvals()
            self.assertEqual(approvals.iloc[0]["promotion_status"], "satisfied:identifier_already_present")

    def test_bulk_safe_apply_receipts_stale_identifier_without_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            common, review, _ = configure(root)
            common.save_card("R1", card("R1", "One"))
            current = card("R2", "Two")
            current["links"] = [
                {
                    "id_type": "re3data",
                    "id_value": "r3dCURRENT",
                    "url": "https://www.re3data.org/repository/r3dCURRENT",
                }
            ]
            common.save_card("R2", current)
            rows = [
                {
                    "review_key": "valid",
                    "repo_uid": "R1",
                    "card_name": "One",
                    "id_type": "rrid",
                    "candidate_id": "RRID:SCR_111111",
                    "candidate_url": "https://scicrunch.org/resolver/SCR_111111",
                    "decision": "ACCEPT",
                    "reviewer": "human",
                    "reviewed_at": "2026-09-22T00:00:00+00:00",
                },
                {
                    "review_key": "stale",
                    "repo_uid": "R2",
                    "card_name": "Two",
                    "id_type": "re3data",
                    "candidate_id": "r3dPROPOSED",
                    "candidate_url": "https://www.re3data.org/repository/r3dPROPOSED",
                    "decision": "ACCEPT",
                    "reviewer": "human",
                    "reviewed_at": "2026-09-22T00:00:00+00:00",
                },
            ]
            prepared = review.prepare_decisions("verify", rows, reviewer="human")
            path, _, _ = review.upsert_adjudications("verify", prepared)
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "step_apply_verify_adjudications.py"),
                    "--adjudications",
                    str(path),
                    "--bulk-safe",
                    "--apply",
                ],
                env=os.environ.copy(),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            one = common.load_card("R1")
            self.assertTrue(any(link.get("id_value") == "RRID:SCR_111111" for link in one["links"]))
            two = common.load_card("R2")
            self.assertEqual(two["links"][0]["id_value"], "r3dCURRENT")
            saved = pd.read_csv(path, dtype=str).fillna("").set_index("review_key")
            self.assertEqual(saved.loc["valid", "apply_status"], "missing_link_added")
            self.assertEqual(
                saved.loc["stale", "apply_status"],
                "superseded:different_identifier_present",
            )

    def test_review_notebook_exposes_bulk_finalize_and_bulk_safe_identifier_mode(self):
        path = ROOT / "nih_tracker_colab_v18_review_workbench.ipynb"
        notebook = nbformat.read(path, as_version=4)
        text = "\n".join(cell.source for cell in notebook.cells)
        self.assertIn("Bulk finalize all human-approved AI recommendations", text)
        self.assertIn("Promote and apply all approved AI recommendations", text)
        self.assertIn("--bulk-safe", text)
        for cell in notebook.cells:
            if cell.cell_type == "code":
                self.assertEqual(cell.metadata.get("cellView"), "form")
                self.assertTrue(cell.metadata.get("jupyter", {}).get("source_hidden"))


if __name__ == "__main__":
    unittest.main()
