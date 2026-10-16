import ast
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'tools' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


scope = load('check_release_scope')
privacy = load('check_release_privacy')


class ReleaseScopeTests(unittest.TestCase):
    def test_host_patches_and_services_cannot_be_published_as_addons(self):
        for name in ('integration/host_changes/main.py.patch', 'integration/old.patch',
                     'plugins/ocv_community/maintenance_src/helper.py', 'backend/app/main.py',
                     'plugins/ocv_community/extension_src/backend/app/openai_image_transport.py',
                     'plugins/ocv_community/extension_src/frontend/src/components/VideoStudio.vue'):
            with self.subTest(name=name):
                self.assertTrue(scope.findings(name))
        for name in ('README.md', 'integration/AUTHOR_GUIDE.md',
                     'plugins/ocv_community/extension_src/backend/app/qwen3_local.py'):
            self.assertEqual(scope.findings(name), [])

    def test_source_inventory_contains_only_enhancements(self):
        index = json.loads((ROOT / 'integration/file-map.json').read_bytes())
        self.assertTrue(index['files'])
        self.assertTrue(all(item['kind'] == 'addon' for item in index['files']))
        for item in index['files']:
            self.assertEqual(scope.findings(item['package']), [])

    def test_privacy_reports_rules_without_echoing_credentials(self):
        key = b'sk-' + b'A' * 32
        result = privacy.findings('module.py', b'example=' + key)
        self.assertEqual(result, [{'file': 'module.py', 'rule': 'provider_key'}])
        self.assertNotIn(key.decode(), str(result))
        self.assertTrue(privacy.findings('workspace/project.json', b'{}'))
        self.assertFalse(privacy.findings('README.md', b'Configure your own API key in the host.'))

    def test_identity_repair_preserves_host_capacity_instead_of_reducing_to_four(self):
        # Exercise actual input assembly; no imports of the host or model code.
        source = ROOT / 'plugins/ocv_community/extension_src/backend/app/video_native_image_repair.py'
        function = next(node for node in ast.parse(source.read_bytes()).body
                        if isinstance(node, ast.FunctionDef) and node.name == '_repair_inputs')
        package = types.ModuleType('addon_host_probe')
        package.__path__ = []
        studio = types.ModuleType('addon_host_probe.video_studio')
        cast_module = types.ModuleType('addon_host_probe.video_cast_planning')
        package.video_studio = studio
        namespace = {'__package__': 'addon_host_probe', 'REPAIR_REVISION': 'framing-aware-opening-edit-v4',
                     'Path': Path, 'json': json, '_digest': lambda p: p.name,
                     '_design_digest': lambda shot: 'design', 'time': types.SimpleNamespace(time=lambda: 0),
                     'copy': __import__('copy')}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), 'exec'), namespace)
        with tempfile.TemporaryDirectory() as folder, patch.dict(sys.modules, {
                'addon_host_probe': package, 'addon_host_probe.video_studio': studio,
                'addon_host_probe.video_cast_planning': cast_module}):
            root = Path(folder)
            (root / 'current.png').write_bytes(b'current')
            studio._storyboard_image_path = lambda path, shot: path / 'current.png'
            for count in (1, 3, 7, 11):
                with self.subTest(identity_references=count):
                    characters = [{'project_character_id': str(i), 'reference_id': str(i),
                                   'name': 'actor ' + str(i)} for i in range(count)]
                    refs = [{'id': str(i), 'file': str(i) + '.png'} for i in range(count)]
                    for ref in refs:
                        (root / ref['file']).write_bytes(b'identity')
                    cast_module.saved_planning_cast = lambda record: {'characters': characters}
                    studio.planning_references = lambda record: refs
                    shot = {'id': 's', 'cast_plan': {'characters': [
                        {'project_character_id': str(i), 'presence': 'opening'} for i in range(count)]}}
                    result = namespace['_repair_inputs'](root, {'shots': [shot]}, shot,
                        [{'code': 'cast', 'correction': 'restore identity'}], {'s': 'current.png'})
                    self.assertEqual([Path(p).name for p in result[2]],
                                     ['current.png'] + [r['file'] for r in refs])
                    self.assertEqual(len(result[3]['references']), count + 1)


if __name__ == '__main__':
    unittest.main()
