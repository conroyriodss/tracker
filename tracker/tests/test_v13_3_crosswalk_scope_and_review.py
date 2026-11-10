from __future__ import annotations

import importlib
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


def clear_modules() -> None:
    for name in list(sys.modules):
        if name in {
            "common",
            "review_widgets",
            "step_build_links",
            "step_apply_crosswalk_adjudications",
        }:
            sys.modules.pop(name, None)


def configure(root: Path):
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
    os.environ["NIH_TRACKER_ENV"] = "dev"
    for name in ("dev", "staging", "prod"):
        os.environ[f"NIH_TRACKER_ROOT_{name.upper()}"] = str(root / name)
    os.environ["NIH_TRACKER_SHARED_CACHE_ROOT"] = str(root / "shared" / "cache")
    return importlib.import_module("common")


def card(uid: str, name: str) -> dict:
    return {
        "schema_version": "1.0",
        "repo_uid": uid,
        "kind": "repository",
        "identity": {"name": name},
        "links": [],
        "blocks": {},
        "curation": {"review_state": "needs_review"},
    }


class ScopeAwareCrosswalkTests(unittest.TestCase):
    def test_probe_only_rows_are_reported_but_do_not_block_build_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            common = configure(Path(temporary))
            common.save_card("R1", card("R1", "Registry Resource"))
            inputs = Path(common.INP)
            inputs.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(
                [
                    {
                        "csv_id": "1",
                        "repo_uid": "R999",
                        "probe_name": "Probe-only resource",
                        "registry_name": "",
                    },
                    {
                        "csv_id": "2",
                        "repo_uid": "R1",
                        "probe_name": "Registry Resource",
                        "registry_name": "Registry Resource",
                    },
                ]
            ).to_csv(inputs / "crosswalk.csv", index=False)

            links = importlib.import_module("step_build_links")
            mapping, report = links.load_crosswalk_targets(common.all_cards(), uid_remap={})
            self.assertEqual(report["status"].tolist(), ["NOT_APPLICABLE", "ACTIVE"])
            self.assertEqual(len(mapping), 1)
            self.assertTrue(links.run_crosswalk_preflight(common.all_cards(), {}))

            ledger = pd.read_csv(Path(common.PROJECT) / "run_ledger.csv")
            outputs = __import__("json").loads(ledger.iloc[-1]["outputs"])
            self.assertEqual(outputs["not_applicable"], 1)
            self.assertEqual(outputs["stale"], 0)

    def test_triggering_shape_yields_82_not_applicable_and_15_stale_not_97_invalid(self):
        with tempfile.TemporaryDirectory() as temporary:
            common = configure(Path(temporary))
            common.save_card("R1", card("R1", "One Active Resource"))
            inputs = Path(common.INP)
            inputs.mkdir(parents=True, exist_ok=True)
            rows = []
            for number in range(1, 83):
                rows.append(
                    {
                        "csv_id": str(number),
                        "repo_uid": f"R{1000 + number}",
                        "probe_name": f"Probe-only {number}",
                        "registry_name": "",
                    }
                )
            for number in range(83, 98):
                rows.append(
                    {
                        "csv_id": str(number),
                        "repo_uid": f"R{1000 + number}",
                        "probe_name": f"Registry stale {number}",
                        "registry_name": f"Registry stale {number}",
                    }
                )
            pd.DataFrame(rows).to_csv(inputs / "crosswalk.csv", index=False)
            links = importlib.import_module("step_build_links")
            _, report = links.load_crosswalk_targets(common.all_cards(), uid_remap={})
            counts = report["status"].value_counts().to_dict()
            self.assertEqual(counts.get("NOT_APPLICABLE"), 82)
            self.assertEqual(counts.get("STALE"), 15)
            self.assertEqual(counts.get("INVALID_ROW", 0), 0)

    def test_populated_name_absent_from_selected_registry_is_nonblocking(self):
        with tempfile.TemporaryDirectory() as temporary:
            common = configure(Path(temporary))
            common.save_card("R1", card("R1", "Current Resource"))
            inputs = Path(common.INP)
            inputs.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(
                [
                    {
                        "csv_id": "8",
                        "repo_uid": "R8",
                        "probe_name": "Historical Resource",
                        "registry_name": "Historical Resource",
                    }
                ]
            ).to_csv(inputs / "crosswalk.csv", index=False)
            registry_path = inputs / "final_ultimate_enhanced_repos.csv"
            pd.DataFrame(
                [{"Repository_Name": "Current Resource"}]
            ).to_csv(registry_path, index=False)

            links = importlib.import_module("step_build_links")
            self.assertTrue(
                links.run_crosswalk_preflight(
                    common.all_cards(), {}, registry_path=registry_path
                )
            )
            report_path = next(
                (Path(common.RUNS) / "build_links_preflight").glob(
                    "*/crosswalk_integrity.csv"
                )
            )
            report = pd.read_csv(report_path, dtype=str).fillna("")
            self.assertEqual(report.iloc[0]["status"], "NOT_APPLICABLE")
            self.assertEqual(
                report.iloc[0]["scope_reason"], "not_in_current_registry"
            )

    def test_registry_linked_stale_row_still_blocks(self):
        with tempfile.TemporaryDirectory() as temporary:
            common = configure(Path(temporary))
            common.save_card("R1", card("R1", "Active Resource"))
            inputs = Path(common.INP)
            inputs.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(
                [
                    {
                        "csv_id": "9",
                        "repo_uid": "R9",
                        "probe_name": "Stale Resource",
                        "registry_name": "Stale Resource",
                    }
                ]
            ).to_csv(inputs / "crosswalk.csv", index=False)
            links = importlib.import_module("step_build_links")
            self.assertFalse(links.run_crosswalk_preflight(common.all_cards(), {}))
            report_path = next(
                (Path(common.RUNS) / "build_links_preflight").glob("*/crosswalk_integrity.csv")
            )
            report = pd.read_csv(report_path, dtype=str).fillna("")
            self.assertEqual(report.iloc[0]["status"], "STALE")
            self.assertTrue(report.iloc[0]["source_row_fingerprint"])
            self.assertTrue(report.iloc[0]["crosswalk_sha256"])


