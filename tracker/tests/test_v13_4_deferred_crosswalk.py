from __future__ import annotations

import hashlib
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


def write_sources(common, rows: list[dict[str, str]]) -> Path:
    inputs = Path(common.INP)
    inputs.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(inputs / "crosswalk.csv", index=False)
    registry_path = inputs / "final_ultimate_enhanced_repos.csv"
    pd.DataFrame(
        [{"Repository_Name": row["registry_name"]} for row in rows]
    ).to_csv(registry_path, index=False)
    return registry_path


def newest_report(common) -> Path:
    return max(
        (Path(common.RUNS) / "build_links_preflight").glob(
            "*/crosswalk_integrity.csv"
        ),
        key=lambda path: path.stat().st_mtime_ns,
    )


def save_decision(report_path: Path, *, decision: str, target: str = "", note: str = "") -> Path:
    review = importlib.import_module("review_widgets")
    kind, queue = review.normalize_review_file(report_path)
    row = queue.iloc[0].to_dict()
    row.update(
        {
            "decision": decision,
            "selected_repo_uid": target,
            "note": note,
        }
    )
    decisions = review.prepare_decisions(
        kind,
        [row],
        reviewer="unit-tester",
        reviewed_at="2026-09-10T20:00:00+00:00",
    )
    path, _, _ = review.upsert_adjudications(kind, decisions)
    return path


class DeferredCrosswalkPolicyTests(unittest.TestCase):
    def test_unreceipted_defer_remains_blocking(self):
        with tempfile.TemporaryDirectory() as temporary:
            common = configure(Path(temporary))
            common.save_card("R1", card("R1", "Deferred Resource"))
            registry_path = write_sources(
                common,
                [
                    {
                        "csv_id": "9",
                        "repo_uid": "R9",
                        "probe_name": "Deferred Resource",
                        "registry_name": "Deferred Resource",
                    }
                ],
            )
            links = importlib.import_module("step_build_links")
            self.assertFalse(
                links.run_crosswalk_preflight(
                    common.all_cards(), {}, registry_path=registry_path
                )
            )
            save_decision(newest_report(common), decision="DEFER")

            # Saving a widget choice is not sufficient. Only an applied receipt waives
            # the completeness gate; the mutation-safety classification remains STALE.
            self.assertFalse(
                links.run_crosswalk_preflight(
                    common.all_cards(), {}, registry_path=registry_path
                )
            )
            report = pd.read_csv(newest_report(common), dtype=str).fillna("")
            self.assertEqual(report.iloc[0]["status"], "STALE")

    def test_receipted_defer_is_nonblocking_and_prevents_fallback_matching(self):
        with tempfile.TemporaryDirectory() as temporary:
            common = configure(Path(temporary))
            # The name deliberately matches an active card. DEFER must still withhold
            # the registry row rather than fall through to exact-name matching.
            common.save_card("R1", card("R1", "Deferred Resource"))
            registry_path = write_sources(
                common,
                [
                    {
                        "csv_id": "9",
                        "repo_uid": "R9",
                        "probe_name": "Deferred Resource",
                        "registry_name": "Deferred Resource",
                    }
                ],
            )
            links = importlib.import_module("step_build_links")
            self.assertFalse(
                links.run_crosswalk_preflight(
                    common.all_cards(), {}, registry_path=registry_path
                )
            )
            adjudications_path = save_decision(newest_report(common), decision="DEFER")

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "step_apply_crosswalk_adjudications.py"),
                    "--adjudications",
                    adjudications_path.name,
                    "--apply",
                ],
                env=os.environ.copy(),
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
            self.assertIn("reviewed nonblocking deferral", result.stdout)

            receipts = pd.read_csv(adjudications_path, dtype=str).fillna("")
            self.assertEqual(
                receipts.iloc[0]["apply_status"],
                "deferred:reviewed_nonblocking",
            )
            self.assertTrue(
                links.run_crosswalk_preflight(
                    common.all_cards(), {}, registry_path=registry_path
                )
            )
            report = pd.read_csv(newest_report(common), dtype=str).fillna("")
            self.assertEqual(report.iloc[0]["status"], "DEFERRED")
            self.assertEqual(
                report.iloc[0]["scope_reason"],
                "current_registry_reviewed_defer",
            )

            registry = pd.read_csv(registry_path, dtype=str).fillna("")
            registry_hash = hashlib.sha256(registry_path.read_bytes()).hexdigest()
            uid_of, review = links.build_join(
                registry,
                common.all_cards(),
                uid_remap={},
                registry_file=registry_path.name,
                registry_sha256=registry_hash,
            )
            self.assertEqual(uid_of, {})
            self.assertEqual(review.iloc[0]["how"], "DEFERRED_CROSSWALK_UID")
            self.assertEqual(review.iloc[0]["matched_card"], None)

            ledger = pd.read_csv(Path(common.PROJECT) / "run_ledger.csv", dtype=str).fillna("")
            outputs = json.loads(ledger.iloc[-1]["outputs"])
            self.assertEqual(outputs["deferred"], 1)
            self.assertEqual(outputs["stale"], 0)
            issues = pd.read_csv(Path(common.PROJECT) / "issues_log.csv", dtype=str).fillna("")
            self.assertEqual(issues.iloc[0]["issue_type"], "crosswalk_target_deferred")
            self.assertEqual(issues.iloc[0]["base_severity"], "advisory")

            # Existing v13.3 receipts used this status. They remain valid so the user's
            # thirteen already-receipted decisions do not require re-entry.
            receipts.loc[0, "apply_status"] = "deferred:no_source_change"
            receipts.to_csv(adjudications_path, index=False)
            self.assertTrue(
                links.run_crosswalk_preflight(
                    common.all_cards(), {}, registry_path=registry_path
                )
            )
            legacy_report = pd.read_csv(newest_report(common), dtype=str).fillna("")
            self.assertEqual(legacy_report.iloc[0]["status"], "DEFERRED")

    def test_defer_cannot_waive_crosswalk_conflict(self):
        with tempfile.TemporaryDirectory() as temporary:
            common = configure(Path(temporary))
            common.save_card("R1", card("R1", "Active One"))
            registry_path = write_sources(
                common,
                [
                    {
                        "csv_id": "1",
                        "repo_uid": "R8",
                        "probe_name": "Conflicted Resource",
                        "registry_name": "Conflicted Resource",
                    },
                    {
                        "csv_id": "2",
                        "repo_uid": "R9",
                        "probe_name": "Conflicted Resource",
                        "registry_name": "Conflicted Resource",
                    },
                ],
            )
            links = importlib.import_module("step_build_links")
            self.assertFalse(
                links.run_crosswalk_preflight(
                    common.all_cards(), {}, registry_path=registry_path
                )
            )
            report = pd.read_csv(newest_report(common), dtype=str).fillna("")
            self.assertTrue((report["status"] == "CONFLICT").all())
            adjudications_path = save_decision(newest_report(common), decision="DEFER")
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "step_apply_crosswalk_adjudications.py"),
                    "--adjudications",
                    adjudications_path.name,
                    "--apply",
                ],
                env=os.environ.copy(),
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2, msg=result.stdout + result.stderr)
            self.assertIn("DEFER is permitted only for a structurally valid STALE target", result.stdout)
            receipts = pd.read_csv(adjudications_path, dtype=str).fillna("")
            self.assertEqual(receipts.iloc[0]["apply_status"], "")


