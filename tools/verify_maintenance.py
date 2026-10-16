"""Verify the optional pinned maintenance patch in a temporary directory."""
import argparse
import ast
import hashlib
import json
import runpy
import re
from pathlib import Path
import subprocess
import tempfile

ROOT=Path(__file__).resolve().parents[1]
sha=lambda data:hashlib.sha256(data).hexdigest()

def verify(official):
    official=Path(official).resolve(strict=True)
    folder=ROOT/'integration/maintenance-2026.10.14.1'
    index=json.loads((folder/'file-map.json').read_text(encoding='utf8'))
    with tempfile.TemporaryDirectory(prefix='ocv-maintenance-check-') as tmp:
        work=Path(tmp)
        for row in index['files']:
            if row['before_sha256'] is None:continue
            file=official/row['path']
            if file.is_file():data=file.read_bytes().replace(b'\r\n',b'\n')
            else:raise ValueError('Missing official source: '+row['path'])
            if sha(data)!=row['before_sha256']:
                raise ValueError('Source differs from pinned official version: '+row['path'])
            target=work/row['path'];target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
        patch=folder/'redraw-four-inputs.patch'
        subprocess.run(['git','-c','core.autocrlf=false','apply','--check',str(patch)],cwd=work,check=True,capture_output=True)
        subprocess.run(['git','-c','core.autocrlf=false','apply',str(patch)],cwd=work,check=True,capture_output=True)
        for row in index['files']:
            data=(work/row['path']).read_bytes().replace(b'\r\n',b'\n')
            assert sha(data)==row['after_sha256'],row['path']
            if row['path'].endswith('.py'):ast.parse(data)
            elif row['path'].endswith('.js'):
                subprocess.run(['node','--input-type=module','--check'],input=data,check=True,capture_output=True)
            elif row['path'].endswith('.vue'):
                script=data.split(b'<script setup>',1)[1].split(b'</script>',1)[0]
                subprocess.run(['node','--input-type=module','--check'],input=script,check=True,capture_output=True)
        tree=ast.parse((work/'backend/app/video_studio.py').read_bytes())
        schema=next(node for node in tree.body if isinstance(node,ast.ClassDef) and node.name=='StoryboardRedraw')
        from pydantic import BaseModel,Field
        image_size=runpy.run_path(str(official/'backend/app/cloud_image_channel.py'))['ImageSize']
        namespace={'BaseModel':BaseModel,'Field':Field,'ImageSize':image_size}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[schema],type_ignores=[])),'schema','exec'),namespace)
        request=namespace['StoryboardRedraw'](revision=1,prompt='An existing frame',reference_ids=['a','b','c','d'])
        assert len(request.reference_ids)==4
        component=(work/'frontend/src/components/VideoStudio.vue').read_text(encoding='utf8')
        functions='\n'.join(re.search(pattern,component,re.S).group(0) for pattern in (
            r'function redrawUsage\([^\n]+\n.*?\n}',
            r'function toggleRedrawReference\(id\)\{\n.*?\n}',
            r'function toggleCurrentReference\(\)\{[^\n]+}'))
        helper=(work/'frontend/src/imageReferenceCapacity.js').read_text(encoding='utf8').replace('export function','function')
        behavior="""
const redrawReferenceIds={value:['a','b','c']},useCurrentReference={value:false},
 useSceneReference={value:false},selectedShot={value:{}},redrawReferenceGallery={value:[]},error={value:''}
const sceneAssetFor=()=>({id:'room'})
toggleRedrawReference('d');if(redrawReferenceIds.value.length!==4)throw Error('Fourth selection lost')
toggleCurrentReference();if(useCurrentReference.value||redrawReferenceIds.value.length!==4)throw Error('Selections silently changed')
useSceneReference.value=true;useCurrentReference.value=true
toggleRedrawReference('d');if(redrawReferenceIds.value.includes('d'))throw Error('Cannot reduce an overfull selection')
toggleCurrentReference();if(useCurrentReference.value)throw Error('Cannot remove the current frame')
toggleRedrawReference('d');if(redrawReferenceIds.value.includes('d'))throw Error('Automatic scene not counted')
"""
        subprocess.run(['node','--input-type=module','-'],input=(helper+'\n'+functions+'\n'+behavior).encode(),check=True,capture_output=True)
    return dict(official_commit=index['official_commit'],host_patch_files=len(index['files']),
                schema_four_inputs_verified=True,selection_behavior_verified=True,source_untouched=True,provider_calls=0,
                plugin_auto_execution=False)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--official-root',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(verify(args.official_root),indent=2))
