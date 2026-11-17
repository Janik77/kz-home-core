"""Control-plane tests; no claim of Docker/systemd lifecycle qualification."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import manage_software as manager


class ManagerTests(unittest.TestCase):
    def test_unit_is_manual_and_uses_existing_launcher(self):
        unit = manager.unit_text(Path('/venv/bin/python'), ROOT, Path('/web'), Path('/private/state'))
        self.assertIn('Restart=no', unit)
        self.assertIn('KillSignal=SIGINT', unit)
        self.assertIn('ExecStopPost=', unit)
        self.assertNotIn('[Install]', unit)
        self.assertNotIn('WantedBy', unit)
        self.assertNotIn('init --state', unit)
        self.assertIn('durable_software.py', unit)

    def test_argument_escaping(self):
        self.assertEqual(manager.quote('/a $b%/c'), '"/a $$b%%/c"')
        self.assertEqual(manager.quote('/a $b%/c', command=False), '"/a $b%%/c"')
        for text in ('x\ny', 'x\ry', 'x\0y'):
            with self.assertRaises(ValueError):
                manager.quote(text)

    def test_name_stable_and_scoped(self):
        self.assertEqual(manager.unit_name('/a'), manager.unit_name('/a'))
        self.assertNotEqual(manager.unit_name('/a'), manager.unit_name('/b'))

    def test_modified_or_symlink_unit_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'unit'
            path.write_text('owned')
            path.chmod(0o600)
            manager.checked_unit(path, 'owned')
            with self.assertRaises(ValueError):
                manager.checked_unit(path, 'different')
            link = Path(directory) / 'link'
            link.symlink_to(path)
            with self.assertRaises(ValueError):
                manager.checked_unit(link, 'owned')
            path.chmod(0o666)
            with self.assertRaises(ValueError):
                manager.checked_unit(path, 'owned')

    def test_identity_failure_precedes_systemd(self):
        with patch.object(manager, 'Profile', side_effect=ValueError('identity')), patch.object(manager, 'ctl') as ctl:
            with self.assertRaises(ValueError):
                manager.manage('restart', Path('/a'), Path('/b'), Path(sys.executable))
            ctl.assert_not_called()

    def test_available_user_manager_required_before_install(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'initialized').touch()
            profile = Mock(path=root)
            fake_stack = Mock(validate_web=lambda path: path)
            with patch.object(manager.shutil, 'which', side_effect=lambda name: '/usr/bin/' + name), patch.object(manager, 'Profile', return_value=profile), patch.object(manager, 'validate_core'), patch.dict(sys.modules, {'run_software_stack': fake_stack}), patch.object(manager, 'ctl', side_effect=RuntimeError('no bus')):
                with self.assertRaises(RuntimeError):
                    manager.manage('install', root, root, Path(sys.executable), unit_dir=root / 'units')
            self.assertFalse((root / 'units').exists())

    def test_controls_only_exact_unit_and_refuses_dropins(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'initialized').touch()
            units = root / 'units'
            units.mkdir()
            python = Path(sys.executable).absolute()
            name = manager.unit_name(root)
            path = units / name
            path.write_text(manager.unit_text(python, manager.CORE, root, root, str(python.parent) + ':/usr/bin:/usr/local/bin:/bin'))
            path.chmod(0o600)
            profile = Mock(path=root)
            fake_stack = Mock(validate_web=lambda value: value)
            def invoke(*args, **kwargs):
                return Mock(stdout=str(path) if '--property=FragmentPath' in args else '')
            with patch.object(manager.shutil, 'which', side_effect=lambda name: '/usr/bin/' + name), patch.object(manager, 'Profile', return_value=profile), patch.object(manager, 'validate_core'), patch.dict(sys.modules, {'run_software_stack': fake_stack}), patch.object(manager, 'ctl', side_effect=invoke) as ctl:
                for action in ('start', 'stop', 'restart'):
                    manager.manage(action, root, root, python, unit_dir=units)
                    self.assertEqual(ctl.call_args.args, (action, name))
                manager.manage('status', root, root, python, unit_dir=units)
                self.assertEqual(ctl.call_args.args[0], 'show')
                ctl.reset_mock()
                ctl.side_effect = lambda *args, **kwargs: Mock(stdout=str(path) if '--property=FragmentPath' in args else '/foreign.conf')
                with self.assertRaises(ValueError):
                    manager.manage('restart', root, root, python, unit_dir=units)
                self.assertFalse(any(call.args[0] == 'restart' for call in ctl.call_args_list))


if __name__ == '__main__':
    unittest.main()
