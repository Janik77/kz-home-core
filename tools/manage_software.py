"""Manage ONE existing software-only profile with an opt-in systemd user unit.

Does not initialize, upgrade, rebind, delete, enable at boot or alter Docker/WSL.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
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
    return subprocess.run(['/usr/bin/systemctl', '--user', *args], check=True,
                          text=True, capture_output=capture)


def safe_directory(directory):
    for parent in (directory, *directory.parents):
        if parent.is_symlink():
            raise ValueError('Symlink unit directory refused')
        if not parent.exists():
            continue
        info = parent.stat()
        sticky_root = info.st_uid == 0 and info.st_mode & stat.S_ISVTX
        if info.st_uid not in (0, os.getuid()) or (info.st_mode & 0o022 and not sticky_root):
            raise ValueError('Unsafe unit directory ownership or permissions')


def private_json(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError('Missing private configuration')
    stat = path.stat()
    if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise ValueError('Unsafe private configuration permissions')
    return json.loads(path.read_text())


def binding(state):
    state = state.expanduser().absolute()
    if any(p.is_symlink() for p in (state, *state.parents)):
        raise ValueError('Symlink profile refused')
    data = private_json(state / 'profile.json')
    identity = f'{state.stat().st_dev}:{state.stat().st_ino}'
    if data.get('profile_path') != str(state) or data.get('directory_identity') != identity:
        raise ValueError('Profile binding mismatch')
    return {'state': str(state), 'identity': identity, 'owner': data['owner']}


def runtime_preflight(state, web, python):
    profile = Profile(state)
    if not (profile.path / 'initialized').is_file():
        raise ValueError('Incomplete profile')
    validate_core()
    sys.path.insert(0, str(CORE / 'tools'))
    from run_software_stack import validate_web
    web = validate_web(web)
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError('Python executable unavailable')
    return profile, web


def manage(action, state, web, python, *, unit_dir=None):
    state = state.expanduser().absolute()
    directory = unit_dir or Path.home() / '.config/systemd/user'
    safe_directory(directory)
    name = unit_name(state)
    path = directory / name
    manifest = directory / (name + '.json')
    identity = binding(state)
    ctl('show-environment', capture=True)
    if action == 'install':
        if web is None:
            raise ValueError('Web root required for install')
        python = python.absolute()
        profile, web = runtime_preflight(state, web, python)
        bins = [str(python.parent)]
        for executable_name in ('node', 'npm', 'docker'):
            executable = shutil.which(executable_name)
            if executable is None:
                raise ValueError('Required executable unavailable')
            bins.append(str(Path(executable).parent))
        bins.extend(('/usr/local/bin', '/usr/bin', '/bin'))
        config = dict(identity, core=str(CORE), web=str(web), python=str(python),
                      executable_path=':'.join(dict.fromkeys(bins)), version=1)
        expected = unit_text(python, CORE, web, state, config['executable_path'])
        with profile.lock():
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            safe_directory(directory)
            if path.exists() or path.is_symlink() or manifest.exists() or manifest.is_symlink():
                checked_unit(path, expected)
                if private_json(manifest) != config:
                    raise ValueError('Existing installation differs; no overwrite')
            else:
                for destination, content in ((path, expected), (manifest, json.dumps(config))):
                    with destination.open('x') as stream:
                        os.chmod(destination, 0o600)
                        stream.write(content)
            ctl('daemon-reload')
        print('Installed manual user unit:', name)
        return
    config = private_json(manifest)
    required = {'state', 'identity', 'owner', 'core', 'web', 'python', 'executable_path', 'version'}
    if set(config) != required or any(not isinstance(config[key], str) for key in required - {'version'}):
        raise ValueError('Invalid installation configuration')
    for key in ('state', 'core', 'web', 'python'):
        if not Path(config[key]).is_absolute():
            raise ValueError('Absolute installation paths required')
    if any(not Path(part).is_absolute() for part in config['executable_path'].split(':')):
        raise ValueError('Unsafe saved executable search path')
    if config.get('version') != 1 or any(config.get(k) != v for k, v in identity.items()):
        raise ValueError('Installation binding mismatch')
    expected = unit_text(Path(config['python']), Path(config['core']),
                         Path(config['web']), state, config['executable_path'])
    checked_unit(path, expected)
    loaded = ctl('show', name, '--property=FragmentPath', '--value', capture=True).stdout.strip()
    if loaded != str(path):
        raise ValueError('Loaded unit is not the checked private unit')
    overrides = ctl('show', name, '--property=DropInPaths', '--value', capture=True).stdout.strip()
    if overrides:
        raise ValueError('Unit overrides refused')
    reload_needed = ctl('show', name, '--property=NeedDaemonReload', '--value', capture=True).stdout.strip()
    if reload_needed != 'no':
        raise ValueError('Loaded unit configuration is stale or unverifiable')
    if action in ('start', 'restart'):
        if str(CORE) != config['core'] or (web is not None and str(web.resolve()) != config['web']):
            raise ValueError('Use the installed checkout')
        runtime_preflight(state, Path(config['web']), Path(config['python']))
    if action == 'status':
        ctl('show', name, '--property=ActiveState,SubState,Result,MainPID')
        print('Service status only; verify login and live state separately.')
    else:
        ctl(action, name)
        print(action, 'requested for', name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('install', 'start', 'status', 'stop', 'restart'))
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--web-root', type=Path)
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
