"""Reject host patches and official replacements from the public Git index."""
import json
from pathlib import PurePosixPath, Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = {'.gitattributes', '.gitignore', 'README.md', 'LICENSE', 'NOTICE', 'AUTHORS.md',
              'ADDITIONAL_TERMS.md', 'TRADEMARKS.md', 'THIRD_PARTY_NOTICES.md'}
INTEGRATION_FILES = {'file-map.json', 'plugin_api.example.json'}
HOST_FILES = {'backend/app/main.py', 'backend/app/video_studio.py', 'backend/app/image_profiles.py',
              'backend/app/image_studio.py', 'backend/app/gemini_client.py', 'backend/app/codex_bridge.py',
              'backend/app/director_extensions.py', 'frontend/src/Studio.vue',
              'frontend/src/components/VideoStudio.vue', 'frontend/src/useStudio.js',
              'backend/app/openai_image_transport.py', 'backend/app/video_image_outputs.py'}


def findings(name):
    path = PurePosixPath(name)
    if path.suffix == '.patch' or any(part.startswith('maintenance') or part == 'host_changes' for part in path.parts):
        return [{'file': name, 'rule': 'host_maintenance_not_plugin'}]
    if name in ROOT_FILES:
        return []
    if path.parts[0] in {'tools', 'tests'}:
        return []
    if path.parts[0] == 'integration' and len(path.parts) == 2 and path.name in INTEGRATION_FILES:
        return []
    prefix = 'plugins/ocv_community/'
    if name.startswith(prefix):
        relative = name[len(prefix):]
        if relative.startswith('extension_src/') and relative[len('extension_src/'):] in HOST_FILES:
            return [{'file': name, 'rule': 'official_service_replacement'}]
        return []
    return [{'file': name, 'rule': 'outside_addon_release_scope'}]


def check():
    names = subprocess.check_output(['git', 'ls-files', '--cached', '-z'], cwd=ROOT).split(b'\0')
    issues = [issue for raw in names if raw for issue in findings(raw.decode('utf8'))]
    data = subprocess.check_output(['git', 'show', ':integration/file-map.json'], cwd=ROOT)
    index = json.loads(data)
    if any(item.get('kind') != 'addon' for item in index['files']):
        issues.append({'file': 'integration/file-map.json', 'rule': 'non_addon_inventory'})
    return {'passed': not issues, 'issues': issues, 'indexed_files': sum(bool(n) for n in names)}


if __name__ == '__main__':
    result = check()
    print(json.dumps(result, indent=2))
    raise SystemExit(not result['passed'])
