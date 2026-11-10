"""Regression coverage for no-lock reporting and explicit cleanup review."""
import importlib
import importlib.util
import csv
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_v13_notebooks_and_operations import fresh_common, clear_tracker_modules, SCRIPTS


class MaintenancePatchTests(unittest.TestCase):
    def setUp(self):
        self.old_env = os.environ.copy()
        self.temp = tempfile.TemporaryDirectory()
        os.environ.pop('NIH_TRACKER_PRODUCTION_ONLY', None)
        self.common = fresh_common(Path(self.temp.name))
        self.maintenance = importlib.import_module('step_maintenance')
        self.widgets = importlib.import_module('maintenance_widgets')
        self.root = Path(self.common.PROJECT)

    def tearDown(self):
        clear_tracker_modules()
        sys.modules.pop('maintenance_widgets', None)
        os.environ.clear()
        os.environ.update(self.old_env)
        self.temp.cleanup()

    def plan(self):
        return self.maintenance.build_cleanup_plan(keep_runs=5, prune_card_snapshots=False,
                                                  keep_snapshots=20, remove_temporary=False,
                                                  temp_older_hours=24)[0]

    def lock_command(self, confirm='REMOVE_ABANDONED_CARD_LOCK'):
        with patch.object(sys, 'argv', ['step_maintenance.py', 'remove-lock', '--apply', '--confirm', confirm]):
            return self.maintenance.main()

    def test_absent_lock_passes_notebook_runtime_and_preserves_noop_audit(self):
        runtime_module = importlib.import_module('notebook_runtime')
        runtime = runtime_module.NotebookRuntime(notebook_name='maintenance', env='dev',
                                                project=self.root, scripts=SCRIPTS, enable_prod_writes=False)
        result = runtime.run_script('step_maintenance.py', ['remove-lock', '--apply', '--confirm',
                                    'REMOVE_ABANDONED_CARD_LOCK'], label='lock_noop')
        self.assertTrue(result.ok)
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.gate_passed)
        with Path(self.common.LEDGER).open() as handle:
            rows = list(csv.DictReader(handle))
        outputs = json.loads(rows[-1]['outputs'])
        self.assertEqual(outputs['outcome'], 'already_unlocked')
        self.assertFalse(outputs['removed'])

    def test_existing_lock_is_removed_and_audited(self):
        lock = Path(self.common.CARDS) / '.lock'
        lock.write_text('{"purpose":"abandoned test"}')
        with patch.object(self.maintenance, 'ledger_append') as ledger:
            self.assertEqual(self.lock_command(), 0)
        self.assertFalse(lock.exists())
        self.assertTrue(ledger.call_args.kwargs['gate_passed'])
        self.assertEqual(ledger.call_args.kwargs['outputs']['outcome'], 'removed')

    def test_confirmation_still_required_when_already_unlocked(self):
        with self.assertRaisesRegex(ValueError, 'confirmation'):
            self.lock_command('')

    def test_unlink_failure_is_not_swallowed(self):
        lock = Path(self.common.CARDS) / '.lock'
        lock.write_text('{}')
        with patch.object(Path, 'unlink', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):
                self.lock_command()
        self.assertTrue(lock.exists())

    def test_remaining_or_reappeared_lock_fails_gate_and_exit(self):
        with patch.object(self.maintenance, 'card_lock_status', side_effect=[None, {'owner': 'new writer'}]), \
             patch.object(self.maintenance, 'ledger_append') as ledger:
            self.assertEqual(self.lock_command(), 1)
        self.assertFalse(ledger.call_args.kwargs['gate_passed'])
        self.assertEqual(ledger.call_args.kwargs['outputs']['outcome'], 'lock_still_held')

    def test_discovery_never_selects_a_plan_or_authorizes_apply(self):
        path = self.plan()
        review = self.widgets.CleanupPlanReview(self.root)
        options, errors = review.discover()
        self.assertEqual([value for _, value in options], [str(path)])
        self.assertEqual(errors, [])
        self.assertIsNone(review.path)
        with self.assertRaisesRegex(ValueError, 'Select a saved plan'):
            review.inspect('')
        with self.assertRaisesRegex(ValueError, 'Inspect'):
            review.apply_arguments(str(path), reviewed=True, no_active_writers=True)

    def test_apply_requires_inspection_both_acknowledgments_and_exact_selection(self):
        path = self.plan()
        review = self.widgets.CleanupPlanReview(self.root)
        plan = review.inspect(str(path))
        self.assertIn('protected_candidates', plan)
        for reviewed, no_writers in [(False, False), (True, False), (False, True)]:
            with self.assertRaises(ValueError):
                review.apply_arguments(str(path), reviewed=reviewed, no_active_writers=no_writers)
        with self.assertRaises(ValueError):
            review.apply_arguments(str(path.parent / 'different.json'), reviewed=True, no_active_writers=True)
        digest = review.sha256
        args = review.apply_arguments(str(path), reviewed=True, no_active_writers=True)
        self.assertIn(digest, args)
        self.assertIn('--reviewed-sha256', args)
        self.assertIsNone(review.path)

    def test_resealed_changed_plan_cannot_use_prior_review_in_ui_or_cli(self):
        path = self.plan()
        review = self.widgets.CleanupPlanReview(self.root)
        plan = review.inspect(str(path))
        old_digest = review.sha256
        plan['created_at'] = 'changed after inspection'
        path.write_text(json.dumps(plan))
        (path.parent / 'cleanup_plan.sha256').write_text(self.common.sha256_file(path) + '  cleanup_plan.json\n')
        with self.assertRaisesRegex(RuntimeError, 'changed since review'):
            review.apply_arguments(str(path), reviewed=True, no_active_writers=True)
        with patch.object(sys, 'argv', ['step_maintenance.py', 'cleanup', '--plan', str(path),
                                      '--apply', '--confirm', self.maintenance.CLEANUP_CONFIRMATION,
                                      '--reviewed-sha256', old_digest]):
            with self.assertRaisesRegex(RuntimeError, 'changed since review'):
                self.maintenance.main()
        self.assertFalse((path.parent / 'cleanup_receipt.json').exists())

    def test_applied_plan_excluded_from_selection(self):
        path = self.plan()
        (path.parent / 'cleanup_receipt.json').write_text('{"status":"committed"}')
        review = self.widgets.CleanupPlanReview(self.root)
        options, errors = review.discover()
        self.assertEqual(options, [])
        self.assertIn('already been applied', errors[0])

    def test_invalid_plan_excluded_with_actionable_error(self):
        path = self.plan()
        (path.parent / 'cleanup_plan.sha256').unlink()
        options, errors = self.widgets.CleanupPlanReview(self.root).discover()
        self.assertEqual(options, [])
        self.assertIn('Missing cleanup plan seal', errors[0])

    @unittest.skipUnless(importlib.util.find_spec('ipywidgets'), 'ipywidgets unavailable locally; exercised in CI')
    def test_widget_inspection_selection_change_and_apply_controls(self):
        path = self.plan()
        calls = []
        def run_script(*args, **kwargs):
            calls.append((args, kwargs))
            return SimpleNamespace(ok=True)
        runtime = SimpleNamespace(project=self.root, run_script=run_script)
        with patch('IPython.display.display'):
            controls = self.widgets.CleanupControls(runtime)
        self.assertEqual(controls.selector.value, '')
        self.assertTrue(controls.apply.disabled)
        controls.selector.value = str(path)
        controls._inspect()
        controls.reviewed.value = controls.no_writers.value = True
        self.assertFalse(controls.apply.disabled)
        controls.selector.value = ''
        self.assertTrue(controls.apply.disabled)
        self.assertFalse(controls.reviewed.value)
        controls.selector.value = str(path)
        controls._inspect()
        controls.reviewed.value = controls.no_writers.value = True
        controls._apply()
        self.assertEqual(len(calls), 1)
        self.assertIn('--reviewed-sha256', calls[0][0][1])
        self.assertTrue(controls.apply.disabled)


if __name__ == '__main__':
    unittest.main()
