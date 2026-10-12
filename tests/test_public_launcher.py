import os
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from scripts.start_public import prepare_config


class PublicLauncherTest(unittest.TestCase):
    def test_previous_published_secret_is_rotated_once(self):
        old = 'test-revoked-session-secret-value'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.env.example').write_text('AUTH_MODE=account\nDISABLE_AUTH=0\n', encoding='utf8')
            (root / '.session-secret').write_text(old, encoding='utf8')
            with patch.dict(os.environ, {'AUTH_MODE':'account','DISABLE_AUTH':'0','SESSION_SECRET':old}, clear=True), patch('backend.app.config.load_project_env'), patch('scripts.start_public.REVOKED_SESSION_FINGERPRINTS', {hashlib.sha256(old.encode()).hexdigest()}):
                prepare_config(root)
                first = os.environ.pop('SESSION_SECRET')
                self.assertNotEqual(first, old)
                prepare_config(root)
                self.assertEqual(os.environ['SESSION_SECRET'], first)
                self.assertEqual((root / '.session-secret').read_text(), first)

    def test_new_install_secret_is_stable_and_login_required(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.env.example').write_text('AUTH_MODE=account\nDISABLE_AUTH=0\n', encoding='utf8')
            with patch.dict(os.environ, {'AUTH_MODE': 'account', 'DISABLE_AUTH': '0'}, clear=True), patch('backend.app.config.load_project_env'):
                prepare_config(root)
                first = os.environ.pop('SESSION_SECRET')
                prepare_config(root)
                self.assertEqual(first, os.environ['SESSION_SECRET'])
                self.assertGreaterEqual(len(first), 32)
                self.assertEqual((root / '.env').read_text(), 'AUTH_MODE=account\nDISABLE_AUTH=0\n')

    def test_public_launcher_rejects_disabled_login(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.env.example').write_text('AUTH_MODE=account\n', encoding='utf8')
            with patch.dict(os.environ, {'AUTH_MODE': 'local', 'SESSION_SECRET': 'x' * 64}, clear=True), patch('backend.app.config.load_project_env'):
                with self.assertRaisesRegex(RuntimeError, 'requires AUTH_MODE=account'):
                    prepare_config(root)
