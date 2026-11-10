from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

MODULES = {
    "common",
    "repository_status_schema",
    "publisher_context_schema",
    "step_build_repository_status",
    "step_build_publisher_context",
    "step_build_spine",
    "step_update_bmic_info",
    "step_update_dbgap_info",
    "step_update_publisher_info",
}


def configure(root: Path):
    for name in list(sys.modules):
        if name in MODULES or name.startswith("step_"):
            sys.modules.pop(name, None)
    for key in list(os.environ):
        if key.startswith("NIH_TRACKER_ROOT_") or key in {
            "NIH_TRACKER",
            "NIH_TRACKER_ENV",
            "NIH_TRACKER_RUN_ID",
            "NIH_TRACKER_ALLOW_PROJECT_OVERRIDE",
            "NIH_TRACKER_SHARED_CACHE_ROOT",
            "NIH_TRACKER_PROD_WRITE_CONFIRMATION",
        }:
            os.environ.pop(key, None)
    os.environ["NIH_TRACKER_ENV"] = "dev"
    os.environ["NIH_TRACKER_RUN_ID"] = "test-v17"
    for env in ("dev", "staging", "prod"):
        os.environ[f"NIH_TRACKER_ROOT_{env.upper()}"] = str(root / env)
    os.environ["NIH_TRACKER_SHARED_CACHE_ROOT"] = str(root / "shared" / "cache")
    return importlib.import_module("common")


def card(uid: str, name: str, homepage: str, status: str = "active") -> dict:
    return {
        "schema_version": "1.0",
        "repo_uid": uid,
        "kind": "repository",
        "identity": {
            "name": name,
            "homepage": homepage,
            "aliases": [],
            "status": {"value": status, "tier": "attestation", "as_of": "2026-09"},
        },
        "links": [],
        "blocks": {},
        "curation": {"review_state": "human_confirmed", "revision": 1},
    }


