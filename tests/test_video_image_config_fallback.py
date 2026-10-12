"""Missing selection must recover a sole profile or return an actionable 400."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import module4_video_render as visual
from backend.app import image_profiles as profiles, video_studio as studio


class ImageConfigFallbackTests(unittest.TestCase):
    def setUp(self):
        self.record = {'settings': {'ratio': '16:9'}, 'creation_parameters': {'image_profile_id': '', 'image_resolution': '1k'}}
        self.snapshot = {'id': 'native', 'resolution': '1k', 'protocol': 'openai_images'}
        self.configs = [{'api_key': 'synthetic', 'model': 'image-test', 'protocol': 'openai_images'}]
        for target, name, values in [
            (visual, '_provider_configs', {'side_effect': RuntimeError('missing key')}),
            (profiles, 'list_profiles', {'return_value': [{'id': 'native', 'configured': True}]}),
            (profiles, 'profile_snapshot', {'return_value': self.snapshot}),
            (profiles, 'profile_provider_configs', {'return_value': self.configs}),
        ]:
            handle = patch.object(target, name, **values)
            setattr(self, name, handle.start())
            self.addCleanup(handle.stop)

    def test_recovers_only_configured_profile_and_saves_nonsecret_snapshot(self):
        result = studio._image_configs(self.record, 1)
        self.assertEqual(result[0]['protocol'], 'openai_images')
        self.assertEqual(result[0]['ratio'], '16:9')
        self.assertEqual(self.record['creation_parameters']['image_profile_id'], 'native')
        self.assertNotIn('api_key', self.record['creation_parameters']['image_profile_snapshot'])

    def test_explicit_profile_id_without_snapshot_is_used(self):
        self.record['creation_parameters']['image_profile_id'] = 'chosen'
        studio._image_configs(self.record, 1)
        self.profile_snapshot.assert_called_once_with('chosen', '1k')
        self._provider_configs.assert_not_called()

    def test_invalid_explicit_profile_never_falls_back(self):
        self.record['creation_parameters']['image_profile_id'] = 'deleted'
        self.profile_snapshot.side_effect = ValueError('missing selected profile')
        with self.assertRaises(ValueError):
            studio._image_configs(self.record, 1)
        self.list_profiles.assert_not_called()

    def test_existing_snapshot_never_falls_back(self):
        self.record['creation_parameters']['image_profile_snapshot'] = self.snapshot
        self.profile_provider_configs.side_effect = ValueError('endpoint changed')
        with self.assertRaises(ValueError):
            studio._image_configs(self.record, 1)
        self.list_profiles.assert_not_called()

    def test_legacy_config_is_preserved(self):
        self._provider_configs.side_effect = None
        self._provider_configs.return_value = [{'protocol': 'async_task'}]
        self.assertEqual(studio._image_configs(self.record)[0]['protocol'], 'async_task')
        self.list_profiles.assert_not_called()

    def test_missing_or_ambiguous_profiles_return_400_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / '1' / ('a' * 32)
            path.mkdir(parents=True)
            record = {**copy.deepcopy(self.record), 'id': 'a' * 32, 'revision': 2,
                      'status': 'generation_ready', 'shots': [], 'logs': []}
            studio.save(path, record)
            before = (path / 'record.json').read_bytes()
            app = FastAPI()
            app.include_router(studio.router)
            with patch.object(studio, 'ROOT', root), patch.object(studio, 'require_user', return_value={'id': 1}), \
                 patch.object(studio, 'verify_project_cast'), patch.object(studio, '_start_storyboard_images') as start, TestClient(app) as client:
                for available in [[], [{'id': 'a', 'configured': True}, {'id': 'b', 'configured': True}]]:
                    self.list_profiles.return_value = available
                    response = client.post('/api/video-studio/' + record['id'] + '/images/generate', json={'revision': 2})
                    self.assertEqual(response.status_code, 400, response.text)
                    self.assertIn('图像接口', response.json()['detail'])
                    self.assertEqual((path / 'record.json').read_bytes(), before)
                start.assert_not_called()


if __name__ == '__main__':
    unittest.main()
