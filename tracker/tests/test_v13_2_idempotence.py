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
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def clear_modules() -> None:
    for name in list(sys.modules):
        if name in {
            "common",
            "identity_hygiene",
            "step_build_links",
            "step_build_names",
            "step_clean_aliases",
        } or name.startswith("step_"):
            sys.modules.pop(name, None)


def configure(root: Path, env: str = "dev"):
    clear_modules()
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
    os.environ["NIH_TRACKER_ENV"] = env
    for name in ("dev", "staging", "prod"):
        os.environ[f"NIH_TRACKER_ROOT_{name.upper()}"] = str(root / name)
    os.environ["NIH_TRACKER_SHARED_CACHE_ROOT"] = str(root / "shared" / "cache")
    return importlib.import_module("common")


def basic_card(uid: str, name: str) -> dict:
    return {
        "schema_version": "1.0",
        "repo_uid": uid,
        "kind": "repository",
        "identity": {"name": name},
        "links": [],
        "blocks": {},
        "curation": {"review_state": "needs_review"},
    }


class IdentityIdempotenceTests(unittest.TestCase):
    def test_build_names_normalizes_before_comparison_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            configure(Path(temporary))
            hygiene = importlib.import_module("identity_hygiene")
            builder = importlib.import_module("step_build_names")
            card = basic_card("R1", "Cell Image Library (CIL)")
            local_index = {
                "cellimagelibrary": {
                    "Cell Image Library",
                    "Cell Image Library (CIL)",
                    "CIL",
                    "Cell Imaging Library Historical Name",
                }
            }
            first = builder.enrich_identity(card, local_index)
            self.assertEqual(first["abbreviation"], "CIL")
            self.assertEqual(first["full_name"], "Cell Image Library")
            self.assertEqual(first.get("aliases"), ["Cell Imaging Library Historical Name"])
            self.assertEqual(hygiene.identity_hygiene_issues(first), [])

            second_card = basic_card("R1", "Cell Image Library (CIL)")
            second_card["identity"] = first
            second = builder.enrich_identity(second_card, local_index)
            self.assertEqual(second, first)

    def test_clean_identity_noop_is_valid(self):
        with tempfile.TemporaryDirectory() as temporary:
            configure(Path(temporary))
            hygiene = importlib.import_module("identity_hygiene")
            identity = {
                "name": "NIMH Data Archive",
                "abbreviation": "NDA",
                "aliases": ["National Database for Autism Research"],
                "name_provenance": {
                    "National Database for Autism Research": {
                        "source": "registry_export",
                        "as_of": "2026-09",
                    }
                },
            }
            normalized, dropped, promoted = hygiene.clean_identity(identity)
            self.assertEqual(normalized, identity)
            self.assertEqual(dropped, [])
            self.assertIsNone(promoted)
            self.assertEqual(hygiene.identity_hygiene_issues(normalized), [])

    def test_noop_cli_steps_record_passing_gates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            common = configure(root)
            common.save_card("R1", basic_card("R1", "Example Resource"))
            inputs = Path(common.INP)
            inputs.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(
                [{"Resource Name": "Example Resource", "Abbreviations": "", "Synonyms": ""}]
            ).to_csv(inputs / "Tools467results.csv", index=False)
            pd.DataFrame(
                [
                    {
                        "Repository_Name": "Example Resource",
                        "FS_Name": "",
                        "RE3_Name": "",
                        "SC_Name": "",
                        "SC_Synonyms": "",
                    }
                ]
            ).to_csv(inputs / "final_ultimate_enhanced_repos.csv", index=False)
            pd.DataFrame([{"Repository_Name": "Example Resource"}]).to_csv(
                inputs / "BMIC_download.csv", index=False
            )

            env = os.environ.copy()
            for script in ("step_build_names.py", "step_clean_aliases.py"):
                result = subprocess.run(
                    [sys.executable, str(SCRIPTS / script)],
                    env=env,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
                self.assertIn("no changes required", result.stdout.lower())
                self.assertNotIn("[FAIL]", result.stdout)

            ledger = pd.read_csv(Path(common.PROJECT) / "run_ledger.csv")
            recent = ledger.tail(2)
            self.assertTrue(recent["gate_passed"].astype(str).str.lower().eq("true").all())
            outputs = [json.loads(value) for value in recent["outputs"]]
            self.assertTrue(all(value.get("no_op") is True for value in outputs))


class CrosswalkPreflightTests(unittest.TestCase):
    def test_stale_target_is_reported_with_candidates_and_remap_passes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            common = configure(root)
            common.save_card("R200", basic_card("R200", "Child Trauma Data Archives"))
            inputs = Path(common.INP)
            inputs.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(
                [
                    {
                        "csv_id": "129",
                        "repo_uid": "R129",
                        "probe_name": "Child Trauma Prevention and Treatment (CTPT) Data Archive",
                        "registry_name": "Child Trauma Prevention and Treatment (CTPT) Data Archive",
                        "match_type": "exact_base",
                        "confidence": "1",
                    }
                ]
            ).to_csv(inputs / "crosswalk.csv", index=False)

            links = importlib.import_module("step_build_links")
            mapping, report = links.load_crosswalk_targets(common.all_cards(), uid_remap={})
            self.assertEqual(mapping, {})
            self.assertEqual(report.iloc[0]["status"], "STALE")
            self.assertIn("R200", report.iloc[0]["candidate_uids"])

            mapping, report = links.load_crosswalk_targets(
                common.all_cards(), uid_remap={"R129": "R200"}
            )
            self.assertEqual(report.iloc[0]["status"], "REMAPPED")
            self.assertEqual(mapping[common.slug("Child Trauma Prevention and Treatment (CTPT) Data Archive")]["resolved_uid"], "R200")

    def test_preflight_failure_is_persisted_before_link_planning(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            common = configure(root)
            common.save_card("R1", basic_card("R1", "Other Resource"))
            inputs = Path(common.INP)
            inputs.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(
                [{"csv_id": "129", "repo_uid": "R129", "registry_name": "Missing Resource"}]
            ).to_csv(inputs / "crosswalk.csv", index=False)
            links = importlib.import_module("step_build_links")
            self.assertFalse(links.run_crosswalk_preflight(common.all_cards(), {}))
            reports = list((Path(common.RUNS) / "build_links_preflight").glob("*/crosswalk_integrity.csv"))
            self.assertEqual(len(reports), 1)
            ledger = pd.read_csv(Path(common.PROJECT) / "run_ledger.csv")
            self.assertEqual(ledger.iloc[-1]["step"], "build_links_preflight")
            self.assertEqual(str(ledger.iloc[-1]["gate_passed"]).lower(), "false")


if __name__ == "__main__":
    unittest.main()