class CrosswalkReviewApplyTests(unittest.TestCase):
    def _make_stale_report(self, common, old_uid="R9") -> Path:
        inputs = Path(common.INP)
        inputs.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(
            [
                {
                    "csv_id": "9",
                    "repo_uid": old_uid,
                    "probe_name": "Historical Registry Label",
                    "registry_name": "Historical Registry Label",
                    "match_type": "exact_base",
                    "confidence": "1",
                }
            ]
        ).to_csv(inputs / "crosswalk.csv", index=False)
        registry_path = inputs / "final_ultimate_enhanced_repos.csv"
        pd.DataFrame(
            [{"Repository_Name": "Historical Registry Label"}]
        ).to_csv(registry_path, index=False)
        links = importlib.import_module("step_build_links")
        self.assertFalse(
            links.run_crosswalk_preflight(
                common.all_cards(), {}, registry_path=registry_path
            )
        )
        return max(
            (Path(common.RUNS) / "build_links_preflight").glob("*/crosswalk_integrity.csv"),
            key=lambda path: path.stat().st_mtime,
        )

    def test_workbench_normalization_and_crosswalk_uid_replacement(self):
        with tempfile.TemporaryDirectory() as temporary:
            common = configure(Path(temporary))
            common.save_card("R1", card("R1", "Current Registry Resource"))
            report_path = self._make_stale_report(common)

            review = importlib.import_module("review_widgets")
            kind, queue = review.normalize_review_file(report_path)
            self.assertEqual(kind, "crosswalk")
            self.assertEqual(len(queue), 1)
            row = queue.iloc[0].to_dict()
            row.update(
                {
                    "decision": "REPLACE_CROSSWALK_UID",
                    "selected_repo_uid": "R1",
                    "note": "Curator confirmed the registry row belongs to active card R1.",
                }
            )
            decisions = review.prepare_decisions(
                "crosswalk", [row], reviewer="tester", reviewed_at="2026-09-10T00:00:00"
            )
            path, inserted, updated = review.upsert_adjudications("crosswalk", decisions)
            self.assertEqual((inserted, updated), (1, 0))

            env = os.environ.copy()
            dry = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "step_apply_crosswalk_adjudications.py"),
                    "--adjudications",
                    path.name,
                ],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(dry.returncode, 0, msg=dry.stdout + dry.stderr)
            self.assertIn("DRY RUN", dry.stdout)

            applied = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "step_apply_crosswalk_adjudications.py"),
                    "--adjudications",
                    path.name,
                    "--apply",
                ],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(applied.returncode, 0, msg=applied.stdout + applied.stderr)
            crosswalk = pd.read_csv(Path(common.INP) / "crosswalk.csv", dtype=str).fillna("")
            self.assertEqual(crosswalk.iloc[0]["repo_uid"], "R1")
            receipts = pd.read_csv(path, dtype=str).fillna("")
            self.assertEqual(receipts.iloc[0]["apply_status"], "applied:crosswalk_uid_replaced")
            links = importlib.import_module("step_build_links")
            self.assertTrue(links.run_crosswalk_preflight(common.all_cards(), {}))

    def test_historical_remap_preserves_crosswalk_and_resolves_preflight(self):
        with tempfile.TemporaryDirectory() as temporary:
            common = configure(Path(temporary))
            common.save_card("R2", card("R2", "Surviving Resource"))
            report_path = self._make_stale_report(common, old_uid="R9")
            review = importlib.import_module("review_widgets")
            kind, queue = review.normalize_review_file(report_path)
            row = queue.iloc[0].to_dict()
            row.update(
                {
                    "decision": "RECORD_ID_REMAP",
                    "selected_repo_uid": "R2",
                    "note": "Transaction history establishes R9 as the retired UID for R2.",
                }
            )
            decisions = review.prepare_decisions(
                kind, [row], reviewer="tester", reviewed_at="2026-09-10T00:00:00"
            )
            path, _, _ = review.upsert_adjudications(kind, decisions)
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "step_apply_crosswalk_adjudications.py"),
                    "--adjudications",
                    path.name,
                    "--apply",
                ],
                env=os.environ.copy(),
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
            crosswalk = pd.read_csv(Path(common.INP) / "crosswalk.csv", dtype=str).fillna("")
            self.assertEqual(crosswalk.iloc[0]["repo_uid"], "R9")
            remap = pd.read_csv(Path(common.PROJECT) / "id_remap.csv", dtype=str).fillna("")
            self.assertEqual(remap.iloc[0]["old_uid"], "R9")
            self.assertEqual(remap.iloc[0]["new_uid"], "R2")
            links = importlib.import_module("step_build_links")
            links = importlib.reload(links)
            self.assertTrue(
                links.run_crosswalk_preflight(common.all_cards(), links.load_uid_remap())
            )

    def test_apply_refuses_crosswalk_drift_after_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            common = configure(Path(temporary))
            common.save_card("R1", card("R1", "Current Registry Resource"))
            report_path = self._make_stale_report(common)
            review = importlib.import_module("review_widgets")
            kind, queue = review.normalize_review_file(report_path)
            row = queue.iloc[0].to_dict()
            row.update(
                {
                    "decision": "REPLACE_CROSSWALK_UID",
                    "selected_repo_uid": "R1",
                    "note": "Evidence note.",
                }
            )
            decisions = review.prepare_decisions(kind, [row], reviewer="tester")
            path, _, _ = review.upsert_adjudications(kind, decisions)

            crosswalk_path = Path(common.INP) / "crosswalk.csv"
            crosswalk = pd.read_csv(crosswalk_path, dtype=str).fillna("")
            crosswalk.loc[0, "probe_name"] = "Changed after review"
            crosswalk.to_csv(crosswalk_path, index=False)

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "step_apply_crosswalk_adjudications.py"),
                    "--adjudications",
                    path.name,
                    "--apply",
                ],
                env=os.environ.copy(),
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2, msg=result.stdout + result.stderr)
            self.assertIn("source row fingerprint resolves to 0 current crosswalk rows", result.stdout)
            current = pd.read_csv(crosswalk_path, dtype=str).fillna("")
            self.assertEqual(current.iloc[0]["repo_uid"], "R9")


if __name__ == "__main__":
    unittest.main()