class IncrementalProductionShapeTests(unittest.TestCase):
    def test_applied_replacements_preserve_defers_and_later_batch_rebases(self):
        with tempfile.TemporaryDirectory() as temporary:
            common = configure(Path(temporary))
            rows = []
            for number in range(1, 6):
                common.save_card(
                    f"R{number}",
                    card(f"R{number}", f"Resource {number}"),
                )
                rows.append(
                    {
                        "csv_id": str(number),
                        "repo_uid": f"OLD{number}",
                        "probe_name": f"Resource {number}",
                        "registry_name": f"Resource {number}",
                    }
                )
            registry_path = write_sources(common, rows)
            links = importlib.import_module("step_build_links")
            self.assertFalse(
                links.run_crosswalk_preflight(
                    common.all_cards(), {}, registry_path=registry_path
                )
            )
            original_report = newest_report(common)
            review = importlib.import_module("review_widgets")
            kind, queue = review.normalize_review_file(original_report)
            decisions = []
            for _, source_row in queue.iterrows():
                row = source_row.to_dict()
                number = int(row["csv_id"])
                if number <= 2:
                    row.update(
                        {
                            "decision": "REPLACE_CROSSWALK_UID",
                            "selected_repo_uid": f"R{number}",
                            "note": f"Identity evidence for resource {number}.",
                        }
                    )
                else:
                    row.update(
                        {
                            "decision": "DEFER",
                            "selected_repo_uid": "",
                            "note": "",
                        }
                    )
                decisions.extend(
                    review.prepare_decisions(
                        kind,
                        [row],
                        reviewer="unit-tester",
                        reviewed_at="2026-09-10T20:00:00+00:00",
                    )
                )
            adjudications_path, _, _ = review.upsert_adjudications(kind, decisions)
            first_apply = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "step_apply_crosswalk_adjudications.py"),
                    "--adjudications",
                    adjudications_path.name,
                    "--apply",
                ],
                env=os.environ.copy(),
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(
                first_apply.returncode, 0, msg=first_apply.stdout + first_apply.stderr
            )

            # Simulate the exact legacy receipt vocabulary already present in the
            # production adjudication ledger.
            receipts = pd.read_csv(adjudications_path, dtype=str).fillna("")
            receipts.loc[
                receipts["decision"] == "DEFER", "apply_status"
            ] = "deferred:no_source_change"
            receipts.to_csv(adjudications_path, index=False)

            self.assertTrue(
                links.run_crosswalk_preflight(
                    common.all_cards(), {}, registry_path=registry_path
                )
            )
            after_first = pd.read_csv(newest_report(common), dtype=str).fillna("")
            self.assertEqual((after_first["status"] == "ACTIVE").sum(), 2)
            self.assertEqual((after_first["status"] == "DEFERRED").sum(), 3)

            # Reopen Resource 3 from the original preflight. Its recorded whole-file
            # hash is stale because Resources 1 and 2 were already corrected, but its
            # own logical row is unchanged and must rebase safely.
            resource_three = queue.loc[queue["csv_id"] == "3"].iloc[0].to_dict()
            resource_three.update(
                {
                    "decision": "REPLACE_CROSSWALK_UID",
                    "selected_repo_uid": "R3",
                    "note": "Later evidence establishes resource 3 identity.",
                }
            )
            reopened = review.prepare_decisions(
                kind,
                [resource_three],
                reviewer="unit-tester",
                reviewed_at="2026-09-10T21:00:00+00:00",
            )
            review.upsert_adjudications(kind, reopened)
            second_apply = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "step_apply_crosswalk_adjudications.py"),
                    "--adjudications",
                    adjudications_path.name,
                    "--apply",
                ],
                env=os.environ.copy(),
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(
                second_apply.returncode, 0, msg=second_apply.stdout + second_apply.stderr
            )
            self.assertIn("ROW-BOUND REBASE", second_apply.stdout)
            self.assertTrue(
                links.run_crosswalk_preflight(
                    common.all_cards(), {}, registry_path=registry_path
                )
            )
            final_report = pd.read_csv(newest_report(common), dtype=str).fillna("")
            self.assertEqual((final_report["status"] == "ACTIVE").sum(), 3)
            self.assertEqual((final_report["status"] == "DEFERRED").sum(), 2)
            self.assertEqual(
                final_report["status"].isin({"STALE", "CONFLICT", "INVALID_ROW"}).sum(),
                0,
            )


