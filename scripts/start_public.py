"""Start the source distribution with login and a persistent per-install secret."""
import argparse
import hashlib
import os
from pathlib import Path
import secrets
import sys

ROOT = Path(__file__).resolve().parents[1]
REVOKED_SESSION_FINGERPRINTS = {'458660ce3f8387c264a21200ebe59ec0df1e32e5b9f7f0a0837039396a567c16'}

def revoked_secret(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest() in REVOKED_SESSION_FINGERPRINTS

def prepare_config(root=ROOT):
    target = root / '.env'
    if not target.exists():
        text = (root / '.env.example').read_text(encoding='utf-8')
        with target.open('x', encoding='utf-8') as stream:
            stream.write(text)
        target.chmod(0o600)
    sys.path.insert(0, str(root))
    from backend.app.config import load_project_env
    load_project_env()
    configured_secret = os.environ.get('SESSION_SECRET', '').strip()
    if not configured_secret or revoked_secret(configured_secret):
        path = root / '.session-secret'
        if path.is_symlink():
            raise RuntimeError('Session secret file must not be a symbolic link')
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                stream.write(secrets.token_hex(32))
        secret = path.read_text(encoding='utf-8').strip()
        if revoked_secret(secret):
            temporary = path.with_name('.session-secret.new-' + secrets.token_hex(8))
            try:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                secret = secrets.token_hex(32)
                with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                    stream.write(secret)
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        if len(secret) < 32:
            raise RuntimeError('Invalid session secret; inspect .session-secret without publishing its contents')
        os.environ['SESSION_SECRET'] = secret
    if os.environ.get('AUTH_MODE', 'account') != 'account' or os.environ.get('DISABLE_AUTH', '').lower() in {'1','true','yes','on'}:
        raise RuntimeError('Public launcher requires AUTH_MODE=account and DISABLE_AUTH=0')

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8010)
    args = parser.parse_args()
    os.chdir(ROOT)
    if not (ROOT / 'frontend/dist/index.html').is_file():
        raise SystemExit('Build the frontend first: npm --prefix frontend ci && npm --prefix frontend run build')
    prepare_config()
    from backend.app.main import app
    from fastapi.staticfiles import StaticFiles
    import uvicorn
    app.mount('/', StaticFiles(directory=ROOT / 'frontend/dist', html=True), name='public-ui')
    uvicorn.run(app, host=args.host, port=args.port)

if __name__ == '__main__':
    main()
