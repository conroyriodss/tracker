from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

import pandas as pd
import yaml

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

MODULES = [
    "common",
    "identity_hygiene",
    "review_widgets",
    "step_apply_verify_adjudications",
    "step_build_links",
    "step_promote_environment",
    "step_restore_card_snapshot",
    "step_reconcile_ids",
    "verify_external_ids",
]


def fresh_modules(root: Path, env: str = "dev"):
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
    os.environ["NIH_TRACKER_ENV"] = env
    for name in ("dev", "staging", "prod"):
        os.environ[f"NIH_TRACKER_ROOT_{name.upper()}"] = str(root / name)
    os.environ["NIH_TRACKER_SHARED_CACHE_ROOT"] = str(root / "shared" / "cache")
    common = importlib.import_module("common")
    return common


def card(uid: str, name: str = "Example"):
    return {
        "schema_version": "1.0",
        "repo_uid": uid,
        "kind": "repository",
        "identity": {"name": name},
        "links": [],
        "blocks": {},
        "curation": {"review_state": "needs_review"},
    }


class EnvironmentTests(unittest.TestCase):
    def test_environment_mapping_and_safe_default(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            common = fresh_modules(root, "staging")
            self.assertEqual(common.ENV, "staging")
            self.assertEqual(Path(common.PROJECT), root / "staging")
            self.assertFalse(common.CREDENTIALS_ALLOWED)
            self.assertTrue(common.RE3DATA_CACHE_READ_ONLY)
            self.assertEqual(Path(common.project_for_env("prod")), root / "prod")

    def test_explicit_environment_rejects_conflicting_legacy_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fresh_modules(root, "dev")
            for name in MODULES:
                sys.modules.pop(name, None)
            os.environ["NIH_TRACKER_ENV"] = "dev"
            os.environ["NIH_TRACKER_ROOT_DEV"] = str(root / "dev")
            os.environ["NIH_TRACKER"] = str(root / "prod")
            with self.assertRaises(RuntimeError):
                importlib.import_module("common")

    def test_production_card_write_requires_lower_level_confirmation(self):
        with tempfile.TemporaryDirectory() as temporary:
            common = fresh_modules(Path(temporary), "prod")
            with self.assertRaisesRegex(RuntimeError, "Production card writes are locked"):
                common.save_card("R1", card("R1"))
            os.environ["NIH_TRACKER_PROD_WRITE_CONFIRMATION"] = common.PROD_WRITE_CONFIRMATION_TOKEN
            common.save_card("R1", card("R1"))
            self.assertTrue((Path(common.CARDS) / "R1.yaml").is_file())

    def test_environment_roots_must_be_distinct(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fresh_modules(root, "dev")
            for name in MODULES:
                sys.modules.pop(name, None)
            os.environ["NIH_TRACKER_ENV"] = "dev"
            os.environ["NIH_TRACKER_ROOT_DEV"] = str(root / "same")
            os.environ["NIH_TRACKER_ROOT_STAGING"] = str(root / "staging")
            os.environ["NIH_TRACKER_ROOT_PROD"] = str(root / "same")
            with self.assertRaisesRegex(RuntimeError, "environment roots must be distinct"):
                importlib.import_module("common")

    def test_project_override_cannot_alias_another_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fresh_modules(root, "dev")
            for name in MODULES:
                sys.modules.pop(name, None)
            os.environ["NIH_TRACKER_ENV"] = "dev"
            for name in ("dev", "staging", "prod"):
                os.environ[f"NIH_TRACKER_ROOT_{name.upper()}"] = str(root / name)
            os.environ["NIH_TRACKER"] = str(root / "prod")
            os.environ["NIH_TRACKER_ALLOW_PROJECT_OVERRIDE"] = "1"
            with self.assertRaisesRegex(RuntimeError, "cannot alias another configured environment"):
                importlib.import_module("common")

    def test_custom_active_production_root_still_requires_confirmation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fresh_modules(root, "dev")
            for name in MODULES:
                sys.modules.pop(name, None)
            os.environ["NIH_TRACKER_ENV"] = "prod"
            for name in ("dev", "staging", "prod"):
                os.environ[f"NIH_TRACKER_ROOT_{name.upper()}"] = str(root / name)
            os.environ["NIH_TRACKER"] = str(root / "custom_prod")
            os.environ["NIH_TRACKER_ALLOW_PROJECT_OVERRIDE"] = "1"
            common = importlib.import_module("common")
            self.assertEqual(Path(common.PROJECT), root / "custom_prod")
            with self.assertRaisesRegex(RuntimeError, "Production card writes are locked"):
                common.save_card("R1", card("R1"))



class TransactionTests(unittest.TestCase):
    def test_commit_is_atomic_snapshot_backed_and_unlocks(self):
        with tempfile.TemporaryDirectory() as temporary:
            common = fresh_modules(Path(temporary))
            common.save_card("R1", card("R1", "Before"))
            with common.card_transaction("test_commit"):
                updated = common.load_card("R1")
                updated["identity"]["name"] = "After"
                common.save_card("R1", updated)
            self.assertEqual(common.load_card("R1")["identity"]["name"], "After")
            self.assertFalse((Path(common.CARDS) / ".lock").exists())
            manifests = list((Path(common.SNAPSHOTS) / "cards").glob("*/manifest.json"))
            committed = [json.loads(path.read_text()) for path in manifests if json.loads(path.read_text())["step"] == "test_commit"]
            self.assertEqual(len(committed), 1)
            self.assertEqual(committed[0]["status"], "committed")
            self.assertTrue(committed[0]["touched"][0]["before_sha256"])

    def test_exception_rolls_back_all_touched_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            common = fresh_modules(Path(temporary))
            common.save_card("R1", card("R1", "Before"))
            auxiliary = Path(common.PROJECT) / "inputs" / "coupled.csv"
            auxiliary.write_text("before\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "boom"):
                with common.card_transaction("test_rollback") as tx:
                    updated = common.load_card("R1")
                    updated["identity"]["name"] = "After"
                    common.save_card("R1", updated)
                    tx.write_text(auxiliary, "after\n", operation="coupled")
                    raise RuntimeError("boom")
            self.assertEqual(common.load_card("R1")["identity"]["name"], "Before")
            self.assertEqual(auxiliary.read_text(), "before\n")
            manifests = [json.loads(path.read_text()) for path in (Path(common.SNAPSHOTS) / "cards").glob("*/manifest.json")]
            rollback = [manifest for manifest in manifests if manifest["step"] == "test_rollback"]
            self.assertEqual(rollback[0]["status"], "rolled_back")

    def test_existing_lock_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            common = fresh_modules(Path(temporary))
            lock = common.FileLock(Path(common.CARDS) / ".lock", purpose="holder").acquire()
            try:
                with self.assertRaises(common.LockError):
                    common.CardTransaction("second").begin()
            finally:
                lock.release()

    def test_save_rejects_embedded_uid_mismatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            common = fresh_modules(Path(temporary))
            with self.assertRaises(ValueError):
                common.save_card("R1", card("R2"))

    def test_optimistic_update_check_refuses_plan_built_from_stale_card(self):
        with tempfile.TemporaryDirectory() as temporary:
            common = fresh_modules(Path(temporary))
            common.save_card("R1", card("R1", "Planned state"))
            expected = common.load_card("R1")
            intervening = common.load_card("R1")
            intervening["identity"]["name"] = "Intervening state"
            common.save_card("R1", intervening)

            with self.assertRaises(common.ConcurrentCardModificationError):
                with common.card_transaction("stale_plan"):
                    common.load_card_for_update("R1", expected)

            self.assertEqual(
                common.load_card("R1")["identity"]["name"], "Intervening state"
            )
            self.assertFalse((Path(common.CARDS) / ".lock").exists())

    def test_transaction_snapshots_cannot_be_disabled(self):
        with tempfile.TemporaryDirectory() as temporary:
            common = fresh_modules(Path(temporary))
            with self.assertRaisesRegex(ValueError, "require snapshot=True"):
                common.CardTransaction("unsafe", snapshot=False)

    def test_manual_card_archives_are_unique_and_exclude_transaction_internals(self):
        with tempfile.TemporaryDirectory() as temporary:
            common = fresh_modules(Path(temporary))
            common.save_card("R1", card("R1"))
            (Path(common.CARDS) / ".tmp").mkdir(exist_ok=True)
            (Path(common.CARDS) / ".tmp" / "partial.yaml").write_text(
                "partial", encoding="utf-8"
            )
            (Path(common.CARDS) / ".lock").write_text("test", encoding="utf-8")
            try:
                first = Path(common.snapshot_cards_zip(label="same label"))
                second = Path(common.snapshot_cards_zip(label="same label"))
            finally:
                (Path(common.CARDS) / ".lock").unlink(missing_ok=True)

            self.assertNotEqual(first, second)
            self.assertTrue(first.is_file())
            self.assertTrue(second.is_file())
            with zipfile.ZipFile(first) as archive:
                self.assertEqual(archive.namelist(), ["R1.yaml"])

    def test_id_remap_lock_is_retained_through_transaction_rollback(self):
        with tempfile.TemporaryDirectory() as temporary:
            common = fresh_modules(Path(temporary))
            with self.assertRaisesRegex(RuntimeError, "force rollback"):
                with common.card_transaction("remap_rollback"):
                    common.append_id_remap("B1", "R1", "test_match")
                    self.assertTrue(Path(f"{common.ID_REMAP_CSV}.lock").is_file())
                    with self.assertRaises(common.LockError):
                        common.FileLock(
                            f"{common.ID_REMAP_CSV}.lock",
                            purpose="competing remap writer",
                        ).acquire()
                    raise RuntimeError("force rollback")
            self.assertFalse(Path(common.ID_REMAP_CSV).exists())
            self.assertFalse(Path(f"{common.ID_REMAP_CSV}.lock").exists())
            self.assertFalse((Path(common.CARDS) / ".lock").exists())



class IssueRegistryTests(unittest.TestCase):
    def test_dedupes_per_run_and_escalates_then_resolves(self):
        with tempfile.TemporaryDirectory() as temporary:
            common = fresh_modules(Path(temporary))
            for run in ("quarter-1", "quarter-2", "quarter-3"):
                os.environ["NIH_TRACKER_RUN_ID"] = run
                common.log_issue(
                    "reconcile_ids",
                    "advisory",
                    "B7 remains unreconciled",
                    repo_uid="B7",
                    issue_type="b_card_unreconciled",
                    escalation_after=3,
                )
                # Duplicate logging inside one run must not increment recurrence.
                common.log_issue(
                    "reconcile_ids",
                    "advisory",
                    "B7 remains unreconciled",
                    repo_uid="B7",
                    issue_type="b_card_unreconciled",
                    escalation_after=3,
                )
            frame = pd.read_csv(common.ISSUES_LOG, dtype=str).fillna("")
            self.assertEqual(len(frame), 1)
            self.assertEqual(frame.iloc[0]["times_seen"], "3")
            self.assertEqual(frame.iloc[0]["severity"], "needs_attention")
            resolved = common.resolve_issues_not_in(
                step="reconcile_ids",
                issue_type="b_card_unreconciled",
                active_repo_uids=[],
            )
            self.assertEqual(resolved, 1)
            frame = pd.read_csv(common.ISSUES_LOG, dtype=str).fillna("")
            self.assertEqual(frame.iloc[0]["resolved"].lower(), "true")


class ReconciliationIssueTests(unittest.TestCase):
    def test_dry_run_proposal_does_not_resolve_existing_b_card(self):
        with tempfile.TemporaryDirectory() as temporary:
            common = fresh_modules(Path(temporary), "dev")
            common.save_card("B1", card("B1", "Candidate"))
            reconcile = importlib.import_module("step_reconcile_ids")
            promotable = pd.DataFrame([{"old_uid": "B1", "new_uid": "R1"}])
            collisions = pd.DataFrame(columns=["old_uid", "new_uid"])
            os.environ["NIH_TRACKER_RUN_ID"] = "dry-run-1"
            count = reconcile.report_unreconciled(
                {"B1": common.load_card("B1")}, promotable, collisions
            )
            self.assertEqual(count, 1)
            frame = pd.read_csv(common.ISSUES_LOG, dtype=str).fillna("")
            self.assertEqual(frame.iloc[0]["resolved"].lower(), "false")
            self.assertIn("proposed but not committed", frame.iloc[0]["detail"])

            common.delete_card("B1")
            reconcile.report_unreconciled({}, promotable, collisions)
            frame = pd.read_csv(common.ISSUES_LOG, dtype=str).fillna("")
            self.assertEqual(frame.iloc[0]["resolved"].lower(), "true")


class ReviewTests(unittest.TestCase):
    def test_verify_report_filters_to_review_eligible_and_upserts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            common = fresh_modules(root)
            review = importlib.import_module("review_widgets")
            source = Path(common.RUNS) / "verify_ids" / "20260908T120000" / "verify_report.csv"
            source.parent.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(
                [
                    {
                        "repo_uid": "R1",
                        "card_name": "One",
                        "id_type": "re3data",
                        "candidate_id": "r3d100000001",
                        "candidate_url": "https://www.re3data.org/repository/r3d100000001",
                        "candidate_eligible": "True",
                        "candidate_confidence": "0.8",
                    },
                    {
                        "repo_uid": "R2",
                        "card_name": "Two",
                        "id_type": "re3data",
                        "candidate_id": "r3d100000002",
                        "candidate_eligible": "False",
                    },
                ]
            ).to_csv(source, index=False)
            kind, queue = review.normalize_review_file(source)
            self.assertEqual(kind, "verify")
            self.assertEqual(len(queue), 1)
            row = queue.iloc[0].to_dict()
            row["decision"] = "REJECT"
            decisions = review.prepare_decisions("verify", [row], reviewer="AB")
            destination, inserted, updated = review.upsert_adjudications("verify", decisions)
            self.assertEqual((inserted, updated), (1, 0))
            decisions[0]["note"] = "confirmed false positive"
            _, inserted, updated = review.upsert_adjudications("verify", decisions)
            self.assertEqual((inserted, updated), (0, 1))
            saved = pd.read_csv(destination, dtype=str).fillna("")
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved.iloc[0]["reviewer"], "AB")

    def test_card_accept_translates_candidate_to_alias(self):
        with tempfile.TemporaryDirectory() as temporary:
            common = fresh_modules(Path(temporary))
            review = importlib.import_module("review_widgets")
            source = Path(common.RUNS) / "build_links" / "20260908T120000" / "links_review.csv"
            source.parent.mkdir(parents=True, exist_ok=True)
            pd.DataFrame([
                {"registry_name": "Registry Label", "matched_card": "R1", "how": "fuzzy_0.94_nameset"}
            ]).to_csv(source, index=False)
            kind, queue = review.normalize_review_file(source)
            row = queue.iloc[0].to_dict()
            row["review_decision"] = "ACCEPT"
            prepared = review.prepare_decisions("card", [row], reviewer="AB")
            self.assertEqual(prepared[0]["verdict"], "ACCEPT_ALIAS")
            self.assertEqual(prepared[0]["add_aliases"], "Registry Label")

    def test_apply_one_refuses_stale_implicit_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            fresh_modules(Path(temporary))
            apply_module = importlib.import_module("step_apply_verify_adjudications")
            current = card("R1")
            current["links"] = [{"id_type": "re3data", "id_value": "r3dOLD", "url": "https://old"}]
            row = {
                "review_key": "key",
                "decision": "ACCEPT",
                "repo_uid": "R1",
                "id_type": "re3data",
                "candidate_id": "r3dNEW",
                "candidate_url": "https://new",
                "existing_id": "",
                "existing_url": "",
            }
            with self.assertRaisesRegex(ValueError, "acquired a different"):
                apply_module.apply_one(current, row)

    def test_direct_verifier_apply_does_not_add_second_identifier_of_same_type(self):
        with tempfile.TemporaryDirectory() as temporary:
            common = fresh_modules(Path(temporary))
            current = card("R1")
            current["links"] = [
                {
                    "id_type": "re3data",
                    "id_value": "r3dCURRENT",
                    "url": "https://current.example",
                }
            ]
            common.save_card("R1", current)
            verifier = importlib.import_module("verify_external_ids")
            path = str(Path(common.CARDS) / "R1.yaml")
            proposals = [
                {
                    "card_file": path,
                    "action": "add_missing_link",
                    "id_type": "re3data",
                    "proposed_link": {
                        "id_type": "re3data",
                        "id_value": "r3dPROPOSED",
                        "url": "https://proposed.example",
                    },
                }
            ]
            changed, skipped = verifier.apply_proposals(
                proposals, cards_dir=common.CARDS
            )
            self.assertEqual(changed, [])
            self.assertEqual(len(skipped), 1)
            reloaded = common.load_card("R1")
            self.assertEqual(len(reloaded["links"]), 1)
            self.assertEqual(reloaded["links"][0]["id_value"], "r3dCURRENT")

    def test_standalone_verifier_apply_fails_closed_without_common(self):
        with tempfile.TemporaryDirectory() as temporary:
            common = fresh_modules(Path(temporary))
            verifier = importlib.import_module("verify_external_ids")
            original_common = verifier.tracker_common
            verifier.tracker_common = None
            try:
                with self.assertRaisesRegex(RuntimeError, "Standalone verifier use is read-only"):
                    verifier.apply_proposals([], cards_dir=common.CARDS)
            finally:
                verifier.tracker_common = original_common

    def test_direct_verifier_apply_refuses_card_hash_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            common = fresh_modules(Path(temporary))
            common.save_card("R1", card("R1", "Reviewed name"))
            verifier = importlib.import_module("verify_external_ids")
            path = Path(common.CARDS) / "R1.yaml"
            proposal = {
                "card_file": str(path),
                "expected_card_sha256": common.sha256_file(path),
                "action": "add_missing_link",
                "id_type": "re3data",
                "proposed_link": {
                    "id_type": "re3data",
                    "id_value": "r3d100000001",
                    "url": "https://www.re3data.org/repository/r3d100000001",
                },
            }
            changed_card = common.load_card("R1")
            changed_card["identity"]["name"] = "Changed after verification"
            common.save_card("R1", changed_card)
            changed, skipped = verifier.apply_proposals([proposal], cards_dir=common.CARDS)
            self.assertEqual(changed, [])
            self.assertEqual(len(skipped), 1)
            self.assertIn("changed after verification", skipped[0][1])
            self.assertEqual(common.load_card("R1")["links"], [])



class PromotionAndRestoreTests(unittest.TestCase):
    def test_plan_creation_fails_closed_when_source_cards_are_locked(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            common = fresh_modules(root, "dev")
            source_cards = root / "dev" / "cards"
            source_cards.mkdir(parents=True, exist_ok=True)
            (source_cards / "R1.yaml").write_text(
                yaml.safe_dump(card("R1"), sort_keys=False), encoding="utf-8"
            )
            promotion = importlib.import_module("step_promote_environment")
            lock = common.FileLock(source_cards / ".lock", purpose="concurrent writer").acquire()
            try:
                with self.assertRaises(common.LockError):
                    promotion.create_plan("dev", "prod")
            finally:
                lock.release()

    def test_plan_apply_preserves_target_only_cards(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            common = fresh_modules(root, "dev")
            source_cards = root / "dev" / "cards"
            target_cards = root / "prod" / "cards"
            source_cards.mkdir(parents=True, exist_ok=True)
            target_cards.mkdir(parents=True, exist_ok=True)
            (source_cards / "R1.yaml").write_text(yaml.safe_dump(card("R1", "Promoted"), sort_keys=False))
            (target_cards / "R2.yaml").write_text(yaml.safe_dump(card("R2", "Target only"), sort_keys=False))
            promotion = importlib.import_module("step_promote_environment")
            plan_path = promotion.create_plan("dev", "prod")
            self.assertTrue((plan_path.parent / "promotion_plan.sha256").is_file())
            receipt = promotion.apply_plan(plan_path, confirm="PROMOTE_DEV_TO_PROD")
            self.assertTrue((target_cards / "R1.yaml").is_file())
            self.assertTrue((target_cards / "R2.yaml").is_file())
            self.assertTrue(receipt.is_file())
            self.assertEqual(yaml.safe_load((target_cards / "R1.yaml").read_text())["identity"]["name"], "Promoted")
            target_manifests = [
                json.loads(path.read_text())
                for path in (root / "prod" / "snapshots" / "cards").glob("*/manifest.json")
                if json.loads(path.read_text()).get("step") == "promote_dev_to_prod"
            ]
            self.assertEqual(len(target_manifests), 1)
            self.assertEqual(target_manifests[0]["env"], "prod")

    def test_tampered_promotion_plan_fails_seal_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fresh_modules(root, "dev")
            source_cards = root / "dev" / "cards"
            source_cards.mkdir(parents=True, exist_ok=True)
            (source_cards / "R1.yaml").write_text(
                yaml.safe_dump(card("R1", "Promoted"), sort_keys=False),
                encoding="utf-8",
            )
            promotion = importlib.import_module("step_promote_environment")
            plan_path = promotion.create_plan("dev", "staging")
            plan = json.loads(plan_path.read_text())
            plan["entries"] = []
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "seal mismatch"):
                promotion.apply_plan(plan_path, confirm="PROMOTE_DEV_TO_STAGING")

    def test_restore_script_reverts_committed_transaction(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            common = fresh_modules(root, "dev")
            common.save_card("R1", card("R1", "Before"))
            with common.card_transaction("bad_commit"):
                changed = common.load_card("R1")
                changed["identity"]["name"] = "Bad"
                common.save_card("R1", changed)
            manifests = []
            for path in (Path(common.SNAPSHOTS) / "cards").glob("*/manifest.json"):
                payload = json.loads(path.read_text())
                if payload["step"] == "bad_commit":
                    manifests.append(path)
            self.assertEqual(len(manifests), 1)
            transaction_id = json.loads(manifests[0].read_text())["transaction_id"]
            env = os.environ.copy()
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "step_restore_card_snapshot.py"),
                    "--manifest",
                    str(manifests[0]),
                    "--apply",
                    "--confirm",
                    f"RESTORE_{transaction_id}",
                ],
                env=env,
                text=True,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, msg=result.stdout + "\n" + result.stderr)
            self.assertEqual(yaml.safe_load((Path(common.CARDS) / "R1.yaml").read_text())["identity"]["name"], "Before")


class BuildLinksJoinSafetyTests(unittest.TestCase):
    def _load(self, root: Path):
        common = fresh_modules(root, "dev")
        sys.modules.pop("step_build_links", None)
        module = importlib.import_module("step_build_links")
        return common, module

    def test_retired_crosswalk_uid_is_translated_through_id_remap_on_apply(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            common, module = self._load(root)
            common.save_card("R200", card("R200", "Example Repository"))
            inputs = Path(common.INP)
            pd.DataFrame(
                [{"registry_name": "Example Repository", "repo_uid": "R129"}]
            ).to_csv(inputs / "crosswalk.csv", index=False)
            pd.DataFrame(
                [
                    {
                        "old_uid": "R129",
                        "new_uid": "R150",
                        "reconciled_at": "2026-01-01T00:00:00",
                        "match_type": "test",
                    },
                    {
                        "old_uid": "R150",
                        "new_uid": "R200",
                        "reconciled_at": "2026-01-02T00:00:00",
                        "match_type": "test",
                    },
                ]
            ).to_csv(common.ID_REMAP_CSV, index=False)
            pd.DataFrame(
                [
                    {
                        "Repository_Name": "Example Repository",
                        "FS_DOI": "10.25504/FAIRsharing.test123",
                        "RE3_Name": "Example Repository",
                        "SC_Identifier": "",
                    }
                ]
            ).to_csv(inputs / "registry.csv", index=False)

            previous = sys.argv[:]
            try:
                sys.argv = ["step_build_links.py", "--registry", "registry.csv", "--apply"]
                module.main()
            finally:
                sys.argv = previous

            updated = common.load_card("R200")
            self.assertTrue(
                any(
                    link.get("id_type") == "fairsharing_doi"
                    and link.get("id_value") == "10.25504/FAIRsharing.test123"
                    for link in updated["links"]
                )
            )
            self.assertFalse((Path(common.CARDS) / "R129.yaml").exists())
            latest_review = sorted(Path(common.RUNS).glob("build_links/*/links_review.csv"))[-1]
            review = pd.read_csv(latest_review)
            self.assertEqual(review.loc[0, "how"], "crosswalk_uid_remapped")
            self.assertEqual(review.loc[0, "matched_card"], "R200")

    def test_unmapped_stale_crosswalk_uid_is_withheld_not_dereferenced(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            common, module = self._load(root)
            common.save_card("R200", card("R200", "Example Repository"))
            pd.DataFrame(
                [{"registry_name": "Example Repository", "repo_uid": "R129"}]
            ).to_csv(Path(common.INP) / "crosswalk.csv", index=False)
            registry = pd.DataFrame([{"Repository_Name": "Example Repository"}])

            uid_of, review = module.build_join(
                registry,
                common.all_cards(),
                min_conf=0.94,
                uid_remap={},
            )
            self.assertEqual(uid_of, {})
            self.assertEqual(review.loc[0, "how"], "STALE_CROSSWALK_UID")
            self.assertEqual(review.loc[0, "crosswalk_uid"], "R129")

    def test_conflicting_id_remap_targets_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            common, module = self._load(root)
            pd.DataFrame(
                [
                    {"old_uid": "R129", "new_uid": "R200"},
                    {"old_uid": "R129", "new_uid": "R201"},
                ]
            ).to_csv(common.ID_REMAP_CSV, index=False)
            with self.assertRaisesRegex(ValueError, "Conflicting id_remap targets"):
                module.load_uid_remap(common.ID_REMAP_CSV)


if __name__ == "__main__":
    unittest.main()