class CrosswalkDecisionRebaseTests(unittest.TestCase):
    def test_unrelated_crosswalk_edit_does_not_invalidate_pending_replacement(self):
        with tempfile.TemporaryDirectory() as temporary:
            common = configure(Path(temporary))
            common.save_card("R1", card("R1", "First Resource"))
            common.save_card("R2", card("R2", "Second Resource"))
            registry_path = write_sources(
                common,
                [
                    {
                        "csv_id": "8",
                        "repo_uid": "R8",
                        "probe_name": "First Resource",
                        "registry_name": "First Resource",
                    },
                    {
                        "csv_id": "9",
                        "repo_uid": "R9",
                        "probe_name": "Second Resource",
                        "registry_name": "Second Resource",
                    },
                ],
            )
            links = importlib.import_module("step_build_links")
            self.assertFalse(
                links.run_crosswalk_preflight(
                    common.all_cards(), {}, registry_path=registry_path
                )
            )
            review = importlib.import_module("review_widgets")
            kind, queue = review.normalize_review_file(newest_report(common))
            second = queue.loc[queue["original_repo_uid"] == "R9"].iloc[0].to_dict()
            second.update(
                {
                    "decision": "REPLACE_CROSSWALK_UID",
                    "selected_repo_uid": "R2",
                    "note": "Independent identity evidence confirms the second resource.",
                }
            )
            decisions = review.prepare_decisions(
                kind,
                [second],
                reviewer="unit-tester",
                reviewed_at="2026-09-10T20:00:00+00:00",
            )
            adjudications_path, _, _ = review.upsert_adjudications(kind, decisions)

            # Resolve a different row. This changes the whole-file SHA-256 but not the
            # reviewed second row's source fingerprint.
            crosswalk_path = Path(common.INP) / "crosswalk.csv"
            crosswalk = pd.read_csv(crosswalk_path, dtype=str).fillna("")
            crosswalk.loc[crosswalk["repo_uid"] == "R8", "repo_uid"] = "R1"
            crosswalk.to_csv(crosswalk_path, index=False)

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "step_apply_crosswalk_adjudications.py"),
                    "--adjudications",
                    adjudications_path.name,
                    "--apply",
                ],
                env=os.environ.copy(),
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
            self.assertIn("ROW-BOUND REBASE", result.stdout)
            updated = pd.read_csv(crosswalk_path, dtype=str).fillna("")
            self.assertEqual(updated.loc[updated["csv_id"] == "8", "repo_uid"].item(), "R1")
            self.assertEqual(updated.loc[updated["csv_id"] == "9", "repo_uid"].item(), "R2")

            run_dir = max(
                (Path(common.RUNS) / "apply_crosswalk_adjudications").iterdir(),
                key=lambda path: path.stat().st_mtime_ns,
            )
            plan = pd.read_csv(
                run_dir / "crosswalk_adjudication_plan.csv", dtype=str
            ).fillna("")
            self.assertEqual(plan.iloc[0]["crosswalk_snapshot_rebased"], "true")
            self.assertEqual(plan.iloc[0]["validation"], "PASS")


if __name__ == "__main__":
    unittest.main()
