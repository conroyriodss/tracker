from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

MODULES = ["common", "review_widgets", "automated_review", "step_build_policy_context", "step_build_publisher_context"]


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


def make_deferred_crosswalk_queue(common) -> Path:
    run_dir = Path(common.RUNS) / "build_links_preflight" / "20260924T000000"
    run_dir.mkdir(parents=True, exist_ok=True)
    source = run_dir / "crosswalk_integrity.csv"
    pd.DataFrame(
        [
            {
                "source_row_fingerprint": "source-fingerprint-1",
                "crosswalk_sha256": "crosswalk-sha",
                "crosswalk_row": "1",
                "registry_file": "registry.csv",
                "registry_sha256": "registry-sha",
                "id_remap_sha256": "remap-sha",
                "csv_id": "1",
                "probe_name": "Deferred resource",
                "registry_name": "Deferred resource",
                "crosswalk_uid": "R0",
                "resolved_uid": "",
                "status": "DEFERRED",
                "candidate_uids": "R1",
                "candidate_names": "Resource One",
                "candidate_scores": "1.0",
                "detail": "Previously reviewed deferral.",
            }
        ]
    ).to_csv(source, index=False)
    return source


class ReviewWorkflowHotfixTests(unittest.TestCase):
    def test_discovery_distinguishes_pending_from_decided_crosswalk_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            common, review, _ = configure(Path(temporary))
            common.save_card("R1", card("R1", "Resource One"))
            source = make_deferred_crosswalk_queue(common)
            kind, queue = review.normalize_review_file(source)
            row = queue.iloc[0].to_dict()
            row["decision"] = "DEFER"
            prepared = review.prepare_decisions(kind, [row], reviewer="human")
            review.upsert_adjudications(kind, prepared)

            discovered = review.discover_review_files(
                common.RUNS,
                include_project_verify_report=False,
            )
            selected = discovered.loc[discovered["source_file"] == str(source.resolve())].iloc[0]
            self.assertEqual(int(selected["reviewable_rows"]), 1)
            self.assertEqual(int(selected["pending_rows"]), 0)
            self.assertEqual(int(selected["decided_rows"]), 1)

    def test_export_requires_deliberate_include_decided_for_reviewed_queue(self):
        with tempfile.TemporaryDirectory() as temporary:
            common, review, automated = configure(Path(temporary))
            common.save_card("R1", card("R1", "Resource One"))
            source = make_deferred_crosswalk_queue(common)
            kind, queue = review.normalize_review_file(source)
            row = queue.iloc[0].to_dict()
            row["decision"] = "DEFER"
            review.upsert_adjudications(
                kind,
                review.prepare_decisions(kind, [row], reviewer="human"),
            )

            with self.assertRaises(automated.NoEligibleReviewRowsError):
                automated.export_review_bundle(source, provider="chatgpt")
            report = automated.export_review_bundle(
                source,
                provider="chatgpt",
                include_decided=True,
            )
            self.assertEqual(report["item_count"], 1)

    def test_export_cli_treats_no_eligible_rows_as_structured_noop(self):
        with tempfile.TemporaryDirectory() as temporary:
            common, review, _ = configure(Path(temporary))
            common.save_card("R1", card("R1", "Resource One"))
            source = make_deferred_crosswalk_queue(common)
            kind, queue = review.normalize_review_file(source)
            row = queue.iloc[0].to_dict()
            row["decision"] = "DEFER"
            review.upsert_adjudications(
                kind,
                review.prepare_decisions(kind, [row], reviewer="human"),
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "step_export_ai_review.py"),
                    "--source",
                    str(source),
                ],
                env=os.environ.copy(),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn("NO ELIGIBLE REVIEW ROWS", result.stdout)
            reports = sorted(Path(common.RUNS).glob("automated_review_export/*/export_report.json"))
            self.assertTrue(reports)
            report = json.loads(reports[-1].read_text())
            self.assertEqual(report["status"], "no_eligible_rows")
            self.assertEqual(report["item_count"], 0)

    def test_empty_card_adjudication_family_is_valid_noop(self):
        with tempfile.TemporaryDirectory() as temporary:
            common, _, _ = configure(Path(temporary))
            adjudications = Path(common.INP) / "adjudications.csv"
            adjudications.parent.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(columns=["repo_uid", "verdict", "applied_at"]).to_csv(
                adjudications,
                index=False,
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "step_promote_review.py"),
                    "--adjudications",
                    "adjudications.csv",
                ],
                env=os.environ.copy(),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn("valid no-op", result.stdout)
            ledger = pd.read_csv(common.LEDGER, dtype=str).fillna("")
            row = ledger.iloc[-1]
            self.assertEqual(row["gate_passed"].lower(), "true")
            outputs = json.loads(row["outputs"])
            self.assertEqual(int(outputs["no_op"]), 1)

    def test_notebook_generator_exposes_pending_and_decided_counts(self):
        text = (ROOT / "build_notebooks.py").read_text()
        self.assertIn("pending |", text)
        self.assertIn("No pending rows.", text)
        self.assertIn("valid no-op", (SCRIPTS / "step_promote_review.py").read_text())

    def test_cadr_configured_uid_uses_explicit_remap_before_host_matching(self):
        with tempfile.TemporaryDirectory() as temporary:
            common, _, _ = configure(Path(temporary))
            common.save_card("R228", card("R228", "Kids First Data Resource Center"))
            pd.DataFrame(
                [{"old_uid": "B219", "new_uid": "R228"}]
            ).to_csv(Path(common.PROJECT) / "id_remap.csv", index=False)
            policy = importlib.import_module("step_build_policy_context")
            cadr = pd.DataFrame(
                [
                    {
                        "official_name": "Kids First Data",
                        "repo_uid": "B219",
                        "url": "https://kidsfirstdrc.org/resources/",
                    }
                ]
            )
            matches, report = policy._cadr_matches(common.all_cards(), cadr)
            self.assertIn("R228", matches)
            self.assertEqual(report[0]["matched_repo_uid"], "R228")
            self.assertEqual(report[0]["match_basis"], "reference_uid_remapped")

    def test_cadr_stale_configured_uid_does_not_fall_back_to_host(self):
        with tempfile.TemporaryDirectory() as temporary:
            common, _, _ = configure(Path(temporary))
            current = card("R228", "Kids First Data Resource Center")
            current["identity"]["homepage"] = "https://kidsfirstdrc.org/resources/"
            common.save_card("R228", current)
            policy = importlib.import_module("step_build_policy_context")
            cadr = pd.DataFrame(
                [
                    {
                        "official_name": "Kids First Data",
                        "repo_uid": "B219",
                        "url": "https://kidsfirstdrc.org/resources/",
                    }
                ]
            )
            matches, report = policy._cadr_matches(common.all_cards(), cadr)
            self.assertFalse(matches)
            self.assertEqual(report[0]["status"], "STALE_REFERENCE")
            self.assertEqual(report[0]["matched_repo_uid"], "")

    def test_publisher_shared_institutional_host_does_not_establish_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            common, _, _ = configure(Path(temporary))
            arrayexpress = card("R45", "ArrayExpress")
            arrayexpress["identity"]["homepage"] = "https://www.ebi.ac.uk/biostudies/arrayexpress"
            common.save_card("R45", arrayexpress)
            publisher = importlib.import_module("step_build_publisher_context")
            mentions = pd.DataFrame(
                [
                    {
                        "repo_uid": "",
                        "repository_name": "European Nucleotide Archive",
                        "repository_url": "https://www.ebi.ac.uk/ena/browser/home",
                        "match_terms": "",
                        "publisher_family": "Nature",
                        "journal_title": "",
                        "policy_title": "Policy",
                        "policy_url": "https://publisher.example/policy",
                        "policy_level": "publisher",
                        "mention_type": "mandatory",
                        "data_domain": "sequence data",
                        "access_model": "public",
                        "source_location": "table",
                        "evidence_note": "",
                        "date_checked": "2026-09-24",
                    }
                ]
            )
            matches, report = publisher._match_mentions(common.all_cards(), mentions)
            self.assertFalse(matches)
            self.assertEqual(report[0]["status"], "UNMATCHED")

    def test_publisher_configured_uid_resolves_through_remap(self):
        with tempfile.TemporaryDirectory() as temporary:
            common, _, _ = configure(Path(temporary))
            common.save_card("R49", card("R49", "NIDDK Central Repository Resources for Research"))
            pd.DataFrame(
                [{"old_uid": "B152", "new_uid": "R49"}]
            ).to_csv(Path(common.PROJECT) / "id_remap.csv", index=False)
            publisher = importlib.import_module("step_build_publisher_context")
            mentions = pd.DataFrame(
                [
                    {
                        "repo_uid": "B152",
                        "repository_name": "NIDDK-CR R4R",
                        "repository_url": "https://repository.niddk.nih.gov/home",
                        "match_terms": "",
                        "publisher_family": "Example",
                        "journal_title": "",
                        "policy_title": "Policy",
                        "policy_url": "https://publisher.example/policy",
                        "policy_level": "publisher",
                        "mention_type": "recommended",
                        "data_domain": "clinical",
                        "access_model": "controlled",
                        "source_location": "table",
                        "evidence_note": "",
                        "date_checked": "2026-09-24",
                    }
                ]
            )
            matches, report = publisher._match_mentions(common.all_cards(), mentions)
            self.assertIn("R49", matches)
            self.assertEqual(report[0]["match_basis"], "reference_uid_remapped")


if __name__ == "__main__":
    unittest.main()
