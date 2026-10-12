"""Verify source extraction and patches against the pinned official Git source.

This uses only temporary copies and CPU syntax checks. It does not modify the
official checkout, import model code, install dependencies or run media jobs.
"""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any

from install_plugin import install

ROOT = Path(__file__).resolve().parents[1]
sha = lambda content: hashlib.sha256(content).hexdigest()

def verify(official):
    official = Path(official).resolve(strict=True)
    index = json.loads((ROOT / 'integration/file-map.json').read_text(encoding='utf8'))
    base = index['official_commit']
    def blob(name):
        return subprocess.check_output(['git','show',base+':'+name],cwd=official).replace(b'\r\n',b'\n')
    additions = [item for item in index['files'] if item['kind']=='addon']
    changes = [item for item in index['files'] if item['kind']=='host_patch']
    for item in additions + index.get('assets',[]):
        content=(ROOT/item['package']).read_bytes()
        if item in additions:content=content.replace(b'\r\n',b'\n')
        assert sha(content)==item['sha256'], item['source']
    compiled = 0
    for path in (ROOT/'plugins/ocv_community').rglob('*.py'):
        compile(path.read_bytes(),str(path.relative_to(ROOT)),'exec')
        compiled += 1
    with tempfile.TemporaryDirectory(prefix='ocv-plugin-source-check-') as folder:
        work=Path(folder)
        for item in changes:
            content=blob(item['source'])
            assert sha(content)==item['base_sha256'],item['source']
            path=work/item['source'];path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(content)
        patch=ROOT/'integration/official-2026.10.12.1.patch'
        subprocess.run(['git','apply','--check',str(patch)],cwd=work,check=True,capture_output=True)
        subprocess.run(['git','apply',str(patch)],cwd=work,check=True,capture_output=True)
        for item in changes:
            assert sha((work/item['source']).read_bytes().replace(b'\r\n',b'\n'))==item['after_sha256'],item['source']
        # Exercise the official metadata reader exactly as shipped, without
        # importing its entire application or starting its background workers.
        listing=next(node for node in ast.parse(blob('backend/app/main.py')).body
                     if isinstance(node,ast.FunctionDef) and node.name=='_plugin_manifest_items')
        ast.fix_missing_locations(listing)
        namespace={'PLUGINS_DIR':work/'plugins','Path':Path,'json':json,'re':re,'Any':Any}
        exec(compile(ast.Module(body=[listing],type_ignores=[]),'official-plugin-reader','exec'),namespace)
        install(work,apply=True)
        meta=next(item for item in namespace['_plugin_manifest_items']() if item['id']=='ocv_community')
        assert meta['valid'] and meta['enabled'] and meta['framework_only']
        (work/'plugins/ocv_community/disabled').write_text('disabled for test\n')
        meta=next(item for item in namespace['_plugin_manifest_items']() if item['id']=='ocv_community')
        assert not meta['enabled'] and meta['framework_only']
    return {'official_commit':base,'addon_sources_verified':len(additions),'host_patches_verified':len(changes),
            'python_files_compiled':compiled,'official_metadata_and_disable_verified':True,
            'functional_integration_verified':False,'model_calls':0}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--official-root',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(verify(args.official_root),ensure_ascii=False,indent=2))

if __name__=='__main__':
    main()
