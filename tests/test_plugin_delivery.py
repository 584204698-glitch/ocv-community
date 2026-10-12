import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'tools' / (name + '.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

installer = load('install_plugin')
checker = load('check_host')

class PluginDeliveryTest(unittest.TestCase):
    def test_windows_git_text_line_endings_are_accepted_and_canonicalized(self):
        import shutil
        with tempfile.TemporaryDirectory() as folder:
            host=self.host(folder)
            source=Path(folder)/'package'
            shutil.copytree(ROOT/'plugins/ocv_community',source,ignore=shutil.ignore_patterns('__pycache__'))
            text=source/'extension_src/frontend/src/voiceLibrary.js'
            original=text.read_bytes().replace(b'\r\n',b'\n')
            text.write_bytes(original.replace(b'\n',b'\r\n'))
            installer.install(host,apply=True,source=source)
            result=host/'plugins/ocv_community/extension_src/frontend/src/voiceLibrary.js'
            self.assertEqual(result.read_bytes(),original)

    def host(self, folder):
        root = Path(folder) / 'OCV'
        entry = root / 'backend/app/main.py'
        entry.parent.mkdir(parents=True)
        entry.write_text('# unchanged host\n', encoding='utf8')
        (root / '.env').write_text('private test config\n', encoding='utf8')
        (root / 'workspace').mkdir()
        (root / 'workspace/project.json').write_text('{"revision":4}', encoding='utf8')
        return root

    def test_preview_and_install_keep_host_and_user_data(self):
        with tempfile.TemporaryDirectory() as folder:
            host = self.host(folder)
            before = {p.relative_to(host).as_posix():p.read_bytes() for p in host.rglob('*') if p.is_file()}
            preview = installer.install(host)
            self.assertFalse(preview['installed'])
            self.assertFalse((host / 'plugins').exists())
            installed = installer.install(host, apply=True)
            self.assertTrue(installed['installed'])
            self.assertFalse(installed['functional'])
            self.assertEqual(before, {name:(host/name).read_bytes() for name in before})
            plugin = host / 'plugins/ocv_community'
            meta = json.loads((plugin / 'plugin.json').read_text(encoding='utf8'))
            self.assertEqual(meta['type'],'placeholder')
            self.assertEqual(meta['entry'],'')
            self.assertEqual(installed['files'],len([p for p in plugin.rglob('*') if p.is_file()]))
            user = plugin / 'config.private.json'
            user.write_text('user preferences', encoding='utf8')
            with self.assertRaises(FileExistsError):installer.install(host,apply=True)
            self.assertEqual(user.read_text(),'user preferences')

    def test_runtime_and_python_cache_are_not_bundled(self):
        import shutil
        with tempfile.TemporaryDirectory() as folder:
            host=self.host(folder)
            source=Path(folder)/'package'
            shutil.copytree(ROOT/'plugins/ocv_community',source,ignore=shutil.ignore_patterns('__pycache__'))
            runtime=source/'runtime';runtime.mkdir();(runtime/'private-model.bin').write_bytes(b'local-only')
            cache=source/'__pycache__';cache.mkdir();(cache/'local.pyc').write_bytes(b'cache')
            (source/'config.private.json').write_text('private')
            installer.install(host,apply=True,source=source)
            installed=host/'plugins/ocv_community'
            self.assertFalse((installed/'runtime').exists())
            self.assertFalse((installed/'__pycache__').exists())
            self.assertFalse((installed/'config.private.json').exists())

    def test_checksum_failure_has_no_host_side_effect(self):
        with tempfile.TemporaryDirectory() as folder:
            host=self.host(folder)
            source=Path(folder)/'source';source.mkdir()
            (source/'plugin.json').write_text(json.dumps({'id':'ocv_community','type':'placeholder','entry':''}))
            (source/'package-files.json').write_text(json.dumps({'plugin.json':'0'*64}))
            with self.assertRaisesRegex(ValueError,'checksum'):installer.install(host,apply=True,source=source)
            self.assertFalse((host/'plugins').exists())

    def test_traversal_in_manifest_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            host=self.host(folder)
            source=Path(folder)/'source';source.mkdir()
            (source/'plugin.json').write_text(json.dumps({'id':'ocv_community','type':'placeholder','entry':''}))
            (source/'package-files.json').write_text(json.dumps({'../outside':'0'*64}))
            with self.assertRaisesRegex(ValueError,'package path'):installer.install(host,apply=True,source=source)
            self.assertFalse((host/'plugins').exists())

    def test_missing_or_declared_interfaces_never_imply_working_plugin(self):
        with tempfile.TemporaryDirectory() as folder:
            host=self.host(folder)
            initial=checker.check(host)
            self.assertEqual(initial['status'],'requires_host_api')
            self.assertEqual(set(initial['missing_interfaces']),set(checker.CAPABILITIES))
            declaration=host/'backend/app/plugin_api.json'
            declaration.write_text(json.dumps({'api_version':1,'capabilities':{k:True for k in checker.CAPABILITIES}}))
            result=checker.check(host)
            self.assertFalse(result['runtime_verified'])
            self.assertEqual(result['status'],'declared_only_not_runtime_verified')
            declaration.write_text(json.dumps({'api_version':1,'capabilities':{'tts_provider':'true'}}))
            with self.assertRaises(ValueError):checker.check(host)

if __name__ == '__main__':
    unittest.main()
