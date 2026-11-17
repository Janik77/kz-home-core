"""Manage ONE existing software-only profile with an opt-in systemd user unit.

Does not initialize, upgrade, rebind, delete, enable at boot or alter Docker/WSL.
"""
import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys

try:
    from .durable_software import CORE, Profile, validate_core
except ImportError:
    from durable_software import CORE, Profile, validate_core


def quote(value, *, command=True):
    value = str(value)
    if any(c in value for c in ('\n', '\r', '\0')):
        raise ValueError('Control characters in path')
    value = value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%')
    if command:
        value = value.replace('$', '$$')
    return '"' + value + '"'


def unit_name(state):
    return 'kzhome-software-' + hashlib.sha256(str(state).encode()).hexdigest()[:16] + '.service'


def unit_text(python, core, web, state, executable_path='/usr/local/bin:/usr/bin:/bin'):
    command = ' '.join(quote(x) for x in (python, '-B', core / 'tools/durable_software.py'))
    return f'''[Unit]
Description=KZ Home isolated software profile (manual lifecycle)

[Service]
Type=simple
WorkingDirectory={quote(core, command=False)}
Environment={quote('PATH=' + executable_path, command=False)}
ExecStart={command} start --state {quote(state)} --web-root {quote(web)}
ExecStopPost={command} stop --state {quote(state)}
KillSignal=SIGINT
KillMode=mixed
TimeoutStopSec=240
Restart=no
UMask=0077
StandardOutput=journal
StandardError=journal
'''


def checked_unit(path, expected):
    if path.is_symlink() or not path.is_file() or path.read_text() != expected:
        raise ValueError('Unit missing or changed; refuse to control a different installation')
    if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o022:
        raise ValueError('Unsafe unit ownership or permissions')


def ctl(*args, capture=False):
    return subprocess.run(['systemctl', '--user', *args], check=True,
                          text=True, capture_output=capture)


def manage(action, state, web, python, *, unit_dir=None):
    # Profile enforces exact pins/path/device/inode before any systemd changes.
    profile = Profile(state)
    if not (profile.path / 'initialized').is_file():
        raise ValueError('Incomplete profile; do not repair markers manually')
    validate_core()
    sys.path.insert(0, str(CORE / 'tools'))
    from run_software_stack import validate_web
    web = validate_web(web)
    python = python.absolute()  # preserve venv executable, do not resolve its symlink
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError('Python executable unavailable')
    state = profile.path
    directory = unit_dir or Path.home() / '.config/systemd/user'
    if any(p.is_symlink() for p in (directory, *directory.parents)):
        raise ValueError('Symlink unit directory refused')
    name = unit_name(state)
    path = directory / name
    # User managers do not inherit an interactive shell's nvm/Node installation.
    bins = [str(python.parent)]
    for executable_name in ('node', 'npm', 'docker'):
        executable = shutil.which(executable_name)
        if executable is None:
            raise ValueError('Required executable unavailable: ' + executable_name)
        bins.append(str(Path(executable).parent))
    bins.extend(('/usr/local/bin', '/usr/bin', '/bin'))
    expected = unit_text(python, CORE, web, state, ':'.join(dict.fromkeys(bins)))
    # No enable/linger or daemon startup; an unavailable user manager is a blocker.
    ctl('show-environment', capture=True)
    if action == 'install':
        with profile.lock():
            directory.mkdir(parents=True, exist_ok=True)
            if path.exists() or path.is_symlink():
                checked_unit(path, expected)
            else:
                with path.open('x') as stream:
                    os.chmod(path, 0o600)
                    stream.write(expected)
            ctl('daemon-reload')
        print('Installed manual user unit:', name)
        return
    checked_unit(path, expected)
    loaded = ctl('show', name, '--property=FragmentPath', '--value', capture=True).stdout.strip()
    if loaded != str(path):
        raise ValueError('Loaded unit is not the checked private unit')
    overrides = ctl('show', name, '--property=DropInPaths', '--value', capture=True).stdout.strip()
    if overrides:
        raise ValueError('Unit overrides refused')
    if action == 'status':
        # Service state is not a claim that MQTT or the application is ready.
        ctl('show', name, '--property=ActiveState,SubState,Result,MainPID')
        print('Service status only. Open the loopback URL printed by the launcher; verify login and live state.')
    else:
        ctl(action, name)
        print(action, 'requested for', name, '; no database deletion or automatic command replay')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('install', 'start', 'status', 'stop', 'restart'))
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--web-root', type=Path, required=True)
    args = parser.parse_args()
    if os.name != 'posix' or os.getuid() == 0:
        parser.error('Use an ordinary Linux/WSL user with existing Docker and user systemd access')
    try:
        manage(args.action, args.state, args.web_root, Path(sys.executable))
    except Exception as error:
        # Do not echo profile values, credentials, child output or SQL.
        print('MANAGER_FAILURE ' + type(error).__name__ + ': preserve profile and storage; inspect prerequisites. No WSL/Docker restart attempted.', file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
