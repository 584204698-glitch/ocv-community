"""Verify add-on sources and metadata without modifying official OCV.

No patch application, dependency installation or model calls. An optional
official source directory checks that add-on paths do not replace host files
and exercises the host's metadata reader without importing the application.
"""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import tempfile
from typing import Any

from install_plugin import install

ROOT = Path(__file__).resolve().parents[1]


def canonical(path):
    content = path.read_bytes()
    return content if path.suffix in {'.jpg', '.webp', '.png'} else content.replace(b'\r\n', b'\n')


def verify(official=None):
    index = json.loads((ROOT / 'integration/file-map.json').read_text(encoding='utf8'))
    additions = index['files']
    assert additions and all(item['kind'] == 'addon' for item in additions), 'Only add-on sources may be shipped'
    for item in additions + index.get('assets', []):
        path = (ROOT / item['package']).resolve(strict=True)
        path.relative_to(ROOT)
        assert hashlib.sha256(canonical(path)).hexdigest() == item['sha256'], item['source']
    source_root = ROOT / 'plugins/ocv_community/extension_src'
    actual = {p.relative_to(source_root).as_posix() for p in source_root.rglob('*')
              if p.is_file() and '__pycache__' not in p.parts and p.suffix in {'.py', '.js', '.vue'}}
    assert actual == {item['source'] for item in additions}, 'Add-on inventory differs from source tree'
    compiled = 0
    for path in (ROOT / 'plugins/ocv_community').rglob('*.py'):
        compile(canonical(path), str(path.relative_to(ROOT)), 'exec')
        compiled += 1
    metadata_checked = False
    official_checked = False
    with tempfile.TemporaryDirectory(prefix='ocv-plugin-source-check-') as folder:
        work = Path(folder)
        (work / 'backend/app').mkdir(parents=True)
        marker = work / 'backend/app/main.py'
        marker.write_text('# unchanged official host\n', encoding='utf8')
        before = marker.read_bytes()
        install(work, apply=True)
        assert marker.read_bytes() == before
        if official is not None:
            official = Path(official).resolve(strict=True)
            overlaps = [item['source'] for item in additions if (official / item['source']).exists()]
            assert not overlaps, 'Add-on replaces official files: ' + ', '.join(overlaps)
            main = (official / 'backend/app/main.py').read_bytes()
            listing = next(node for node in ast.parse(main).body
                           if isinstance(node, ast.FunctionDef) and node.name == '_plugin_manifest_items')
            namespace = {'PLUGINS_DIR': work / 'plugins', 'Path': Path, 'json': json, 're': re, 'Any': Any}
            exec(compile(ast.Module(body=[listing], type_ignores=[]), 'official-plugin-reader', 'exec'), namespace)
            meta = next(item for item in namespace['_plugin_manifest_items']() if item['id'] == 'ocv_community')
            assert meta['valid'] and meta['enabled'] and meta['framework_only']
            (work / 'plugins/ocv_community/disabled').write_text('disabled for test\n', encoding='utf8')
            meta = next(item for item in namespace['_plugin_manifest_items']() if item['id'] == 'ocv_community')
            assert not meta['enabled'] and meta['framework_only']
            metadata_checked = True
            official_checked = True
    return {'addon_sources_verified': len(additions), 'assets_verified': len(index.get('assets', [])),
            'host_patches_shipped': 0, 'python_files_compiled': compiled,
            'official_source_overlap_checked': official_checked,
            'official_metadata_and_disable_verified': metadata_checked,
            'functional_integration_verified': False, 'model_calls': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--official-root', type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.official_root), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
