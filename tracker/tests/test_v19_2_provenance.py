"""Synthetic tests for source selection, retention, recovery, and inventory."""
import importlib
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from run_provenance import archive_candidates, digest, retention_report, protection_reason


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'prod'
        self.old_env = os.environ.copy()
        for name in list(os.environ):
            if name.startswith('NIH_TRACKER'):
                del os.environ[name]
        os.environ.update(NIH_TRACKER_PRODUCTION_ONLY='1', NIH_TRACKER_ENV='prod',
                          NIH_TRACKER_ROOT_PROD=str(self.root), NIH_TRACKER_SHARED_CACHE_ROOT=str(Path(self.temp.name) / 'cache'))
        self.clear_modules()
        self.common = importlib.import_module('common')
        self.workflow = importlib.import_module('review_workflow')
        self.inventory = importlib.import_module('step_project_inventory')
        self.maintenance = importlib.import_module('step_maintenance')

    def clear_modules(self):
        for name in ('common', 'review_workflow', 'review_widgets', 'automated_review', 'step_project_inventory', 'step_maintenance', 'step_initialize_environments'):
            sys.modules.pop(name, None)

    def tearDown(self):
        self.clear_modules()
        os.environ.clear(); os.environ.update(self.old_env)
        self.temp.cleanup()

    def queue(self, run, label='Example'):
        path = self.root / 'runs' / 'crosswalk' / run / 'crosswalk_review.csv'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('registry_name,matched_card,how\n' + label + ',R1,fuzzy\n')
        os.utime(path, ns=(1_000_000_000, 1_000_000_000))
        return path

    def old_logs(self):
        old = self.root / 'runs' / 'logs_only' / 'old'
        old.mkdir(parents=True); (old / 'output.txt').write_text('older log')
        new = old.parent / 'new'; new.mkdir(); (new / 'output.txt').write_text('new log')
        os.utime(old, ns=(100, 100)); os.utime(new, ns=(200, 200))
        return old, new

    def plan(self):
        return self.maintenance.build_cleanup_plan(keep_runs=1, prune_card_snapshots=False,
                   keep_snapshots=20, remove_temporary=False, temp_older_hours=24)

    def test_tied_mtimes_are_stable_across_refreshes(self):
        self.queue('20260928T203328'); expected = self.queue('20260928T210105')
        for _ in range(8):
            row = self.workflow.current_queue_summary()[0]
            self.assertEqual(row['source_file'], str(expected))
            self.assertEqual(row['source_sha256'], digest(expected))

    def test_pin_retains_queue_when_new_run_appears(self):
        old = self.queue('old')
        self.inventory.pin_sources(self.root)
        new = self.queue('new'); os.utime(new, ns=(2_000_000_000, 2_000_000_000))
        row = self.workflow.current_queue_summary()[0]
        self.assertEqual(row['source_file'], str(old)); self.assertEqual(row['selection'], 'pinned')

    def test_pinned_drift_fails_closed(self):
        old = self.queue('old'); self.inventory.pin_sources(self.root)
        old.write_text(old.read_text() + 'Changed,R2,fuzzy\n')
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.workflow.compute_review_status()

    def test_missing_pinned_source_does_not_fall_back(self):
        old = self.queue('old'); self.inventory.pin_sources(self.root)
        old.unlink(); self.queue('new')
        with self.assertRaisesRegex(ValueError, 'missing'):
            self.workflow.current_queue_summary()

    def test_cross_family_pin_rejected(self):
        self.queue('old'); self.inventory.pin_sources(self.root)
        path = self.root / 'inputs/active_review_sources.json'; data = json.loads(path.read_text())
        data['sources']['identifier'] = data['sources'].pop('card'); path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'family'):
            self.workflow.current_queue_summary()

    def test_unpin_archives_original_bytes(self):
        self.queue('old'); self.inventory.pin_sources(self.root)
        path = self.root / 'inputs/active_review_sources.json'; before = path.read_bytes()
        self.inventory.unpin_sources(self.root)
        self.assertFalse(path.exists())
        self.assertIn(before, [p.read_bytes() for p in (self.root/'runs/review_source_selection').glob('*/previous.json')])

    def test_all_unreadable_queues_fail_closed(self):
        self.queue('old')
        with patch.object(self.workflow, 'normalize_review_file', side_effect=ValueError('invalid')):
            with self.assertRaisesRegex(RuntimeError, 'No readable'):
                self.workflow.compute_review_status()

    def test_reference_protects_log_only_run(self):
        old, _ = self.old_logs()
        (self.root/'inputs/decisions.csv').write_text('source_file\n' + str(old/'output.txt') + '\n')
        report = retention_report(self.root)
        self.assertTrue(protection_reason(old, report))
        _, plan = self.plan()
        self.assertNotIn(str(old), [r['path'] for r in plan['candidates']])

    def test_structured_runs_and_card_history_retained(self):
        old, _ = self.old_logs(); (old/'decision.json').write_text('{}')
        snap = self.root/'snapshots/cards/one'; snap.mkdir(parents=True); (snap/'manifest.json').write_text('{"status":"committed"}')
        report = retention_report(self.root)
        self.assertTrue(protection_reason(old, report)); self.assertTrue(protection_reason(snap, report))

    def test_reference_added_after_plan_blocks_all_deletion(self):
        old, _ = self.old_logs(); path, plan = self.plan()
        self.assertIn(str(old), [r['path'] for r in plan['candidates']])
        (self.root/'inputs/decisions.csv').write_text('source_file\n' + str(old/'output.txt') + '\n')
        with self.assertRaisesRegex(RuntimeError, 'no files were deleted'):
            self.maintenance.apply_cleanup(path, confirm=self.maintenance.CLEANUP_CONFIRMATION)
        self.assertTrue(old.exists())

    def test_archive_roundtrip_before_removal(self):
        old, new = self.old_logs(); original = (old/'output.txt').read_bytes()
        (old/'empty').mkdir(); os.utime(old, ns=(100, 100))
        path, _ = self.plan()
        result = self.maintenance.apply_cleanup(path, confirm=self.maintenance.CLEANUP_CONFIRMATION)
        self.assertFalse(old.exists()); self.assertTrue(new.exists())
        archive = Path(result['archive']['path'])
        self.assertEqual(digest(archive), result['archive']['sha256'])
        with zipfile.ZipFile(archive) as z:
            self.assertEqual(z.read('runs/logs_only/old/output.txt'), original)
            self.assertIn('runs/logs_only/old/empty/', z.namelist())

    def test_archive_failure_prevents_removal(self):
        old, _ = self.old_logs(); path, _ = self.plan()
        with patch.object(self.maintenance, 'archive_candidates', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.maintenance.apply_cleanup(path, confirm=self.maintenance.CLEANUP_CONFIRMATION)
        self.assertTrue(old.exists())

    def test_archive_refuses_symlink(self):
        old, _ = self.old_logs(); (old/'link').symlink_to(old/'output.txt')
        with self.assertRaisesRegex(ValueError, 'symlink'):
            archive_candidates(self.root, [{'path':str(old)}], self.root/'snapshots/recovery.zip')

    def test_inventory_hashes_observed_bytes_and_escapes_html(self):
        self.queue('old', '<script>alert(1)</script>')
        secret = self.root/'inputs/service_account_credentials.json'; secret.write_text('{"private":"excluded"}')
        out = self.root/'runs/project_inventory/test'
        report = self.inventory.collect_inventory(self.root, out)
        self.assertTrue(report['complete'])
        record = next(r for r in report['files'] if r['path']==str(secret.relative_to(self.root)))
        self.assertEqual(record['status'], 'sensitive_name_excluded'); self.assertNotIn('sha256', record)
        self.assertFalse(any(r['path'].startswith('runs/project_inventory/test/') for r in report['files']))
        self.assertEqual(report['review']['queue_pending_rows'], 1)

    def test_git_absence_is_reported_not_invented(self):
        from code_provenance import describe_code
        result = describe_code(self.root)
        self.assertIsNone(result['git_commit']); self.assertIsNone(result['git_dirty'])
        self.assertEqual(len(result['source_tree_sha256']), 64)

    def test_ledger_records_sha256_and_source_identity(self):
        path = self.queue('old')
        run = self.common.new_run_dir('test_provenance')
        row = self.common.ledger_append('test_provenance', {}, {'queue': str(path)}, {}, True, run)
        self.assertEqual(json.loads(row['input_sha256'])['queue'], digest(path))
        self.assertTrue(Path(row['code_provenance']).is_file())
        self.assertEqual(len(row['source_tree_sha256']), 64)

    def test_ledger_without_run_directory_creates_scoped_receipt(self):
        row = self.common.ledger_append('maintenance_event', {}, {}, {}, True, '')
        location = Path(row['code_provenance'])
        self.assertTrue(location.is_relative_to(self.root/'runs/maintenance_event'))
        self.assertTrue(location.is_file())
        self.assertEqual(Path(row['run_dir']), location.parent)

    def test_directory_input_hash_changes_with_content(self):
        directory = self.root/'inputs'; path = directory/'sample.txt'; path.write_text('before')
        before = self.common.sha256_path(directory)
        path.write_text('after')
        self.assertNotEqual(before, self.common.sha256_path(directory))
        self.assertEqual(len(before), 64)

    def test_installer_preserves_live_reference_snapshots(self):
        initializer = importlib.import_module('step_initialize_environments')
        for name in ('dbGaP_Studies.csv', 'repository_status_overrides.csv', 're3data_confirmed_mappings.csv'):
            target = self.root/'reference'/name; target.parent.mkdir(exist_ok=True); target.write_text('live reviewed snapshot')
        run = self.root/'runs/install_release/plan'; run.mkdir(parents=True)
        path = initializer.build_plan(source_env='prod', release_root=ROOT, mode='refresh-code', run_dir=run)
        plan = initializer._validate_plan(path)
        rows = {r['relative_path']: r for r in plan['entries']}
        for name in ('dbGaP_Studies.csv', 'repository_status_overrides.csv', 're3data_confirmed_mappings.csv'):
            self.assertEqual(rows['reference/'+name]['action'], 'preserve_existing')

    def coverage(self):
        catalog = self.root/'build/catalog'; catalog.mkdir(parents=True)
        data = catalog/'field_coverage.csv'
        data.write_text('repo_uid,name,data_format_documented\nR1,Example,0\n')
        manifest = {'catalog_id':'synthetic', 'catalog_as_of':'2026-11-10', 'counts':{'resources':1},
                    'artifacts':{'field_coverage.csv':{'sha256':digest(data)}}}
        (catalog/'manifest.json').write_text(json.dumps(manifest))
        return catalog, data, manifest

    def test_coverage_report_checks_hash_and_denominator(self):
        from step_build_coverage_report import build_report
        catalog, data, manifest = self.coverage()
        result = build_report(catalog, self.root/'coverage.html')
        self.assertEqual(result['resources'], 1)
        self.assertEqual(result['fields']['data_format_documented'], 0)
        manifest['counts']['resources'] = 2
        (catalog/'manifest.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'denominator'):
            build_report(catalog, self.root/'coverage.html')
        data.write_text(data.read_text() + 'R2,Other,1\n')
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            build_report(catalog, self.root/'coverage.html')

    def test_coverage_report_does_not_coerce_missing_flags(self):
        from step_build_coverage_report import build_report
        catalog, data, manifest = self.coverage()
        data.write_text('repo_uid,name,data_format_documented\nR1,Example,\n')
        manifest['artifacts']['field_coverage.csv']['sha256'] = digest(data)
        (catalog/'manifest.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'flags'):
            build_report(catalog, self.root/'coverage.html')

    def test_maintenance_notebook_requires_expected_release(self):
        notebook = json.loads((ROOT/'nih_tracker_colab_v19_maintenance.ipynb').read_text())
        code = '\n'.join(''.join(c['source']) for c in notebook['cells'] if c['cell_type']=='code')
        self.assertIn('parent.glob("nih_tracker_v19_2_bundle.zip")', code)
        self.assertIn('installed_manifest.get("release") != EXPECTED_RELEASE_VERSION', code)
        self.assertNotIn('parent.glob("*v19*bundle*.zip")', code)


if __name__ == '__main__':
    unittest.main()
