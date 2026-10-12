#!/usr/bin/env python3
"""Verify optional Qwen3-TTS checkpoints, or explicitly download pinned files.

No GPU inference, environment installation or OCV configuration changes occur.
Model files are fetched only from the official Qwen namespace on ModelScope.
"""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import urllib.parse
import urllib.request

MODELS = {'Qwen3-TTS-12Hz-1.7B-VoiceDesign', 'Qwen3-TTS-12Hz-1.7B-Base'}
MANIFEST = Path(__file__).with_name('qwen3-model-files.json')


def digest(file):
    value = hashlib.sha256()
    with file.open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024**2), b''):
            value.update(block)
    return value.hexdigest()


def validate_rows(rows):
    seen = set()
    for row in rows:
        relative = PurePosixPath(row['file'])
        if (row['model'] not in MODELS or relative.is_absolute() or '..' in relative.parts
                or '\\' in row['file'] or ':' in row['file'] or not relative.parts
                or relative.as_posix() != row['file']
                or type(row['size']) is not int or row['size'] < 1
                or not re.fullmatch('[a-f0-9]{64}', row['sha256'])
                or not re.fullmatch('[a-f0-9]{40}', row['revision'])):
            raise ValueError('Invalid checkpoint manifest entry')
        identity = (row['model'], row['file'])
        if identity in seen:
            raise ValueError('Duplicate checkpoint manifest entry')
        seen.add(identity)
    if {row['model'] for row in rows} != MODELS:
        raise ValueError('Both official voice models must be present')


def prepare(root, rows, *, download=False, report=print):
    validate_rows(rows)
    root = Path(root).expanduser().resolve()
    for row in rows:
        target = root / 'models' / row['model'] / row['file']
        if root not in target.resolve().parents or target.is_symlink():
            raise ValueError('Checkpoint path escapes the selected model root')
        if target.exists():
            if target.stat().st_size != row['size'] or digest(target) != row['sha256']:
                raise ValueError(f"Existing checkpoint does not match: {row['model']}/{row['file']}; it was not overwritten")
        else:
            if not download:
                raise FileNotFoundError(f"Missing: {row['model']}/{row['file']}; use --download to fetch it")
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_name(target.name + '.partial')
            if partial.exists():
                raise FileExistsError(f'Partial download already exists; inspect it before retrying: {partial}')
            url = ('https://modelscope.cn/models/Qwen/' + row['model'] + '/resolve/'
                   + row['revision'] + '/' + urllib.parse.quote(row['file'], safe='/'))
            count = 0
            with urllib.request.urlopen(url, timeout=60) as response, partial.open('xb') as output:
                for block in iter(lambda: response.read(4 * 1024**2), b''):
                    count += len(block)
                    if count > row['size']:
                        raise ValueError('Downloaded checkpoint exceeds its expected size')
                    output.write(block)
            if count != row['size'] or digest(partial) != row['sha256']:
                raise ValueError('Download checksum mismatch; partial retained, checkpoint not adopted')
            if target.exists():
                raise FileExistsError('Checkpoint appeared during download; refusing to overwrite')
            partial.rename(target)
        report(f"Verified: {row['model']}/{row['file']}")
    marker = root / 'download-verified.json'
    if marker.is_symlink():
        raise ValueError('Verification marker must not be a symbolic link')
    temporary = root / 'download-verified.json.partial'
    with temporary.open('x', encoding='utf-8') as stream:
        json.dump(rows, stream, indent=2)
    temporary.replace(marker)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True, help='QWEN3_TTS_ROOT directory, containing models/')
    parser.add_argument('--download', action='store_true', help='Explicitly fetch missing model files (about 9.1 GB total)')
    args = parser.parse_args()
    rows = json.loads(MANIFEST.read_text(encoding='utf-8'))
    prepare(args.root, rows, download=args.download)
    print('Checkpoint verification complete. Configure a separate Qwen3-TTS Python environment before synthesis.')


if __name__ == '__main__':
    main()
