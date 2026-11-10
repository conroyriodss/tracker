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
    "step_export_ai_review",
    "step_import_ai_review",
    "step_promote_ai_review",
    "step_run_ai_review",
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


def card(uid: str, name: str, *, homepage: str = "https://example.org/") -> dict:
    return {
        "schema_version": "1.0",
        "repo_uid": uid,
        "kind": "repository",
        "identity": {
            "name": name,
            "abbreviation": "EX",
            "aliases": [f"{name} archive"],
            "homepage": homepage,
        },
        "links": [],
        "blocks": {
            "resource_profile": {
                "descriptions": [
                    {
                        "text": f"Public description for {name}.",
                        "source_url": homepage,
                        "tier": "attestation",
                    }
                ],
                "organizations": [{"name": "Example Institute", "role": "provider"}],
            }
        },
        "curation": {"review_state": "needs_review"},
    }


def make_verify_queue(common, uid: str = "R1") -> Path:
    run_dir = Path(common.RUNS) / "verify_ids" / "20260101T000000"
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "verify_report.csv"
    pd.DataFrame(
        [
            {
                "repo_uid": uid,
                "card_name": "Example Repository",
                "id_type": "rrid",
                "candidate_id": "RRID:SCR_123456",
                "candidate_url": "https://scicrunch.org/resolver/SCR_123456",
                "candidate_name": "Example Repository",
                "candidate_confidence": "0.98",
                "candidate_source": "scicrunch",
                "candidate_evidence_kind": "exact_name",
                "candidate_eligible": "true",
            }
        ]
    ).to_csv(path, index=False)
    return path


def make_response(report: dict, *, provider: str, decision: str = "ACCEPT", target: str = "RRID:SCR_123456") -> tuple[Path, dict]:
    bundle = Path(report["bundle_root"])
    batch_dir = next((bundle / "batches").glob("batch_*"))
    manifest = json.loads((batch_dir / "manifest.json").read_text(encoding="utf-8"))
    item = json.loads((batch_dir / "items.jsonl").read_text(encoding="utf-8").splitlines()[0])
    response = {
        "schema": "nih-tracker-automated-review-response/v1",
        "bundle_id": manifest["bundle_id"],
        "batch_id": manifest["batch_id"],
        "batch_manifest_sha256": (batch_dir / "manifest.sha256").read_text().strip(),
        "provider": provider,
        "model": f"{provider}-test",
        "generated_at": "2026-09-17T00:00:00+00:00",
        "recommendations": [
            {
                "review_key": item["review_key"],
                "item_sha256": item["item_sha256"],
                "decision": decision,
                "target": target,
                "alias": "",
                "confidence": "HIGH",
                "evidence_grade": "DIRECT_IDENTIFIER",
                "rationale": "The resolver identifier and repository identity agree.",
                "evidence_refs": ["queue_context", "registry_evidence"],
                "external_sources": [
                    {
                        "url": "https://scicrunch.org/resolver/SCR_123456",
                        "title": "SciCrunch resolver",
                        "supports": "identifier resolution",
                    }
                ],
                "warnings": [],
                "human_review_required": True,
            }
        ],
    }
    path = Path(report["run_dir"]) / f"{provider}_response.json"
    path.write_text(json.dumps(response), encoding="utf-8")
    return path, item


