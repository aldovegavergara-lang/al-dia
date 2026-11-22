"""Manage Al día as a per-user macOS LaunchAgent, without administrator access."""
from pathlib import Path
import os
import plistlib
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
LABEL = os.environ.get('NEWSNEST_SERVICE_LABEL', 'local.aldia.lector')
DOMAIN = f'gui/{os.getuid()}'
SERVICE = f'{DOMAIN}/{LABEL}'
PLIST = Path.home() / 'Library' / 'LaunchAgents' / f'{LABEL}.plist'
LOGS = Path.home() / 'Library' / 'Logs' / 'AlDia'


def command(*args, check=True):
    return subprocess.run(['launchctl', *args], check=check)


def install():
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    for name in ('service.log', 'error.log'):
        fd = os.open(LOGS / name, os.O_WRONLY | os.O_CREAT, 0o600)
        os.close(fd)
    settings = {
        'Label': LABEL,
        'ProgramArguments': [str(ROOT / '.venv' / 'bin' / 'python'), str(ROOT / 'run-reels.py')],
        'WorkingDirectory': str(ROOT),
        'RunAtLoad': True,
        'KeepAlive': True,
        'ThrottleInterval': 10,
        'ProcessType': 'Background',
        'EnvironmentVariables': {'PYTHONUNBUFFERED': '1', 'PYTHONDONTWRITEBYTECODE': '1'},
        'StandardOutPath': str(LOGS / 'service.log'),
        'StandardErrorPath': str(LOGS / 'error.log'),
    }
    with PLIST.open('wb') as f:
        plistlib.dump(settings, f)
    PLIST.chmod(0o600)
    loaded = subprocess.run(['launchctl', 'print', SERVICE], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL).returncode == 0
    if loaded:
        command('bootout', SERVICE)
    command('enable', SERVICE)
    command('bootstrap', DOMAIN, str(PLIST))
    print(f'Servicio instalado: {PLIST}')
    print('Lector: http://127.0.0.1:3097/reels')


if __name__ == '__main__':
    action = sys.argv[1] if len(sys.argv) > 1 else 'status'
    if action == 'install':
        install()
    elif action == 'status':
        command('print', SERVICE)
    elif action == 'restart':
        command('kickstart', '-k', SERVICE)
    elif action == 'stop':
        command('bootout', SERVICE)
    else:
        raise SystemExit('Uso: .venv/bin/python scripts/servicio.py [install|status|restart|stop]')
