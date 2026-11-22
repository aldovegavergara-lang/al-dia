"""Run the local reader. The generated password stays in ignored runtime data."""
import os
from pathlib import Path
import secrets
import shutil

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
support = Path.home() / 'Library' / 'Application Support' / 'AlDia'
config = Path(os.environ.get('NEWSNEST_CONFIG_DIR', str(support / 'config')))
data = Path(os.environ.get('NEWSNEST_DATA_DIR', str(support / 'data')))
config.parent.mkdir(parents=True, exist_ok=True)
data.mkdir(parents=True, exist_ok=True)
if not config.exists():
    shutil.copytree(ROOT / 'reels-config', config)
password_file = data / '.local-password'
if not password_file.exists():
    fd = os.open(password_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(secrets.token_urlsafe(18))
os.environ.setdefault('NEWSNEST_PASSWORD', password_file.read_text().strip())
os.environ.setdefault('NEWSNEST_CONFIG_DIR', str(config))
os.environ.setdefault('NEWSNEST_DATA_DIR', str(data))
os.environ.setdefault('NEWSNEST_AI_ENABLED', 'off')
from newsnest.bootstrap import ensure_dirs
from newsnest.__main__ import setup_logging
import uvicorn

if __name__ == '__main__':
    ensure_dirs()
    setup_logging()
    print('Al día: http://127.0.0.1:3097/reels', flush=True)
    print(f'Contraseña local en {password_file} (archivo privado).', flush=True)
    uvicorn.run('newsnest.web:app', host='127.0.0.1', port=3097, workers=1, log_config=None)
