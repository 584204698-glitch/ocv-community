"""Register the plugin directory only; never patch OCV or install models."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ID = 'ocv_community'

def linked(path):
    try:
        attrs = os.lstat(path)
    except FileNotFoundError:
        return False
    return path.is_symlink() or bool(getattr(attrs, 'st_file_attributes', 0) & 0x400)

def install(ocv_root, *, apply=False, source=None):
    raw = Path(ocv_root).expanduser().absolute()
    if any(linked(p) for p in (raw, *raw.parents)):
        raise ValueError('OCV root must not use symbolic links or junctions')
    host = raw.resolve(strict=True)
    if not host.is_dir() or not (host / 'backend/app/main.py').is_file():
        raise ValueError('Choose the official OCV root containing backend/app/main.py')
    source = Path(source or ROOT / 'plugins' / PLUGIN_ID).resolve(strict=True)
    meta = json.loads((source / 'plugin.json').read_text(encoding='utf-8'))
    if meta.get('id') != PLUGIN_ID or meta.get('type') != 'placeholder' or meta.get('entry'):
        raise ValueError('This installer only registers the non-executable integration preview')
    manifest_path = source / 'package-files.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    files = {}
    for rel, expected in manifest.items():
        relative = Path(rel)
        if relative.is_absolute() or '..' in relative.parts or '\\' in rel or ':' in rel:
            raise ValueError('Invalid plugin package path')
        path = source / relative
        if path.is_symlink() or not path.resolve().is_relative_to(source):
            raise ValueError('Plugin package contains a linked or escaped path')
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError('Plugin package checksum mismatch: ' + rel)
        files[rel] = data
    files['package-files.json'] = manifest_path.read_bytes()
    actual = {p.relative_to(source).as_posix() for p in source.rglob('*') if p.is_file()
              and not any(part in {'__pycache__', 'runtime', 'models'} for part in p.relative_to(source).parts)
              and p.suffix != '.pyc' and p.name != 'config.private.json'}
    if actual != set(files):
        raise ValueError('Plugin package contains unlisted files; rebuild its manifest')
    plugins = host / 'plugins'
    if linked(plugins) or not plugins.resolve().is_relative_to(host):
        raise ValueError('Host plugins directory must not escape OCV root')
    target = plugins / PLUGIN_ID
    if target.exists() or target.is_symlink():
        raise FileExistsError('Plugin already exists; preserve its contents before updating')
    report = {'plugin': PLUGIN_ID, 'installed': False, 'files': len(files),
              'functional': False, 'status': 'requires_host_api',
              'message': 'Only metadata registration is supported by stock OCV.'}
    if not apply:
        return report
    plugins.mkdir(parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix='.ocv-community-install-', dir=plugins)).resolve()
    try:
        for rel, data in files.items():
            path = staged / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        if target.exists() or target.is_symlink():
            raise FileExistsError('Plugin appeared during installation; nothing overwritten')
        staged.rename(target)
        report['installed'] = True
    finally:
        if staged.exists():
            assert staged.parent == plugins.resolve() and staged.name.startswith('.ocv-community-install-')
            shutil.rmtree(staged)
    return report

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ocv-root', type=Path, required=True)
    parser.add_argument('--install', action='store_true')
    args = parser.parse_args()
    try:
        print(json.dumps(install(args.ocv_root, apply=args.install), ensure_ascii=False, indent=2))
    except (OSError, ValueError) as exc:
        parser.exit(2, str(exc) + '\n')

if __name__ == '__main__':
    main()
