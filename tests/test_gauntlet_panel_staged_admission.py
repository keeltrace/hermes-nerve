"""GUI installer admission regression with tiny payloads and no native CLI calls."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from hermes_nerve.control_panel import ControlPanel, PanelError


class TestPanelStagedAdmission(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.source = root / 'source'
        package = self.source / 'hermes_nerve'
        package.mkdir(parents=True)
        (self.source / 'plugin.yaml').write_text('name: nerve\nversion: fixture\n')
        (self.source / '__init__.py').write_text('# synthetic registration\n')
        (self.source / 'CONTRIBUTORS.md').write_text('Fixture credit: @phuongvm and @rharmanca\n')
        (self.source / 'CHANGELOG.md').write_text('Fixture release provenance\n')
        (package / '__init__.py').write_text('# synthetic package\n')
        (package / 'cli.py').write_text('# synthetic CLI\n')
        self.home = root / 'home'
        self.dest = self.home / 'plugins' / 'hermes-nerve'
        self.dest.mkdir(parents=True)
        (self.dest / 'plugin.yaml').write_text('name: nerve\nversion: old\n')
        (self.dest / 'old-marker').write_bytes(b'previous installation')
        (self.home / 'config.yaml').write_text('{}\n')
        (self.home / 'users.json').write_text('{}\n')
        self.panel = ControlPanel(home=self.home, source=self.source)
        # Status/identity hashing is unrelated to admission ordering; do not hash
        # or copy the full source checkout in these tiny transaction fixtures.
        self.addCleanup(patch.stopall)
        patch.object(self.panel, 'status', return_value={'warnings': [], 'fresh': False}).start()
        patch('hermes_nerve.control_panel.shutil.which', return_value='/fixture/hermes').start()

    def test_rejection_preserves_existing_install_and_never_enables(self):
        calls = []
        def native(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 1 if 'validate' in args else 0, '', '')
        before = {name: (self.home / name).read_bytes() for name in ('config.yaml', 'users.json')}
        with patch('hermes_nerve.control_panel.subprocess.run', side_effect=native):
            preview = self.panel.install_preview()
            with self.assertRaisesRegex(PanelError, 'validation'):
                self.panel.install({'preview_id': preview['preview_id']})
        self.assertEqual([cmd[-2] for cmd in calls], ['validate'])
        self.assertEqual((self.dest / 'old-marker').read_bytes(), b'previous installation')
        self.assertEqual(before, {name: (self.home / name).read_bytes() for name in before})
        self.assertEqual(sorted(p.name for p in self.dest.parent.iterdir()), ['hermes-nerve'])

    def test_gui_payload_excludes_checkout_private_files(self):
        package = self.source / 'hermes_nerve'
        for name in ('.env', '.git/config', 'state.sqlite', 'web/.env',
                     '.venv/private.py', 'scratch.bak.py', 'web/.env.js', 'web/app.bak.js'):
            target = package / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('synthetic private fixture, not a credential\n')
        (package / 'web/app.js').write_text('// shipped runtime fixture\n')
        with patch('hermes_nerve.control_panel.subprocess.run',
                   return_value=subprocess.CompletedProcess([], 0, '', '')):
            preview = self.panel.install_preview()
            self.panel.install({'preview_id': preview['preview_id']})
        shipped = {str(p.relative_to(self.dest / 'hermes_nerve'))
                   for p in (self.dest / 'hermes_nerve').rglob('*') if p.is_file()}
        self.assertEqual(shipped, {'__init__.py', 'cli.py', 'web/app.js'})

    def test_gui_assets_and_modules_are_preserved_exactly(self):
        package = self.source / 'hermes_nerve'
        assets = ('web/guide.html', 'web/styles.css', 'web/app.js',
                  'policy/safety.txt', 'research/safety.md', 'nested/module.py')
        expected = {'__init__.py': b'# synthetic package\n', 'cli.py': b'# synthetic CLI\n'}
        for name in assets:
            source = package / name
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(('fixture: ' + name + '\n').encode())
            expected[name] = source.read_bytes()
        staged = {}
        def native(args, **kwargs):
            if args[-2] == 'validate':
                staged.update({str(p.relative_to(Path(args[-1]) / 'hermes_nerve')): p.read_bytes()
                               for p in (Path(args[-1]) / 'hermes_nerve').rglob('*') if p.is_file()})
            return subprocess.CompletedProcess(args, 0, '', '')
        with patch('hermes_nerve.control_panel.subprocess.run', side_effect=native):
            preview = self.panel.install_preview()
            self.panel.install({'preview_id': preview['preview_id']})
        actual = {str(p.relative_to(self.dest / 'hermes_nerve')): p.read_bytes()
                  for p in (self.dest / 'hermes_nerve').rglob('*') if p.is_file()}
        self.assertEqual(staged, expected)
        self.assertEqual(actual, expected)

    def test_selected_package_symlink_is_rejected_before_native_admission(self):
        outside = self.source.parent / 'private.py'
        outside.write_text('synthetic_private = True\n')
        entry = self.source / 'hermes_nerve/cli.py'
        entry.unlink()
        entry.symlink_to(outside)
        with patch('hermes_nerve.control_panel.subprocess.run',
                   return_value=subprocess.CompletedProcess([], 0, '', '')) as native:
            preview = self.panel.install_preview()
            with self.assertRaisesRegex(PanelError, 'symlink'):
                self.panel.install({'preview_id': preview['preview_id']})
        native.assert_not_called()
        self.assertEqual((self.dest / 'old-marker').read_bytes(), b'previous installation')
        self.assertEqual(sorted(p.name for p in self.dest.parent.iterdir()), ['hermes-nerve'])

    def test_gui_install_delivers_exact_contributor_and_release_credit(self):
        with patch('hermes_nerve.control_panel.subprocess.run',
                   return_value=subprocess.CompletedProcess([], 0, '', '')):
            preview = self.panel.install_preview()
            result = self.panel.install({'preview_id': preview['preview_id']})
        self.assertTrue(result['ok'])
        for name in ('CONTRIBUTORS.md', 'CHANGELOG.md'):
            with self.subTest(document=name):
                target = self.dest / name
                self.assertTrue(target.is_file(), f'GUI installer omitted {name}')
                self.assertEqual(target.read_bytes(), (self.source / name).read_bytes())

    def test_missing_required_credit_stops_before_validation_or_replacement(self):
        (self.source / 'CONTRIBUTORS.md').unlink()
        with patch('hermes_nerve.control_panel.subprocess.run',
                   return_value=subprocess.CompletedProcess([], 0, '', '')) as native:
            preview = self.panel.install_preview()
            with self.assertRaisesRegex(PanelError, 'Install failed'):
                self.panel.install({'preview_id': preview['preview_id']})
        native.assert_not_called()
        self.assertEqual((self.dest / 'old-marker').read_bytes(), b'previous installation')
        self.assertEqual(sorted(p.name for p in self.dest.parent.iterdir()), ['hermes-nerve'])

    def test_cli_disappearing_after_preview_fails_before_native_call(self):
        with patch('hermes_nerve.control_panel.shutil.which',
                   side_effect=['/fixture/hermes', '/fixture/hermes', None]):
            with patch('hermes_nerve.control_panel.subprocess.run',
                       return_value=subprocess.CompletedProcess([], 0, '', '')) as native:
                preview = self.panel.install_preview()
                with self.assertRaisesRegex(PanelError, 'no longer available'):
                    self.panel.install({'preview_id': preview['preview_id']})
        native.assert_not_called()
        self.assertEqual((self.dest / 'old-marker').read_bytes(), b'previous installation')
        self.assertEqual(sorted(p.name for p in self.dest.parent.iterdir()), ['hermes-nerve'])

    def test_validated_staged_bytes_are_the_bytes_enabled(self):
        calls, staged, enabled = [], {}, {}
        def native(args, **kwargs):
            op = args[-2]
            calls.append(op)
            self.assertEqual(kwargs['env']['HERMES_HOME'], str(self.home))
            if op == 'validate':
                self.assertTrue((self.dest / 'old-marker').is_file())
                stage = Path(args[-1])
                self.assertNotEqual(stage, self.dest)
                staged.update({str(p.relative_to(stage)): p.read_bytes()
                               for p in stage.rglob('*') if p.is_file()})
                self.assertIn('hermes_nerve/cli.py', staged)
            elif op == 'enable':
                enabled.update({str(p.relative_to(self.dest)): p.read_bytes()
                                for p in self.dest.rglob('*') if p.is_file()})
            else:
                self.fail(f'unexpected native operation: {op}')
            return subprocess.CompletedProcess(args, 0, '', '')
        with patch('hermes_nerve.control_panel.subprocess.run', side_effect=native):
            preview = self.panel.install_preview()
            result = self.panel.install({'preview_id': preview['preview_id']})
        self.assertTrue(result['ok'])
        self.assertEqual(calls, ['validate', 'enable'])
        self.assertTrue(staged)
        self.assertEqual(enabled, staged)
        self.assertTrue(result['restart_required'])
        self.assertEqual((Path(result['backup']) / 'old-marker').read_bytes(), b'previous installation')

    def _native_discovered_nerve(self):
        try:
            from hermes_cli.plugins_discovery import scan_directory
        except ImportError:
            self.skipTest('Hermes native manifest discovery is unavailable; not native qualification')
        return [item for item in scan_directory(self.dest.parent, 'user') if item.name == 'nerve']

    def _never_imported_entrypoints(self):
        self._native_discovered_nerve()  # Skip before entering the install error wrapper if unavailable.
        for root in (self.source, self.dest):
            (root / '__init__.py').write_text('raise AssertionError("manifest-only probe must not import plugin")\n')

    def test_gui_stage_is_not_discovered_before_admission(self):
        self._never_imported_entrypoints()
        def native(args, **kwargs):
            if args[-2] == 'validate':
                self.assertTrue((self.dest / 'old-marker').exists())
                self.assertEqual(len(self._native_discovered_nerve()), 1)
            return subprocess.CompletedProcess(args, 0, '', '')
        with patch('hermes_nerve.control_panel.subprocess.run', side_effect=native):
            preview = self.panel.install_preview()
            self.panel.install({'preview_id': preview['preview_id']})

    def test_gui_retained_backup_is_not_discovered_after_install(self):
        self._never_imported_entrypoints()
        with patch('hermes_nerve.control_panel.subprocess.run',
                   return_value=subprocess.CompletedProcess([], 0, '', '')):
            preview = self.panel.install_preview()
            result = self.panel.install({'preview_id': preview['preview_id']})
        self.assertEqual((Path(result['backup']) / 'old-marker').read_bytes(), b'previous installation')
        self.assertEqual(len(self._native_discovered_nerve()), 1)

    def test_gui_archive_symlink_rejected_without_following_or_native_call(self):
        outside = self.home.parent / 'outside'
        outside.mkdir()
        (outside / 'marker').write_bytes(b'untouched external fixture')
        archive = self.dest.parent / '__nerve_backups__'
        archive.symlink_to(outside, target_is_directory=True)
        with patch('hermes_nerve.control_panel.subprocess.run') as native:
            preview = self.panel.install_preview()
            with self.assertRaises(PanelError):
                self.panel.install({'preview_id': preview['preview_id']})
        native.assert_not_called()
        self.assertTrue(archive.is_symlink())
        self.assertEqual(sorted(p.name for p in outside.iterdir()), ['marker'])
        self.assertEqual((outside / 'marker').read_bytes(), b'untouched external fixture')
        self.assertEqual((self.dest / 'old-marker').read_bytes(), b'previous installation')

    def test_gui_enable_failure_restores_discovery_profile_and_install(self):
        self._never_imported_entrypoints()
        for name, mode in (('config.yaml', 0o600), ('users.json', 0o640)):
            (self.home / name).chmod(mode)
        before_modes = {name: (self.home / name).stat().st_mode & 0o7777 for name in ('config.yaml', 'users.json')}
        before = {name: (self.home / name).read_bytes() for name in ('config.yaml', 'users.json')}
        def native(args, **kwargs):
            if args[-2] == 'enable':
                for name in before:
                    (self.home / name).write_text('changed by synthetic activation\n')
                    (self.home / name).chmod(0o666)
                return subprocess.CompletedProcess(args, 48, '', '')
            return subprocess.CompletedProcess(args, 0, '', '')
        with patch('hermes_nerve.control_panel.subprocess.run', side_effect=native):
            preview = self.panel.install_preview()
            with self.assertRaisesRegex(PanelError, 'enable failed'):
                self.panel.install({'preview_id': preview['preview_id']})
        self.assertEqual(before, {name: (self.home / name).read_bytes() for name in before})
        self.assertEqual(before_modes, {name: (self.home / name).stat().st_mode & 0o7777 for name in before})
        self.assertEqual((self.dest / 'old-marker').read_bytes(), b'previous installation')
        self.assertEqual(len(self._native_discovered_nerve()), 1)
        self.assertEqual(sorted(p.name for p in self.dest.parent.iterdir()), ['hermes-nerve'])

    def test_gui_reserved_stage_and_backup_collisions_preserve_existing_data(self):
        archive = self.dest.parent / '__nerve_backups__'
        archive.mkdir(mode=0o700)
        outside = self.home.parent / 'collision-outside'
        outside.mkdir()
        (outside / 'marker').write_bytes(b'outside unchanged')
        for kind in ('stage', 'backup'):
            for symlink in (False, True):
                with self.subTest(kind=kind, symlink=symlink):
                    name = ('hermes-nerve.installing-nonce' if kind == 'stage'
                            else 'hermes-nerve.before-panel-stamp-nonce')
                    collision = archive / name
                    if symlink:
                        collision.symlink_to(outside, target_is_directory=True)
                    else:
                        collision.mkdir()
                        (collision / 'marker').write_bytes(b'prior archive unchanged')
                    try:
                        with patch('hermes_nerve.control_panel.secrets.token_hex', return_value='nonce'), \
                             patch('hermes_nerve.control_panel.datetime') as clock, \
                             patch('hermes_nerve.control_panel.subprocess.run',
                                   return_value=subprocess.CompletedProcess([], 0, '', '')) as native:
                            clock.now.return_value.strftime.return_value = 'stamp'
                            preview = self.panel.install_preview()
                            with self.assertRaises(PanelError):
                                self.panel.install({'preview_id': preview['preview_id']})
                        self.assertEqual([call.args[0][-2] for call in native.call_args_list],
                                         [] if kind == 'stage' else ['validate'])
                        self.assertEqual((self.dest / 'old-marker').read_bytes(), b'previous installation')
                        self.assertEqual((outside / 'marker').read_bytes(), b'outside unchanged')
                        if symlink:
                            self.assertTrue(collision.is_symlink())
                        else:
                            self.assertEqual((collision / 'marker').read_bytes(), b'prior archive unchanged')
                    finally:
                        if collision.is_symlink():
                            collision.unlink()
                        else:
                            (collision / 'marker').unlink()
                            collision.rmdir()

    def test_gui_cross_filesystem_archive_is_rejected_before_native_call(self):
        import os
        archive = self.dest.parent / '__nerve_backups__'
        archive.mkdir(mode=0o700)
        ordinary_stat = Path.stat
        def stat(path, **kwargs):
            result = ordinary_stat(path, **kwargs)
            if path == archive:
                return os.stat_result((result.st_mode, result.st_ino, result.st_dev + 1, *result[3:]))
            return result
        with patch.object(Path, 'stat', new=stat), \
             patch('hermes_nerve.control_panel.subprocess.run') as native:
            preview = self.panel.install_preview()
            with self.assertRaisesRegex(PanelError, 'filesystem'):
                self.panel.install({'preview_id': preview['preview_id']})
        self.assertEqual(native.call_count, 0)
        self.assertEqual(list(archive.iterdir()), [])
        self.assertEqual((self.dest / 'old-marker').read_bytes(), b'previous installation')

    def test_gui_failed_host_restore_preserves_private_recovery_evidence(self):
        from hermes_nerve.control_panel import owner_only_write
        before = (self.home / 'config.yaml').read_bytes()
        def native(args, **kwargs):
            if args[-2] == 'enable':
                (self.home / 'config.yaml').write_bytes(b'failed activation')
                return subprocess.CompletedProcess(args, 48, '', '')
            return subprocess.CompletedProcess(args, 0, '', '')
        def write(path, data):
            if path == self.home / 'config.yaml':
                raise OSError('synthetic restoration failure')
            return owner_only_write(path, data)
        with patch('hermes_nerve.control_panel.owner_only_write', side_effect=write), \
             patch('hermes_nerve.control_panel.subprocess.run', side_effect=native):
            preview = self.panel.install_preview()
            with self.assertRaisesRegex(PanelError, 'INCOMPLETE'):
                self.panel.install({'preview_id': preview['preview_id']})
        evidence = list((self.dest.parent / '__nerve_backups__').glob('profile-before-*'))
        self.assertEqual(len(evidence), 1)
        self.assertEqual((evidence[0] / 'config.yaml').read_bytes(), before)
        self.assertEqual(evidence[0].stat().st_mode & 0o777, 0o700)
        self.assertEqual((evidence[0] / 'config.yaml').stat().st_mode & 0o777, 0o600)
        self.assertTrue((evidence[0] / 'snapshot.json').is_file())
        self.assertEqual((self.dest / 'old-marker').read_bytes(), b'previous installation')

    def test_gui_failed_activation_restores_absent_and_present_host_state(self):
        names = ('config.yaml', 'users.json')
        for config_present in (False, True):
            for users_present in (False, True):
                with self.subTest(config_present=config_present, users_present=users_present):
                    expected = dict(zip(names, (config_present, users_present)))
                    for name, present in expected.items():
                        path = self.home / name
                        if present:
                            path.write_bytes(b'original host fixture\n')
                        else:
                            path.unlink(missing_ok=True)
                    def native(args, **kwargs):
                        if args[-2] == 'enable':
                            for name in names:
                                (self.home / name).write_bytes(b'new activation data\n')
                            return subprocess.CompletedProcess(args, 48, '', '')
                        return subprocess.CompletedProcess(args, 0, '', '')
                    with patch('hermes_nerve.control_panel.subprocess.run', side_effect=native):
                        preview = self.panel.install_preview()
                        with self.assertRaisesRegex(PanelError, 'enable failed'):
                            self.panel.install({'preview_id': preview['preview_id']})
                    for name, present in expected.items():
                        self.assertEqual((self.home / name).exists(), present)
                        if present:
                            self.assertEqual((self.home / name).read_bytes(), b'original host fixture\n')
                    self.assertEqual((self.dest / 'old-marker').read_bytes(), b'previous installation')


if __name__ == '__main__':
    unittest.main()