class AutomatedReviewTests(unittest.TestCase):
    def test_export_is_sealed_public_metadata_and_prompt_is_non_operational(self):
        with tempfile.TemporaryDirectory() as temporary:
            common, _, automated = configure(Path(temporary))
            common.save_card("R1", card("R1", "Example Repository"))
            queue = make_verify_queue(common)
            report = automated.export_review_bundle(
                queue,
                provider="both",
                batch_size=10,
                web_research=True,
            )
            self.assertEqual(report["queue_kind"], "verify")
            self.assertEqual(report["item_count"], 1)
            self.assertTrue(Path(report["master_zip"]).is_file())
            batch = next((Path(report["bundle_root"]) / "batches").glob("batch_*"))
            prompt = (batch / "PROMPT.md").read_text(encoding="utf-8")
            self.assertIn("recommendation", prompt.lower())
            self.assertIn("human", prompt.lower())
            self.assertIn("do not", prompt.lower())
            self.assertIn("do not echo the rejected candidate", prompt.lower())
            self.assertIn("json serializer", prompt.lower())
            self.assertIn("never use markdown link syntax", prompt.lower())
            self.assertNotIn("OPENAI_API_KEY", (batch / "items.jsonl").read_text())
            self.assertEqual(len(report["download_files"]), 2)
            self.assertTrue(all(Path(row["path"]).is_file() for row in report["download_files"]))
            manifest = json.loads((Path(report["bundle_root"]) / "bundle_manifest.json").read_text())
            self.assertEqual(manifest["data_classification"], "public_repository_metadata_only")
            self.assertEqual(manifest["operational_effect"], "recommendations_only; human promotion required")

    def test_export_batches_a_117_item_identifier_backlog(self):
        with tempfile.TemporaryDirectory() as temporary:
            common, _, automated = configure(Path(temporary))
            common.save_card("R1", card("R1", "Example Repository"))
            run_dir = Path(common.RUNS) / "verify_ids" / "20260101T000117"
            run_dir.mkdir(parents=True, exist_ok=True)
            queue = run_dir / "verify_report.csv"
            rows = []
            for index in range(117):
                rows.append(
                    {
                        "repo_uid": "R1",
                        "card_name": "Example Repository",
                        "id_type": "rrid",
                        "candidate_id": f"RRID:SCR_{index:06d}",
                        "candidate_url": f"https://scicrunch.org/resolver/SCR_{index:06d}",
                        "candidate_name": "Example Repository",
                        "candidate_confidence": "0.90",
                        "candidate_source": "scicrunch",
                        "candidate_evidence_kind": "candidate",
                        "candidate_eligible": "true",
                    }
                )
            pd.DataFrame(rows).to_csv(queue, index=False)
            report = automated.export_review_bundle(
                queue,
                provider="both",
                batch_size=20,
                web_research=True,
            )
            self.assertEqual(report["item_count"], 117)
            self.assertEqual(report["batch_count"], 6)
            batch_counts = [batch["item_count"] for batch in report["batches"]]
            self.assertEqual(batch_counts, [20, 20, 20, 20, 20, 17])
            self.assertTrue(Path(report["master_zip"]).is_file())

    def test_valid_response_imports_recommendation_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            common, _, automated = configure(Path(temporary))
            common.save_card("R1", card("R1", "Example Repository"))
            report = automated.export_review_bundle(make_verify_queue(common), provider="chatgpt")
            response_path, _ = make_response(report, provider="chatgpt")
            imported = automated.import_review_response(response_path, strict_complete_batches=True)
            self.assertEqual(imported["valid_recommendations"], 1)
            recommendations = automated.load_recommendations()
            self.assertEqual(len(recommendations), 1)
            self.assertEqual(recommendations.iloc[0]["validation_status"], "VALID")
            self.assertFalse((Path(common.INP) / "verify_adjudications.csv").exists())
            self.assertEqual(common.load_card("R1")["links"], [])

    def test_fifty_item_reject_batch_with_echoed_targets_imports_safely(self):
        with tempfile.TemporaryDirectory() as temporary:
            common, _, automated = configure(Path(temporary))
            common.save_card("R1", card("R1", "Example Repository"))
            run_dir = Path(common.RUNS) / "verify_ids" / "20260101T000050"
            run_dir.mkdir(parents=True, exist_ok=True)
            queue = run_dir / "verify_report.csv"
            rows = []
            for index in range(50):
                candidate = f"RRID:SCR_{index:06d}"
                rows.append(
                    {
                        "repo_uid": "R1",
                        "card_name": "Example Repository",
                        "id_type": "rrid",
                        "candidate_id": candidate,
                        "candidate_url": f"https://scicrunch.org/resolver/{candidate}",
                        "candidate_name": f"Different Resource {index}",
                        "candidate_confidence": "0.70",
                        "candidate_source": "scicrunch",
                        "candidate_evidence_kind": "candidate",
                        "candidate_eligible": "true",
                    }
                )
            pd.DataFrame(rows).to_csv(queue, index=False)
            report = automated.export_review_bundle(
                queue,
                provider="chatgpt",
                batch_size=50,
            )
            batch_dir = next((Path(report["bundle_root"]) / "batches").glob("batch_*"))
            manifest = json.loads((batch_dir / "manifest.json").read_text())
            items = [json.loads(line) for line in (batch_dir / "items.jsonl").read_text().splitlines()]
            response = {
                "schema": "nih-tracker-automated-review-response/v1",
                "bundle_id": manifest["bundle_id"],
                "batch_id": manifest["batch_id"],
                "batch_manifest_sha256": (batch_dir / "manifest.sha256").read_text().strip(),
                "provider": "chatgpt",
                "model": "GPT-test",
                "generated_at": "2026-09-18T00:00:00Z",
                "recommendations": [
                    {
                        "review_key": item["review_key"],
                        "item_sha256": item["item_sha256"],
                        "decision": "REJECT",
                        "target": item["allowed_targets"][0],
                        "alias": "",
                        "confidence": "HIGH",
                        "evidence_grade": "CONFLICTING",
                        "rationale": "The candidate is a different resource.",
                        "evidence_refs": ["queue_context"],
                        "external_sources": [],
                        "warnings": [],
                        "human_review_required": True,
                    }
                    for item in items
                ],
            }
            response_path = Path(report["run_dir"]) / "fifty_rejects.json"
            response_path.write_text(json.dumps(response))
            imported = automated.import_review_response(
                response_path,
                strict_complete_batches=True,
            )
            self.assertEqual(imported["valid_recommendations"], 50)
            self.assertEqual(imported["normalized_recommendations"], 50)
            self.assertEqual(imported["normalization_events"], 50)
            recommendations = automated.load_recommendations()
            self.assertEqual(len(recommendations), 50)
            self.assertTrue((recommendations["recommended_target"] == "").all())
            self.assertTrue((recommendations["submitted_target"] != "").all())

    def test_non_action_target_echo_is_safely_normalized_and_audited(self):
        with tempfile.TemporaryDirectory() as temporary:
            common, _, automated = configure(Path(temporary))
            common.save_card("R1", card("R1", "Example Repository"))
            report = automated.export_review_bundle(make_verify_queue(common), provider="chatgpt")
            response_path, _ = make_response(
                report,
                provider="chatgpt",
                decision="REJECT",
                target="RRID:SCR_123456",
            )
            imported = automated.import_review_response(
                response_path,
                strict_complete_batches=True,
            )
            self.assertEqual(imported["valid_recommendations"], 1)
            self.assertEqual(imported["normalized_recommendations"], 1)
            self.assertEqual(imported["normalization_events"], 1)
            recommendation = automated.load_recommendations().iloc[0]
            self.assertEqual(recommendation["recommended_decision"], "REJECT")
            self.assertEqual(recommendation["recommended_target"], "")
            self.assertEqual(recommendation["submitted_target"], "RRID:SCR_123456")
            self.assertEqual(
                recommendation["validation_status"],
                "VALID_WITH_NORMALIZATION",
            )
            events = json.loads(recommendation["normalizations_json"])
            self.assertEqual(events[0]["field"], "target")
            self.assertTrue(events[0]["matched_exported_target"])
            consensus = automated.recommendation_consensus()
            self.assertEqual(len(consensus), 1)
            self.assertEqual(consensus.iloc[0]["consensus_decision"], "REJECT")
            self.assertFalse((Path(common.INP) / "verify_adjudications.csv").exists())
            self.assertEqual(common.load_card("R1")["links"], [])

    def test_common_chat_json_syntax_damage_is_repaired_and_audited(self):
        with tempfile.TemporaryDirectory() as temporary:
            common, _, automated = configure(Path(temporary))
            common.save_card("R1", card("R1", "Example Repository"))
            report = automated.export_review_bundle(make_verify_queue(common), provider="chatgpt")
            response_path, _ = make_response(
                report,
                provider="chatgpt",
                decision="REJECT",
                target="",
            )
            response = json.loads(response_path.read_text())
            response["recommendations"][0]["rationale"] = (
                'The card identifies "Example Repository" while the candidate is different.'
            )
            text = json.dumps(response, separators=(",", ":"))
            text = text.replace(
                '\\"Example Repository\\"',
                '"Example Repository"',
            )
            text = text.replace(
                '"human_review_required":true',
                '"human_review_required"\\:true',
            )
            response_path.write_text(text)

            imported = automated.import_review_response(
                response_path,
                strict_complete_batches=True,
            )
            self.assertEqual(imported["valid_recommendations"], 1)
            self.assertEqual(imported["syntax_repaired_files"], 1)
            self.assertEqual(imported["syntax_repair_events"], 2)
            repaired = Path(imported["repaired_response_files"][0])
            self.assertTrue(repaired.is_file())
            parsed = json.loads(repaired.read_text())
            self.assertEqual(parsed["recommendations"][0]["decision"], "REJECT")
            self.assertEqual(parsed["recommendations"][0]["target"], "")
            self.assertIn('"Example Repository"', parsed["recommendations"][0]["rationale"])
            repair_report = Path(imported["run_dir"]) / "syntax_repair_report.json"
            self.assertTrue(repair_report.is_file())

    def test_safe_json_repair_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as temporary:
            common, _, automated = configure(Path(temporary))
            common.save_card("R1", card("R1", "Example Repository"))
            report = automated.export_review_bundle(make_verify_queue(common), provider="chatgpt")
            response_path, _ = make_response(
                report,
                provider="chatgpt",
                decision="REJECT",
                target="",
            )
            text = response_path.read_text().replace(
                '"human_review_required": true',
                '"human_review_required"\\: true',
            )
            response_path.write_text(text)
            with self.assertRaisesRegex(Exception, "could not be parsed"):
                automated.import_review_response(
                    response_path,
                    strict_complete_batches=True,
                    allow_safe_json_repair=False,
                )

    def test_markdown_autolink_damage_is_reversed_without_changing_actionable_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            common, _, automated = configure(Path(temporary))
            common.save_card("R1", card("R1", "Example Repository"))
            report = automated.export_review_bundle(make_verify_queue(common), provider="chatgpt")
            response_path, _ = make_response(report, provider="chatgpt")
            response = json.loads(response_path.read_text())
            source = response["recommendations"][0]["external_sources"][0]
            source["url"] = "[https://scicrunch.org/resolver/SCR_123456"
            source["title"] = (
                "SciCrunch](https://scicrunch.org/resolver/SCR_123456/"
                "%22,%22title%22:%22SciCrunch) resolver"
            )
            response_path.write_text(json.dumps(response))

            imported = automated.import_review_response(
                response_path,
                strict_complete_batches=True,
            )
            self.assertEqual(imported["valid_recommendations"], 1)
            self.assertEqual(imported["normalization_events"], 1)
            row = automated.load_recommendations().iloc[0]
            self.assertEqual(row["recommended_target"], "RRID:SCR_123456")
            sources = json.loads(row["external_sources_json"])
            self.assertEqual(
                sources[0]["url"],
                "https://scicrunch.org/resolver/SCR_123456",
            )
            self.assertEqual(sources[0]["title"], "SciCrunch resolver")

    def test_import_rejects_invented_target_and_is_all_or_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            common, _, automated = configure(Path(temporary))
            common.save_card("R1", card("R1", "Example Repository"))
            report = automated.export_review_bundle(make_verify_queue(common), provider="chatgpt")
            response_path, _ = make_response(report, provider="chatgpt", target="RRID:SCR_INVENTED")
            with self.assertRaisesRegex(Exception, "target is not one of"):
                automated.import_review_response(response_path)
            self.assertFalse(automated.AI_RECOMMENDATIONS_CSV.exists())

    def test_import_rejects_queue_or_card_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            common, _, automated = configure(Path(temporary))
            common.save_card("R1", card("R1", "Example Repository"))
            queue = make_verify_queue(common)
            report = automated.export_review_bundle(queue, provider="chatgpt")
            response_path, _ = make_response(report, provider="chatgpt")
            queue.write_text(queue.read_text() + "\n", encoding="utf-8")
            with self.assertRaisesRegex(Exception, "source queue SHA-256 changed"):
                automated.import_review_response(response_path)

    def test_two_provider_high_evidence_agreement_is_bulk_eligible(self):
        with tempfile.TemporaryDirectory() as temporary:
            common, _, automated = configure(Path(temporary))
            common.save_card("R1", card("R1", "Example Repository"))
            report = automated.export_review_bundle(make_verify_queue(common), provider="both")
            for provider in ("chatgpt", "perplexity"):
                response_path, _ = make_response(report, provider=provider)
                automated.import_review_response(response_path, strict_complete_batches=True)
            consensus = automated.recommendation_consensus()
            self.assertEqual(consensus.iloc[0]["consensus_status"], "AGREE")
            self.assertEqual(consensus.iloc[0]["provider_count"], 2)
            self.assertEqual(consensus.iloc[0]["bulk_eligible"], "true")

    def test_two_models_from_same_provider_do_not_count_as_independent_consensus(self):
        with tempfile.TemporaryDirectory() as temporary:
            common, _, automated = configure(Path(temporary))
            common.save_card("R1", card("R1", "Example Repository"))
            report = automated.export_review_bundle(make_verify_queue(common), provider="chatgpt")
            for provider in ("chatgpt", "openai"):
                response_path, _ = make_response(report, provider=provider)
                # Use distinct model strings while retaining one provider family.
                payload = json.loads(response_path.read_text())
                payload["model"] = f"{provider}-distinct-model"
                response_path.write_text(json.dumps(payload))
                automated.import_review_response(response_path, strict_complete_batches=True)
            consensus = automated.recommendation_consensus()
            self.assertEqual(consensus.iloc[0]["provider_count"], 1)
            self.assertEqual(consensus.iloc[0]["consensus_status"], "SINGLE")
            self.assertEqual(consensus.iloc[0]["bulk_eligible"], "false")

    def test_human_approval_promotes_to_adjudication_but_not_card(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            common, _, automated = configure(root)
            common.save_card("R1", card("R1", "Example Repository"))
            report = automated.export_review_bundle(make_verify_queue(common), provider="chatgpt")
            response_path, item = make_response(report, provider="chatgpt")
            automated.import_review_response(response_path, strict_complete_batches=True)
            rec = automated.load_recommendations().iloc[0]
            automated.save_approvals(
                [
                    {
                        "recommendation_key": rec["recommendation_key"],
                        "review_key": rec["review_key"],
                        "queue_kind": "verify",
                        "approved": "true",
                        "approved_decision": "ACCEPT",
                        "approved_target": "RRID:SCR_123456",
                        "reviewer": "human-reviewer",
                        "bundle_id": report["bundle_id"],
                        "item_sha256": item["item_sha256"],
                    }
                ]
            )
            env = os.environ.copy()
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "step_promote_ai_review.py"),
                    "--apply",
                    "--confirm",
                    "PROMOTE_AI_RECOMMENDATIONS",
                ],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            adjudications = pd.read_csv(Path(common.INP) / "verify_adjudications.csv", dtype=str).fillna("")
            self.assertEqual(len(adjudications), 1)
            self.assertEqual(adjudications.iloc[0]["decision"], "ACCEPT")
            self.assertEqual(common.load_card("R1")["links"], [])

    def test_crosswalk_response_cannot_select_unexported_uid(self):
        with tempfile.TemporaryDirectory() as temporary:
            common, _, automated = configure(Path(temporary))
            common.save_card("R1", card("R1", "Candidate One"))
            common.save_card("R2", card("R2", "Candidate Two"))
            inputs = Path(common.INP)
            pd.DataFrame([{"csv_id": "1", "probe_name": "Old", "registry_name": "Old", "repo_uid": "R999"}]).to_csv(inputs / "crosswalk.csv", index=False)
            pd.DataFrame([{"Repository_Name": "Old"}]).to_csv(inputs / "final_ultimate_enhanced_repos.csv", index=False)
            run_dir = Path(common.RUNS) / "build_links_preflight" / "20260101T000000"
            run_dir.mkdir(parents=True, exist_ok=True)
            source = run_dir / "crosswalk_integrity.csv"
            pd.DataFrame(
                [
                    {
                        "crosswalk_row": "2",
                        "csv_id": "1",
                        "probe_name": "Old",
                        "registry_name": "Old",
                        "crosswalk_uid": "R999",
                        "resolved_uid": "",
                        "status": "STALE",
                        "scope_reason": "",
                        "candidate_uids": "R1; R2",
                        "candidate_names": "Candidate One; Candidate Two",
                        "candidate_scores": "0.9; 0.8",
                        "source_row_fingerprint": "abc123",
                        "crosswalk_sha256": common.sha256_file(inputs / "crosswalk.csv"),
                        "registry_file": str(inputs / "final_ultimate_enhanced_repos.csv"),
                        "registry_sha256": common.sha256_file(inputs / "final_ultimate_enhanced_repos.csv"),
                        "id_remap_sha256": "",
                        "detail": "stale",
                    }
                ]
            ).to_csv(source, index=False)
            report = automated.export_review_bundle(source, provider="chatgpt")
            bundle = Path(report["bundle_root"])
            batch = next((bundle / "batches").glob("batch_*"))
            manifest = json.loads((batch / "manifest.json").read_text())
            item = json.loads((batch / "items.jsonl").read_text().splitlines()[0])
            response = {
                "schema": "nih-tracker-automated-review-response/v1",
                "bundle_id": manifest["bundle_id"],
                "batch_id": manifest["batch_id"],
                "batch_manifest_sha256": (batch / "manifest.sha256").read_text().strip(),
                "provider": "chatgpt",
                "model": "test",
                "generated_at": "2026-09-17T00:00:00+00:00",
                "recommendations": [
                    {
                        "review_key": item["review_key"],
                        "item_sha256": item["item_sha256"],
                        "decision": "REPLACE_CROSSWALK_UID",
                        "target": "R3",
                        "alias": "",
                        "confidence": "HIGH",
                        "evidence_grade": "EXACT_NAME_ONLY",
                        "rationale": "Invented target should fail.",
                        "evidence_refs": [],
                        "external_sources": [],
                        "warnings": [],
                        "human_review_required": True,
                    }
                ],
            }
            response_path = Path(report["run_dir"]) / "bad_crosswalk.json"
            response_path.write_text(json.dumps(response))
            with self.assertRaisesRegex(Exception, "target is not one of"):
                automated.import_review_response(response_path)

    def test_direct_api_runner_is_a_non_network_compatibility_stub(self):
        with tempfile.TemporaryDirectory() as temporary:
            configure(Path(temporary))
            result = subprocess.run(
                [sys.executable, str(SCRIPTS / "step_run_ai_review.py")],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("Direct API review is not included", result.stdout)
            self.assertIn("offline export", result.stdout)

    def test_released_notebooks_hide_all_code_cells(self):
        for path in ROOT.glob("nih_tracker_colab_v18_*.ipynb"):
            notebook = nbformat.read(path, as_version=4)
            code_cells = [cell for cell in notebook.cells if cell.cell_type == "code"]
            self.assertTrue(code_cells, path.name)
            for cell in code_cells:
                self.assertEqual(cell.metadata.get("cellView"), "form")
                self.assertTrue(cell.metadata.get("jupyter", {}).get("source_hidden"))
                self.assertTrue(cell.metadata.get("colab", {}).get("view-in-form"))
                self.assertEqual(cell.outputs, [])
                self.assertIsNone(cell.execution_count)


if __name__ == "__main__":
    unittest.main()
