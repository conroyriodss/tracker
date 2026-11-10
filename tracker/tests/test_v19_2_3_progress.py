"""Observable long operations, bounded pin inspection and interrupted children."""
import ast
import contextlib
import hashlib
import importlib
import io
import json
import os
import queue
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from test_v13_notebooks_and_operations import fresh_common, clear_tracker_modules, ROOT, SCRIPTS
from operation_progress import ProgressReporter


class ProgressTests(unittest.TestCase):
    def test_heartbeat_runs_while_main_thread_waits_without_invented_percent(self):
        lines, heartbeat = [], threading.Event()
        def emit(text):
            lines.append(text)
            if 'still running' in text:
                heartbeat.set()
        with ProgressReporter('test scan', interval=.02, emit=emit) as progress:
            progress.update('reading', current='fixture.csv', files_hashed=0)
            progress.advance(bytes_hashed=7)
            self.assertTrue(heartbeat.wait(.5))
        self.assertTrue(any('bytes_hashed=7' in s and 'fixture.csv' in s for s in lines))
        self.assertFalse(any('%' in s for s in lines))
        self.assertIn('finished', lines[-1])

    def test_failure_is_not_reported_as_finished(self):
        lines = []
        with self.assertRaises(OSError):
            with ProgressReporter('broken read', emit=lines.append):
                raise OSError('disconnected')
        self.assertIn('failed', lines[-1])


