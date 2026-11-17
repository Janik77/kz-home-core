"""Control tests only, not real systemd lifecycle qualification."""
import json
from contextlib import nullcontext
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import manage_software as m

class ManagerTests(unittest.TestCase):
    def fixture(self, root):
        state = root / 'state'; state.mkdir()
        data = dict(profile_path=str(state), directory_identity=f'{state.stat().st_dev}:{state.stat().st_ino}', owner='a'*32)
        profile = state / 'profile.json'; profile.write_text(json.dumps(data)); profile.chmod(0o600)
        units = root / 'units'; units.mkdir(mode=0o700)
        config = dict(m.binding(state), version=1, core=str(m.CORE), web='/missing/web', python='/missing/venv/python', executable_path='/missing/node:/usr/bin')
        name = m.unit_name(state); path = units / name
        path.write_text(m.unit_text(Path(config['python']), m.CORE, Path(config['web']), state, config['executable_path'])); path.chmod(0o600)
        manifest = units / (name + '.json'); manifest.write_text(json.dumps(config)); manifest.chmod(0o600)
        return state, units, path, manifest

    def test_install_saves_private_config_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            state, units, path, manifest = self.fixture(Path(d))
            path.unlink(); manifest.unlink()
            profile = Mock(); profile.lock.side_effect = nullcontext
            with patch.object(m, 'ctl', return_value=Mock(stdout='')), patch.object(m, 'runtime_preflight', return_value=(profile, Path('/web'))), patch.object(m.shutil, 'which', side_effect=lambda name: '/usr/bin/' + name):
                m.manage('install', state, Path('/web'), Path('/venv/python'), unit_dir=units)
                config = json.loads(manifest.read_text())
                self.assertEqual(config['web'], '/web')
                self.assertEqual(manifest.stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                before = manifest.read_bytes()
                with self.assertRaises(ValueError):
                    m.manage('install', state, Path('/web'), Path('/different/python'), unit_dir=units)
                self.assertEqual(before, manifest.read_bytes())

    def test_stop_status_with_missing_runtimes_and_changed_path(self):
        with tempfile.TemporaryDirectory() as d:
            state, units, path, manifest = self.fixture(Path(d))
            def ctl(*args, **kwargs):
                return Mock(stdout=str(path) if '--property=FragmentPath' in args else 'no' if '--property=NeedDaemonReload' in args else '')
            with patch.object(m, 'ctl', side_effect=ctl) as calls, patch.object(m, 'runtime_preflight', side_effect=AssertionError('must not validate runtime')), patch.object(m.shutil, 'which', side_effect=AssertionError('must not use current PATH')), patch.dict(m.os.environ, {'PATH':'/changed/nvm'}):
                for action in ('stop', 'status'):
                    m.manage(action, state, None, Path('/different/python'), unit_dir=units)
                self.assertTrue(any(c.args == ('stop', path.name) for c in calls.call_args_list))

    def test_rejects_stale_loaded_unit_dropin_foreign_fragment(self):
        for bad in ('stale', 'dropin', 'fragment'):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as d:
                state, units, path, manifest = self.fixture(Path(d))
                def ctl(*args, **kwargs):
                    if '--property=FragmentPath' in args: return Mock(stdout='/foreign' if bad == 'fragment' else str(path))
                    if '--property=DropInPaths' in args: return Mock(stdout='/override' if bad == 'dropin' else '')
                    return Mock(stdout='yes' if bad == 'stale' else 'no')
                with patch.object(m, 'ctl', side_effect=ctl) as calls:
                    with self.assertRaises(ValueError): m.manage('stop', state, None, Path('/p'), unit_dir=units)
                    self.assertFalse(any(c.args[0] == 'stop' for c in calls.call_args_list))

    def test_identity_mismatch_precedes_systemd(self):
        with tempfile.TemporaryDirectory() as d:
            state, units, path, manifest = self.fixture(Path(d))
            profile = state / 'profile.json'; data=json.loads(profile.read_text()); data['directory_identity']='bad'; profile.write_text(json.dumps(data))
            with patch.object(m, 'ctl') as ctl:
                with self.assertRaises(ValueError): m.manage('stop', state, None, Path('/p'), unit_dir=units)
                ctl.assert_not_called()

    def test_unit_manifest_and_directory_tampering(self):
        for bad in ('unit', 'manifest_mode', 'manifest_owner', 'directory', 'parent', 'symlink'):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as d:
                root=Path(d); state, units, path, manifest = self.fixture(root)
                if bad == 'unit': path.write_text('foreign')
                if bad == 'manifest_mode': manifest.chmod(0o644)
                if bad == 'manifest_owner':
                    data=json.loads(manifest.read_text()); data['owner']='b'*32; manifest.write_text(json.dumps(data))
                if bad == 'directory': units.chmod(0o777)
                if bad == 'parent': root.chmod(0o777)
                if bad == 'symlink': manifest.unlink(); manifest.symlink_to(state/'profile.json')
                with patch.object(m, 'ctl', return_value=Mock(stdout='')):
                    with self.assertRaises(ValueError): m.manage('stop', state, None, Path('/p'), unit_dir=units)

    def test_start_and_restart_still_require_runtime(self):
        with tempfile.TemporaryDirectory() as d:
            state, units, path, manifest = self.fixture(Path(d))
            def ctl(*args, **kwargs): return Mock(stdout=str(path) if '--property=FragmentPath' in args else 'no' if '--property=NeedDaemonReload' in args else '')
            with patch.object(m, 'ctl', side_effect=ctl), patch.object(m, 'runtime_preflight', side_effect=ValueError('unavailable')) as preflight:
                for action in ('start','restart'):
                    with self.assertRaises(ValueError): m.manage(action,state,None,Path('/p'),unit_dir=units)
                self.assertEqual(preflight.call_count,2)
                self.assertEqual(preflight.call_args.args, (state, Path('/missing/web'), Path('/missing/venv/python')))

    def test_escaping_and_manual_unit(self):
        self.assertEqual(m.quote('/a $b%'), '"/a $$b%%"')
        for bad in ('\n','\r','\0'):
            with self.assertRaises(ValueError): m.quote(bad)
        unit=m.unit_text(Path('/p'),Path('/c'),Path('/w'),Path('/s'))
        self.assertIn('Restart=no',unit); self.assertIn('KillSignal=SIGINT',unit); self.assertNotIn('[Install]',unit)

if __name__ == '__main__': unittest.main()
