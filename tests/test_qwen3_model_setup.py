import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.prepare_qwen3_models import MODELS, prepare, validate_rows
from backend.app import qwen3_local


class OptionalModelSetupTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.rows = [{'model': model, 'file': 'config.json', 'size': 2,
                      'sha256': hashlib.sha256(b'{}').hexdigest(), 'revision': 'a'*40}
                     for model in sorted(MODELS)]

    def populate(self):
        for row in self.rows:
            file = self.root / 'models' / row['model'] / row['file']
            file.parent.mkdir(parents=True)
            file.write_bytes(b'{}')

    def test_verify_existing_models_never_uses_network(self):
        self.populate()
        with patch('urllib.request.urlopen', side_effect=AssertionError('Unexpected network')):
            prepare(self.root, self.rows, report=lambda _: None)
        self.assertEqual(json.loads((self.root/'download-verified.json').read_text()), self.rows)

    def test_missing_models_do_not_download_or_create_marker_by_default(self):
        with patch('urllib.request.urlopen', side_effect=AssertionError('Unexpected network')):
            with self.assertRaises(FileNotFoundError):
                prepare(self.root, self.rows)
        self.assertFalse((self.root/'download-verified.json').exists())

    def test_changed_checkpoint_is_not_overwritten_or_certified(self):
        self.populate()
        file = self.root/'models'/self.rows[0]['model']/'config.json'
        file.write_bytes(b'[]')
        with self.assertRaises(ValueError):
            prepare(self.root, self.rows, download=True)
        self.assertEqual(file.read_bytes(),b'[]')
        self.assertFalse((self.root/'download-verified.json').exists())

    def test_invalid_manifest_path_rejected_before_filesystem_changes(self):
        for bad in ['../escape', '/absolute', 'C:/absolute', 'dir\\escape', 'a/../b']:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_rows([{**self.rows[0], 'file':bad}, self.rows[1]])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_interrupted_download_is_not_silently_replaced(self):
        file = self.root/'models'/self.rows[0]['model']/'config.json.partial'
        file.parent.mkdir(parents=True)
        file.write_bytes(b'partial')
        with patch('urllib.request.urlopen', side_effect=AssertionError('Unexpected network')):
            with self.assertRaises(FileExistsError):
                prepare(self.root, self.rows, download=True)
        self.assertEqual(file.read_bytes(),b'partial')

    def test_windows_and_linux_default_interpreter_paths_and_explicit_override(self):
        with patch.object(qwen3_local, 'load_project_env'), patch.dict('os.environ', {'QWEN3_TTS_ROOT':str(self.root)}, clear=True):
            for platform, relative in [('win32','venv/Scripts/python.exe'), ('linux','venv/bin/python')]:
                with patch.object(qwen3_local.sys, 'platform', platform):
                    self.assertEqual(qwen3_local.configuration()['python'],self.root/relative)
            with patch.dict('os.environ', {'QWEN3_TTS_PYTHON':str(self.root/'custom-python')}):
                self.assertEqual(qwen3_local.configuration()['python'],self.root/'custom-python')


if __name__ == '__main__':
    unittest.main()