class RuntimeProgressTests(unittest.TestCase):
    def setUp(self):
        self.old_env = os.environ.copy()
        os.environ.pop('NIH_TRACKER_PRODUCTION_ONLY', None)
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.common = fresh_common(self.base, 'dev')
        self.root = Path(self.common.PROJECT)
        self.module = importlib.import_module('notebook_runtime')
        self.runtime = self.module.NotebookRuntime(notebook_name='test', env='dev', project=self.root,
                                                  scripts=SCRIPTS, enable_prod_writes=False)
        sys.modules.pop('review_workflow', None)
        self.inventory = importlib.import_module('step_project_inventory')

    def tearDown(self):
        clear_tracker_modules()
        sys.modules.pop('review_workflow', None)
        os.environ.clear()
        os.environ.update(self.old_env)
        self.temp.cleanup()

    def script(self, source):
        path = self.base/'probe.py'
        path.write_text(source)
        return path

    def test_silent_child_has_heartbeat_and_completed_log_is_copied(self):
        script = self.script("import time\ntime.sleep(.2)\nprint('done')\n")
        output = io.StringIO()
        with patch.object(self.module, 'HEARTBEAT_SECONDS', .02), contextlib.redirect_stdout(output):
            result = self.runtime.run_script(script)
        self.assertTrue(result.ok)
        self.assertIn('waiting for subprocess output', output.getvalue())
        self.assertEqual(Path(result.log_path).read_bytes(), Path(result.local_log_path).read_bytes())
        self.assertIn('done', Path(result.log_path).read_text())

    def test_disconnected_drive_retains_local_log_and_does_not_pass(self):
        script = self.script("print('observed output')\n")
        with patch.object(self.module.shutil, 'copyfile', side_effect=OSError(107, 'Transport endpoint is not connected')):
            result = self.runtime.run_script(script)
        self.assertFalse(result.ok)
        self.assertEqual(result.returncode, 0)
        self.assertIn('107', result.artifact_sync_error)
        self.assertTrue(Path(result.log_path).is_relative_to(self.runtime.local_debug_dir))
        self.assertIn('observed output', Path(result.log_path).read_text())
        self.assertFalse(json.loads(Path(result.result_path).read_text())['ok'])

    def test_unverifiable_ledger_does_not_pass_successful_child(self):
        script = self.script("print('completed child')\n")
        with patch.object(self.runtime, '_ledger_rows_for_run', side_effect=OSError('Drive unavailable')):
            result = self.runtime.run_script(script)
        self.assertFalse(result.ok)
        self.assertIn('Ledger could not be verified', result.artifact_sync_error)

    @unittest.skipUnless(os.name == 'posix', 'process groups require POSIX')
    def test_unconfirmed_termination_is_not_reported_as_stopped(self):
        from unittest.mock import Mock
        process = Mock(pid=12345)
        process.wait.side_effect = self.module.subprocess.TimeoutExpired('fixture', .01)
        with patch.object(self.module.os, 'killpg') as signal_group:
            self.assertFalse(self.runtime._stop_process(process, grace_seconds=.01))
        self.assertEqual([c.args[1] for c in signal_group.call_args_list],
                         [self.module.signal.SIGTERM, self.module.signal.SIGKILL])

    @unittest.skipUnless(os.name == 'posix', 'process groups require POSIX')
    def test_interrupt_stops_child_and_descendant_and_records_failure(self):
        pidfile = self.base/'child.json'
        script = self.script("import os,sys,json,subprocess,time\n"
                             "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)'])\n"
                             f"open({str(pidfile)!r},'w').write(json.dumps([os.getpid(),child.pid]))\n"
                             "print('ready',flush=True)\ntime.sleep(120)\n")
        class InterruptQueue(queue.Queue):
            def get(self, *args, **kwargs):
                value = super().get(*args, **kwargs)
                if value == ('line', 'ready\n'):
                    raise KeyboardInterrupt()
                return value
        with patch.object(self.module.queue, 'Queue', InterruptQueue):
            with self.assertRaises(KeyboardInterrupt):
                self.runtime.run_script(script)
        parent, child = json.loads(pidfile.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(parent, 0)
        proc = Path(f'/proc/{child}/stat')
        if proc.exists():
            self.assertEqual(proc.read_text().split()[2], 'Z', 'descendant is still running')
        record = self.runtime.run_results()[-1]
        self.assertTrue(record['interrupted'])
        self.assertFalse(record['ok'])
        self.assertTrue(Path(record['result_path']).exists())

    def pin(self, relative, family='card'):
        target = self.root/relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('registry_name,matched_card,how\nExample,R1,fuzzy\n')
        path = self.root/'inputs/active_review_sources.json'
        path.write_text(json.dumps({'schema':'nih-tracker-review-sources/v1','sources':{
            family:{'path':relative,'sha256':hashlib.sha256(target.read_bytes()).hexdigest()}}}))
        return target, path

    def test_pin_inspection_never_discovers_historical_queues(self):
        target, manifest = self.pin('runs/crosswalk/one/crosswalk_review.csv')
        original = manifest.read_bytes()
        with patch.object(Path, 'glob', side_effect=AssertionError('historical discovery')), \
             patch.object(Path, 'rglob', side_effect=AssertionError('historical discovery')):
            report = self.inventory.inspect_pins(self.root)
        self.assertEqual(report['sources'][0]['source_file'], str(target))
        self.assertEqual(manifest.read_bytes(), original)
        self.assertIn('pending counts', report['scope'])

    def test_cross_family_pin_rejected_even_without_discovery(self):
        self.pin('runs/crosswalk/one/crosswalk_review.csv', family='identifier')
        with self.assertRaisesRegex(ValueError, 'outside its queue family'):
            self.inventory.inspect_pins(self.root)

    def test_full_inventory_reports_phases_hashes_and_counts(self):
        target, _ = self.pin('runs/crosswalk/one/crosswalk_review.csv')
        lines = []
        with ProgressReporter('inventory', emit=lines.append) as progress:
            report = self.inventory.collect_inventory(self.root, self.root/'runs/project_inventory/test', progress=progress)
        self.assertTrue(report['complete'])
        hashed = [r for r in report['files'] if r['status']=='hashed']
        self.assertEqual(progress.counts['files_hashed'], len(hashed))
        self.assertEqual(progress.counts['bytes_hashed'], sum(r['size_bytes'] for r in hashed))
        self.assertTrue(any('retention:' in s for s in lines))
        self.assertTrue(any('report: write' in s for s in lines))
        self.assertIn(hashlib.sha256(target.read_bytes()).hexdigest(), [r['sha256'] for r in hashed])

    def test_walk_error_makes_inventory_incomplete(self):
        def broken_walk(*args, **kwargs):
            kwargs['onerror'](OSError(107, 'Disconnected', str(self.root/'runs')))
            return iter([])
        with patch.object(self.inventory, 'retention_report', return_value={'protected':[], 'errors':[]}), \
             patch.object(self.inventory.os, 'walk', side_effect=broken_walk):
            report = self.inventory.collect_inventory(self.root, self.root/'runs/project_inventory/broken')
        self.assertFalse(report['complete'])
        self.assertIn('Disconnected', report['errors'][0]['error'])

    def test_pipeline_interrupt_returns_stopped_status_without_success_marker(self):
        notebook = json.loads((ROOT/'nih_tracker_colab_v19_pipeline.ipynb').read_text())
        source = next(''.join(c['source']) for c in notebook['cells'] if ''.join(c['source']).startswith('RUN_PIPELINE ='))
        tree = ast.parse(source)
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name=='_execute_pipeline')
        # Trigger interruption inside the real function, before any pipeline write.
        from types import SimpleNamespace
        context = {'RUN_PIPELINE':True, 'APPLY_CHANGES':False, 'REFRESH_EXTERNAL_EVIDENCE':False,
                   'ProgressReporter':ProgressReporter,
                   'common':SimpleNamespace(ENV='prod'), 'PIPELINE_SUCCESS_PATH':self.base/'success.json',
                   'PIPELINE_EXCEPTION_PATH':self.base/'exception.txt', 'REQUIRED_INPUTS':[],
                   'PIPELINE_STEPS':[], 'RUNTIME':SimpleNamespace(clear_pipeline_secrets=lambda: None,
                                                                smoke_suite=lambda **kw: (_ for _ in ()).throw(KeyboardInterrupt()),
                                                                run_results=lambda: [{'ok':False,'interrupted':True}]),
                   '_pipeline_status':lambda ok,stage,message,results=None,**kw: {'ok':ok,'stage':stage,'results':results}}
        exec(compile(ast.Module(body=[function], type_ignores=[]), '<pipeline>', 'exec'), context)
        result = context['_execute_pipeline']()
        self.assertFalse(result['ok'])
        self.assertEqual(result['stage'], 'interrupted')
        self.assertFalse(context['PIPELINE_SUCCESS_PATH'].exists())


if __name__ == '__main__':
    unittest.main()
