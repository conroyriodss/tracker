"""Bound installer I/O and reject notebook/runtime version mismatches."""
import contextlib
import hashlib
import importlib
import io
import json
import os
import runpy
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from test_v13_notebooks_and_operations import fresh_common, clear_tracker_modules, ROOT


class InstallationPatchTests(unittest.TestCase):
    def setUp(self):
        self.old_env = os.environ.copy()
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        os.environ['NIH_TRACKER_PRODUCTION_ONLY'] = '1'
        self.common = fresh_common(self.base, 'prod')
        self.root = Path(self.common.PROJECT)
        self.installer = importlib.import_module('step_initialize_environments')
        self.release = self.base / 'release'
        (self.release / 'scripts').mkdir(parents=True)
        (self.release / 'scripts/step_initialize_environments.py').write_text('# installer fixture\n')
        (self.release / 'scripts/work.py').write_text('new code\n')

    def tearDown(self):
        clear_tracker_modules()
        sys.modules.pop('review_workflow', None)
        os.environ.clear()
        os.environ.update(self.old_env)
        self.temp.cleanup()

    def plan(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return self.installer.build_plan(source_env='prod', release_root=self.release,
                                             mode='refresh-code', run_dir=self.root / 'runs/install_release' / uuid.uuid4().hex)

    def install_fixture(self):
        for source in self.installer.collect_release_files(self.release):
            path = self.root / source.relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(source.data)

    def reseal(self, path, data):
        path.write_text(json.dumps(data))
        (path.parent / self.installer.PLAN_SEAL_NAME).write_text(self.common.sha256_file(path)+'  '+path.name+'\n')

    def test_unchanged_release_has_zero_payload_writes_and_one_target_hash_each(self):
        self.install_fixture()
        with patch.object(self.installer, 'sha256_file', wraps=self.installer.sha256_file) as hashing:
            path = self.plan()
        plan = json.loads(path.read_text())
        self.assertTrue(all(e['action']=='unchanged' and not e['payload_staged'] for e in plan['entries']))
        self.assertTrue(all(e['payload_sha256']=='' for e in plan['entries']))
        self.assertFalse((path.parent/'payload').exists())
        for entry in plan['entries']:
            target = self.root/entry['relative_path']
            self.assertEqual(sum(Path(call.args[0])==target for call in hashing.call_args_list), 1)
        result = self.installer.apply_plan(path, confirm='INSTALL_PRODUCTION_RELEASE')
        self.assertEqual(result['files_written'], 0)

    def test_only_changed_code_staged_and_live_reference_preserved(self):
        (self.release/'reference').mkdir()
        (self.release/'reference/snapshot.csv').write_text('distributed reference')
        self.install_fixture()
        (self.root/'scripts/work.py').write_text('old code')
        (self.root/'reference/snapshot.csv').write_text('live reference')
        path = self.plan()
        staged = [p.relative_to(path.parent/'payload').as_posix() for p in (path.parent/'payload').rglob('*') if p.is_file()]
        self.assertEqual(staged, ['scripts/work.py'])
        self.installer.apply_plan(path, confirm='INSTALL_PRODUCTION_RELEASE')
        self.assertEqual((self.root/'scripts/work.py').read_text(), 'new code\n')
        self.assertEqual((self.root/'reference/snapshot.csv').read_text(), 'live reference')

    def test_changed_payload_blocks_all_install_writes(self):
        path = self.plan()
        (path.parent/'payload/scripts/work.py').write_text('tampered')
        with self.assertRaisesRegex(RuntimeError, 'payload changed'):
            self.installer.apply_plan(path, confirm='INSTALL_PRODUCTION_RELEASE')
        self.assertFalse((self.root/'scripts/work.py').exists())
        self.assertFalse((self.root/'scripts/step_initialize_environments.py').exists())

    def test_changed_target_still_blocks_install(self):
        self.install_fixture()
        (self.root/'scripts/work.py').write_text('old code')
        path = self.plan()
        (self.root/'scripts/work.py').write_text('concurrent change')
        with self.assertRaisesRegex(RuntimeError, 'Target changed'):
            self.installer.apply_plan(path, confirm='INSTALL_PRODUCTION_RELEASE')
        self.assertEqual((self.root/'scripts/work.py').read_text(), 'concurrent change')

    def test_legacy_all_payload_plan_still_applies(self):
        path = self.plan()
        data = json.loads(path.read_text())
        del data['payload_scope']
        for entry in data['entries']:
            del entry['payload_staged']
        self.reseal(path, data)
        self.assertEqual(self.installer.apply_plan(path, confirm='INSTALL_PRODUCTION_RELEASE')['files_written'], 2)

    def test_write_action_without_staged_payload_is_rejected(self):
        path = self.plan()
        data = json.loads(path.read_text())
        data['entries'][0]['payload_staged'] = False
        self.reseal(path, data)
        with self.assertRaisesRegex(ValueError, 'no staged payload'):
            self.installer.apply_plan(path, confirm='INSTALL_PRODUCTION_RELEASE')

    def test_preview_ledger_hashes_plan_not_project_and_reports_progress(self):
        def only_files(original):
            def checked(path, *args, **kwargs):
                self.assertFalse(Path(path).is_dir(), f'Unexpected directory hash: {path}')
                return original(path, *args, **kwargs)
            return checked
        output = io.StringIO()
        argv = ['step_initialize_environments.py', '--source-env', 'prod', '--mode', 'refresh-code', '--release-root', str(self.release)]
        with patch.object(sys, 'argv', argv), contextlib.redirect_stdout(output), \
             patch.object(self.common, 'sha1', side_effect=only_files(self.common.sha1)), \
             patch.object(self.common, 'sha256_path', side_effect=only_files(self.common.sha256_path)), \
             patch.object(self.installer, 'collect_project_files', side_effect=AssertionError('Project data scanned')):
            self.assertEqual(self.installer.main(), 0)
        for stage in ('[start]', '[compare prod', '[stage', '[audit]', '[done]'):
            self.assertIn(stage, output.getvalue())

    def test_notebook_blocks_old_missing_and_mixed_code_before_runtime_creation(self):
        namespace = runpy.run_path(str(ROOT/'extend_operations_notebooks.py'))
        check = namespace['RUNTIME_CHECK']
        context = {'SCRIPTS': self.root/'scripts', 'RUNTIME': object()}
        with self.assertRaisesRegex(RuntimeError, 'manifest is missing'):
            exec(check, context)
        self.assertNotIn('RUNTIME', context)
        manifest_path = self.root/'RELEASE_MANIFEST.json'
        manifest_path.write_text('{"release":"19.2"}')
        with self.assertRaisesRegex(RuntimeError, 'Drive has 19.2'):
            exec(check, context)
        files = {}
        names = ('common.py','notebook_runtime.py','run_provenance.py','code_provenance.py',
                 'step_initialize_environments.py','step_maintenance.py','maintenance_widgets.py','step_project_inventory.py','operation_progress.py')
        for name in names:
            path = self.root/'scripts'/name
            path.parent.mkdir(exist_ok=True)
            path.write_text('# installed fixture\n')
            files['scripts/'+name] = {'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        manifest_path.write_text(json.dumps({'release':'19.2.3','files':files}))
        exec(check, context)
        (self.root/'scripts/maintenance_widgets.py').unlink()
        with self.assertRaisesRegex(RuntimeError, 'maintenance_widgets.py'):
            exec(check, context)
        notebook = json.loads((ROOT/'nih_tracker_colab_v19_maintenance.ipynb').read_text())
        code = '\n'.join(''.join(c['source']) for c in notebook['cells'] if c['cell_type']=='code')
        self.assertIn('sys.executable, "-u",', code)
        self.assertIn('PREVIEW COMPLETE: release code has NOT been installed', code)

    def test_inspect_pins_validates_without_replacing_them(self):
        for name in ('review_workflow','step_project_inventory'):
            sys.modules.pop(name, None)
        inventory = importlib.import_module('step_project_inventory')
        queue = self.root/'runs/crosswalk/example/crosswalk_review.csv'
        queue.parent.mkdir(parents=True)
        queue.write_text('registry_name,matched_card,how\nExample,R1,fuzzy\n')
        inventory.pin_sources(self.root)
        manifest = self.root/'inputs/active_review_sources.json'
        before = manifest.read_bytes()
        output = io.StringIO()
        with patch.object(sys, 'argv', ['step_project_inventory.py','inspect-review']), contextlib.redirect_stdout(output):
            self.assertEqual(inventory.main(), 0)
        self.assertEqual(manifest.read_bytes(), before)
        self.assertEqual(json.loads(output.getvalue())['status'], 'pinned')
        queue.write_text(queue.read_text()+'Changed,R2,fuzzy\n')
        with patch.object(sys, 'argv', ['step_project_inventory.py','inspect-review']):
            with self.assertRaisesRegex(ValueError, 'changed'):
                inventory.main()
        self.assertEqual(manifest.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