class RepositoryStatusTests(unittest.TestCase):
    def test_bmic_broad_vs_limited_and_curated_pause(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            common = configure(root)
            common.save_card("R1", card("R1", "Open Repository", "https://open.example/"))
            common.save_card("R2", card("R2", "Program Repository", "https://program.example/"))
            bmic = Path(common.INP) / "BMIC_download.csv"
            pd.DataFrame(
                [
                    {
                        "Repository_Name": "Open Repository",
                        "Open_Data_Submission": "Yes",
                        "Data_Submission_Policy": "https://open.example/deposit",
                        "Open_Time_Frame_for_Data_Deposit": "Yes",
                        "Access_to_Data": "https://open.example/access",
                        "Access_Type": "Open",
                        "Current_NIH_Funding_Support": "Yes",
                        "Sustained_Support": "Yes",
                        "Last_Reviewed": "2026-09-16",
                    },
                    {
                        "Repository_Name": "Program Repository",
                        "Open_Data_Submission": "No",
                        "Data_Submission_Policy": "Program-specific intake",
                        "Open_Time_Frame_for_Data_Deposit": "No",
                        "Access_to_Data": "https://program.example/access",
                        "Access_Type": "Controlled",
                        "Current_NIH_Funding_Support": "Yes",
                        "Sustained_Support": "Yes",
                        "Last_Reviewed": "2026-09-16",
                    },
                ]
            ).to_csv(bmic, index=False)
            curated = Path(common.INP) / "repository_status_curated.csv"
            pd.DataFrame(
                [
                    {
                        "repo_uid": "R2",
                        "operational_status": "temporarily_paused",
                        "operational_label": "Temporarily paused",
                        "submission_status": "not_accepting_new_submissions",
                        "accepting_user_contributed_data": "no",
                        "submission_policy_url": "https://program.example/status",
                        "submission_policy_text": "",
                        "submission_note": "No new deposits are being processed.",
                        "source_url": "https://program.example/status",
                        "source_as_of": "2026-09-16",
                        "note": "Service remains available for existing users.",
                    }
                ]
            ).to_csv(curated, index=False)
            module = importlib.import_module("step_build_repository_status")
            ok, coverage, _ = module.run(bmic_path=bmic, curated_path=curated, reference_overrides_path=root / "none.csv", apply=True)
            self.assertTrue(ok)
            self.assertEqual(coverage["applied"], 2)
            r1 = common.load_card("R1")["blocks"]["repository_status"]
            r2 = common.load_card("R2")["blocks"]["repository_status"]
            self.assertEqual(r1["submissions"]["status"], "accepting_broad_submissions")
            self.assertIs(r1["submissions"]["accepting_user_contributed_data"], True)
            self.assertEqual(r2["operational"]["status"], "temporarily_paused")
            self.assertEqual(r2["submissions"]["status"], "not_accepting_new_submissions")
            self.assertIs(r2["submissions"]["accepting_user_contributed_data"], False)
            ok2, coverage2, _ = module.run(bmic_path=bmic, curated_path=curated, reference_overrides_path=root / "none.csv", apply=True)
            self.assertTrue(ok2)
            self.assertEqual(coverage2["changed"], 0)
            self.assertEqual(coverage2["applied"], 0)


    def test_ambiguous_exact_bmic_name_blocks_assignment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            common = configure(root)
            first = card("R1", "Repository One", "https://one.example/")
            second = card("R2", "Repository Two", "https://two.example/")
            first["identity"]["aliases"] = ["Shared Repository"]
            second["identity"]["aliases"] = ["Shared Repository"]
            common.save_card("R1", first)
            common.save_card("R2", second)
            bmic = Path(common.INP) / "BMIC_download.csv"
            pd.DataFrame(
                [{"Repository_Name": "Shared Repository", "Open_Data_Submission": "Yes"}]
            ).to_csv(bmic, index=False)
            module = importlib.import_module("step_build_repository_status")
            ok, coverage, run_dir = module.run(
                bmic_path=bmic,
                curated_path=Path(common.INP) / "missing.csv",
                reference_overrides_path=root / "missing-overrides.csv",
                apply=True,
            )
            self.assertFalse(ok)
            self.assertEqual(coverage["applied"], 0)
            report = pd.read_csv(Path(run_dir) / "bmic_status_match_report.csv", dtype=str)
            self.assertEqual(report.loc[0, "status"], "AMBIGUOUS")
            self.assertNotIn("repository_status", common.load_card("R1").get("blocks", {}))

    def test_compatible_duplicate_bmic_rows_are_coalesced(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            common = configure(root)
            common.save_card("R1", card("R1", "Duplicate Repository", "https://duplicate.example/"))
            bmic = Path(common.INP) / "BMIC_download.csv"
            pd.DataFrame(
                [
                    {
                        "Repository_Name": "Duplicate Repository",
                        "Open_Data_Submission": "Yes",
                        "Data_Submission_Policy": "https://duplicate.example/deposit",
                        "Access_Type": "Controlled",
                        "Current_NIH_Funding_Support": "Yes",
                        "Sustained_Support": "Yes",
                        "Last_Reviewed": "2026-09-15",
                    },
                    {
                        "Repository_Name": "Duplicate Repository",
                        "Open_Data_Submission": "Yes",
                        "Access_to_Data": "https://duplicate.example/access",
                        "Access_Type": "Controlled",
                        "Current_NIH_Funding_Support": "Yes",
                        "Sustained_Support": "Yes",
                        "Last_Reviewed": "2026-09-16",
                    },
                ]
            ).to_csv(bmic, index=False)
            module = importlib.import_module("step_build_repository_status")
            ok, coverage, run_dir = module.run(
                bmic_path=bmic,
                curated_path=Path(common.INP) / "missing.csv",
                reference_overrides_path=root / "missing-overrides.csv",
                apply=True,
            )
            self.assertTrue(ok)
            self.assertEqual(coverage["bmic_coalesced_cards"], 1)
            self.assertEqual(coverage["applied"], 1)
            block = common.load_card("R1")["blocks"]["repository_status"]
            self.assertTrue(block["bmic"]["coalesced"])
            self.assertEqual(block["bmic"]["source_rows"], [2, 3])
            self.assertEqual(block["bmic"]["last_reviewed"], "2026-09-16")
            report = pd.read_csv(Path(run_dir) / "bmic_status_match_report.csv", dtype=str)
            self.assertEqual(set(report["status"]), {"COALESCED"})
            self.assertEqual(set(report["contributing_source_rows"]), {"2; 3"})

    def test_contradictory_duplicate_bmic_rows_remain_blocking(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            common = configure(root)
            common.save_card("R1", card("R1", "Conflicted Repository", "https://conflict.example/"))
            bmic = Path(common.INP) / "BMIC_download.csv"
            pd.DataFrame(
                [
                    {
                        "Repository_Name": "Conflicted Repository",
                        "Open_Data_Submission": "Yes",
                        "Access_Type": "Open",
                    },
                    {
                        "Repository_Name": "Conflicted Repository",
                        "Open_Data_Submission": "No",
                        "Access_Type": "Controlled",
                    },
                ]
            ).to_csv(bmic, index=False)
            module = importlib.import_module("step_build_repository_status")
            ok, coverage, run_dir = module.run(
                bmic_path=bmic,
                curated_path=Path(common.INP) / "missing.csv",
                reference_overrides_path=root / "missing-overrides.csv",
                apply=True,
            )
            self.assertFalse(ok)
            self.assertEqual(coverage["applied"], 0)
            report = pd.read_csv(Path(run_dir) / "bmic_status_match_report.csv", dtype=str)
            self.assertEqual(set(report["status"]), {"CONFLICT"})
            self.assertTrue(
                report["conflict_fields"].str.contains("open_data_submission").all()
            )
            self.assertNotIn("repository_status", common.load_card("R1").get("blocks", {}))

    def test_bmic_no_is_not_automatically_closed(self):
        schema = importlib.import_module("repository_status_schema")
        block = schema.normalize_repository_status(
            {"submissions": {"status": "accepting_limited_or_programmatic_submissions"}}
        )
        self.assertIsNone(block["submissions"]["accepting_user_contributed_data"])


class PublisherContextTests(unittest.TestCase):
    def test_policy_posture_is_not_collapsed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            common = configure(root)
            common.save_card("R1", card("R1", "Domain Repository", "https://domain.example/"))
            common.save_card("R2", card("R2", "General Repository", "https://general.example/"))
            reference = Path(common.PROJECT) / "reference"
            reference.mkdir(parents=True, exist_ok=True)
            sources = reference / "publisher_policy_sources.csv"
            pd.DataFrame(
                [
                    {
                        "publisher_family": "Publisher A",
                        "policy_title": "Data policy",
                        "policy_url": "https://publisher.example/policy",
                        "expected_terms": "repository",
                        "date_checked": "2026-09-16",
                        "content_sha256": "abc",
                        "snapshot_path": "reference/snapshot.html.gz",
                        "http_status": "200",
                        "final_url": "https://publisher.example/policy",
                        "refresh_status": "current",
                    }
                ]
            ).to_csv(sources, index=False)
            mentions = reference / "publisher_repository_mentions.csv"
            pd.DataFrame(
                [
                    {
                        "publisher_family": "Publisher A",
                        "journal_title": "",
                        "policy_title": "Data policy",
                        "policy_url": "https://publisher.example/policy",
                        "policy_level": "required for genomics",
                        "repository_name": "Domain Repository",
                        "repository_url": "https://domain.example/",
                        "mention_type": "mandatory",
                        "data_domain": "genomics",
                        "access_model": "mixed",
                        "source_location": "Genomics",
                        "evidence_note": "Required for the stated data type.",
                        "date_checked": "2026-09-16",
                        "repo_uid": "R1",
                        "match_terms": "",
                    },
                    {
                        "publisher_family": "Publisher A",
                        "journal_title": "",
                        "policy_title": "Data policy",
                        "policy_url": "https://publisher.example/policy",
                        "policy_level": "examples",
                        "repository_name": "General Repository",
                        "repository_url": "https://general.example/",
                        "mention_type": "example",
                        "data_domain": "general",
                        "access_model": "open",
                        "source_location": "Examples",
                        "evidence_note": "Listed as an example only.",
                        "date_checked": "2026-09-16",
                        "repo_uid": "R2",
                        "match_terms": "",
                    },
                ]
            ).to_csv(mentions, index=False)
            module = importlib.import_module("step_build_publisher_context")
            ok, coverage, _ = module.run(mentions_path=mentions, sources_path=sources, apply=True)
            self.assertTrue(ok)
            self.assertEqual(coverage["applied"], 2)
            p1 = common.load_card("R1")["blocks"]["publisher_context"]
            p2 = common.load_card("R2")["blocks"]["publisher_context"]
            self.assertTrue(p1["publisher_recommended"])
            self.assertTrue(p1["publisher_mandated"])
            self.assertTrue(p2["publisher_mentioned"])
            self.assertFalse(p2["publisher_recommended"])


class UpdateScriptTests(unittest.TestCase):
    def test_local_bmic_and_dbgap_updates_are_atomic_and_validated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            common = configure(root)
            bmic_source = root / "BMIC.csv"
            pd.DataFrame(
                [{"Repository Name": "Example", "Open Data Submission": "Yes"} for _ in range(3)]
            ).to_csv(bmic_source, index=False)
            bmic_module = importlib.import_module("step_update_bmic_info")
            ok, report, _ = bmic_module.run(
                source_file=bmic_source,
                source_url="",
                destination=Path(common.INP) / "BMIC_download.csv",
                minimum_rows=1,
                maximum_drop_fraction=0.5,
                apply=True,
            )
            self.assertTrue(ok)
            self.assertTrue(report["applied"])

            source = root / "dbgap.csv"
            rows = []
            for index in range(3):
                rows.append({column: "" for column in importlib.import_module("step_update_dbgap_info").COLUMNS})
                rows[-1].update({"accession": f"phs{index:06d}.v1.p1", "name": f"Study {index}", "Release Date": "2026-09-16"})
            pd.DataFrame(rows).to_csv(source, index=False)
            dbg = importlib.import_module("step_update_dbgap_info")
            destination = Path(common.PROJECT) / "reference" / "dbGaP_Studies.csv"
            ok2, report2, _ = dbg.run(
                mode="csv",
                source_file=source,
                endpoint=dbg.FHIR_ENDPOINT,
                destination=destination,
                minimum_rows=1,
                maximum_drop_fraction=0.5,
                apply=True,
            )
            self.assertTrue(ok2)
            self.assertTrue(report2["applied"])
            self.assertEqual(len(pd.read_csv(destination)), 3)


    def test_duplicate_dbgap_accessions_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            common = configure(root)
            dbg = importlib.import_module("step_update_dbgap_info")
            source = root / "dbgap-duplicates.csv"
            rows = []
            for index in range(2):
                row = {column: "" for column in dbg.COLUMNS}
                row.update({"accession": "phs000001.v1.p1", "name": f"Study {index}"})
                rows.append(row)
            pd.DataFrame(rows).to_csv(source, index=False)
            destination = Path(common.PROJECT) / "reference" / "dbGaP_Studies.csv"
            ok, report, _ = dbg.run(
                mode="csv",
                source_file=source,
                endpoint=dbg.FHIR_ENDPOINT,
                destination=destination,
                minimum_rows=1,
                maximum_drop_fraction=0.5,
                apply=True,
            )
            self.assertFalse(ok)
            self.assertFalse(report["applied"])
            self.assertFalse(destination.exists())

    def test_publisher_source_refresh_flags_content_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            common = configure(root)
            reference = Path(common.PROJECT) / "reference"
            reference.mkdir(parents=True, exist_ok=True)
            sources = reference / "publisher_policy_sources.csv"
            pd.DataFrame(
                [
                    {
                        "publisher_family": "Publisher A",
                        "policy_title": "Policy",
                        "policy_url": "https://publisher.example/policy",
                        "expected_terms": "repository;deposit",
                        "date_checked": "2026-09-01",
                        "content_sha256": "0" * 64,
                        "snapshot_path": "",
                        "http_status": "",
                        "final_url": "",
                        "refresh_status": "current",
                    }
                ]
            ).to_csv(sources, index=False)
            module = importlib.import_module("step_update_publisher_info")
            payload = b"<html><body>repository deposit policy</body></html>"
            with mock.patch.object(module, "_fetch", return_value=(payload, 200, "https://publisher.example/policy", "text/html")):
                ok, coverage, _ = module.run(sources_path=sources, apply=True, continue_on_error=False)
            self.assertTrue(ok)
            self.assertEqual(coverage["changed_pages"], 1)
            self.assertEqual(coverage["review_required_pages"], 1)
            updated = pd.read_csv(sources, dtype=str, keep_default_na=False)
            self.assertEqual(updated.loc[0, "refresh_status"], "review_required")
            self.assertTrue((Path(common.PROJECT) / updated.loc[0, "snapshot_path"]).is_file())


class ReleaseSurfaceTests(unittest.TestCase):
    def test_frontend_and_notebook_generator_expose_new_domains(self):
        frontend = (ROOT / "frontend.html").read_text(encoding="utf-8")
        self.assertIn("Operational and submission status", frontend)
        self.assertIn("Publisher repository policies", frontend)
        self.assertIn("fOperational", frontend)
        self.assertIn("fPublisher", frontend)
        generator = (ROOT / "build_notebooks.py").read_text(encoding="utf-8")
        self.assertIn("step_build_repository_status.py", generator)
        self.assertIn("step_build_publisher_context.py", generator)
        self.assertIn("step_update_dbgap_info.py", generator)
        self.assertIn("step_update_publisher_info.py", generator)

    def test_release_publisher_inventory_uses_controlled_postures(self):
        schema = importlib.import_module("publisher_context_schema")
        frame = pd.read_csv(ROOT / "reference" / "publisher_repository_mentions.csv", dtype=str, keep_default_na=False)
        self.assertGreater(len(frame), 20)
        self.assertTrue(set(frame["mention_type"]).issubset(schema.MENTION_TYPES))
        self.assertIn("mandatory", set(frame["mention_type"]))
        self.assertIn("recommended", set(frame["mention_type"]))
        self.assertIn("acceptable_fallback", set(frame["mention_type"]))


if __name__ == "__main__":
    unittest.main()
