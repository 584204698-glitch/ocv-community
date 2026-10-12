"""Install optional Qwen in its own venv. No changes to OCV/IndexTTS environments."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import venv

try:
    from .prepare_qwen3_models import MANIFEST, prepare
except ImportError:
    from prepare_qwen3_models import MANIFEST, prepare

DEFAULT_ROOT = Path(__file__).resolve().parents[1] / 'runtime' / 'qwen3-tts'

def interpreter(root):
    return root / 'venv' / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')

def install(root, *, cuda='cu130', download=False):
    root = Path(root).expanduser().resolve()
    if sys.version_info[:2] not in {(3, 10), (3, 11), (3, 12)}:
        raise RuntimeError('Run with Python 3.10, 3.11 or 3.12 (validated with 3.11).')
    if cuda not in {'cu128', 'cu130'}:
        raise ValueError('Choose cu128 or cu130 to match the NVIDIA driver.')
    if root.is_symlink() or (root / 'venv').is_symlink():
        raise ValueError('Choose a dedicated, non-symlink runtime directory.')
    python = interpreter(root)
    marker = root / 'venv' / 'pyvenv.cfg'
    if (root / 'venv').exists() and not marker.is_file():
        raise RuntimeError('Existing venv directory is not a Python venv; refusing to modify it.')
    if python.resolve() == Path(sys.executable).resolve():
        raise RuntimeError('Run this helper with the OCV/system Python, not the target Qwen Python.')
    if not marker.exists():
        venv.EnvBuilder(with_pip=True).create(root / 'venv')
    if not python.is_file():
        raise RuntimeError('The selected environment belongs to another OS; use a new directory.')
    def pip(*args):
        subprocess.run([str(python), '-m', 'pip', *args], check=True)
    pip('install', '--upgrade', 'pip')
    pip('install', 'torch==2.10.0', 'torchaudio==2.10.0',
        '--index-url', 'https://download.pytorch.org/whl/' + cuda)
    pip('install', 'qwen-tts==0.1.1', 'soundfile==0.13.1')
    pip('check')
    subprocess.run([str(python), '-c', 'import qwen_tts,torch,torchaudio,soundfile; print("Qwen dependencies imported")'], check=True)
    if download:
        prepare(root, json.loads(MANIFEST.read_text(encoding='utf-8')), download=True)
    print('Installation finished. Add these values to the OCV backend .env, then restart:')
    print('QWEN3_TTS_ROOT=' + root.as_posix())
    print('QWEN3_TTS_PYTHON=' + python.as_posix())
    print('No GPU inference has run. Generate one audition to verify the driver and voice output.')
    if not download:
        print('Weights not downloaded. Run prepare_qwen3_models.py --root <same root> --download next.')

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=DEFAULT_ROOT)
    parser.add_argument('--cuda', choices=['cu128', 'cu130'], default='cu130')
    parser.add_argument('--install', action='store_true', help='Explicitly create/update the dedicated Qwen venv')
    parser.add_argument('--download', action='store_true', help='Also fetch and verify approximately 9.1 GB of weights')
    args = parser.parse_args()
    if not args.install:
        if args.download:
            parser.error('--download requires --install; for weights only, use prepare_qwen3_models.py')
        print('No files changed. Add --install to install dependencies; add --download to fetch models.')
        print('Target: ' + str(args.root.resolve()))
        return
    install(args.root, cuda=args.cuda, download=args.download)

if __name__ == '__main__':
    main()
