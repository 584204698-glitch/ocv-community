import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app import voice_design as voices, qwen3_local


class VoiceDesignApiTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.owner = 1
        self.work = []
        for target, value in (
            ('ROOT', self.root), ('require_user', lambda request: {'id': self.owner}),
        ):
            replacement = patch.object(voices, target, value)
            replacement.start()
            self.addCleanup(replacement.stop)
        replacement = patch.object(voices, 'threading', SimpleNamespace(Thread=lambda **kw: SimpleNamespace(start=lambda: self.work.append(kw))))
        replacement.start()
        self.addCleanup(replacement.stop)
        replacement = patch.object(qwen3_local, 'health', return_value={'ready': True, 'missing': []})
        self.health = replacement.start()
        self.addCleanup(replacement.stop)
        app = FastAPI()
        app.include_router(voices.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.payload = {'request_id': 'a' * 32, 'name': 'Test voice',
                        'description': 'Warm adult female voice', 'reference_text': 'Please speak this reference sentence naturally.'}

    def create(self):
        response = self.client.post('/api/voice-design', json=self.payload)
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def ready_voice(self):
        row = self.create()
        path = voices.folder(self.owner, row['id'])
        (path / 'design').mkdir()
        audio = path / 'design/chunk-0001.wav'
        audio.write_bytes(b'local-test-audio')
        record = voices.read(path)
        record.update(status='ready', audio={'sha256': hashlib.sha256(audio.read_bytes()).hexdigest()})
        voices.save(path, record)
        return row, path

    def test_replayed_submission_is_one_generation_and_changed_payload_conflicts(self):
        first = self.create()
        self.assertEqual(self.create()['id'], first['id'])
        self.assertEqual(len(self.work), 1)
        changed = {**self.payload, 'name': 'Different name'}
        self.assertEqual(self.client.post('/api/voice-design', json=changed).status_code, 409)
        self.assertEqual(len(self.work), 1)

    def test_accounts_cannot_read_adopt_or_download_each_others_voices(self):
        row, path = self.ready_voice()
        self.owner = 2
        for method, suffix in [('get', ''), ('get', '/audio'), ('post', '/adopt')]:
            self.assertEqual(getattr(self.client, method)('/api/voice-design/' + row['id'] + suffix).status_code, 404)
        self.assertEqual(self.client.get('/api/voice-design').json()['voices'], [])
        with self.assertRaises(Exception):
            qwen3_local.resolve_saved_voice('qwen3:' + row['id'], 2)

    def test_only_adopted_immutable_reference_can_be_reused(self):
        row, path = self.ready_voice()
        voice_id = 'qwen3:' + row['id']
        with self.assertRaises(ValueError):
            qwen3_local.resolve_saved_voice(voice_id, 1)
        response = self.client.post('/api/voice-design/' + row['id'] + '/adopt')
        self.assertEqual(response.status_code, 200)
        reference, text = qwen3_local.resolve_saved_voice(voice_id, 1)
        self.assertEqual(text, self.payload['reference_text'])
        reference.write_bytes(b'changed')
        with self.assertRaises(ValueError):
            qwen3_local.resolve_saved_voice(voice_id, 1)

    def test_cannot_adopt_pending_voice_and_another_submission_does_not_run(self):
        row = self.create()
        self.assertEqual(self.client.post('/api/voice-design/' + row['id'] + '/adopt').status_code, 409)
        self.assertEqual(self.client.get('/api/voice-design/' + row['id'] + '/audio').status_code, 404)
        second = {**self.payload, 'request_id': 'b' * 32}
        self.assertEqual(self.client.post('/api/voice-design', json=second).status_code, 409)
        self.assertEqual(len(self.work), 1)

    def test_health_and_invalid_fields_fail_before_persisting_or_launching(self):
        self.health.return_value = {'ready': False, 'missing': ['model']}
        self.assertEqual(self.client.post('/api/voice-design', json=self.payload).status_code, 503)
        self.assertFalse(list(self.root.glob('*/**/record.json')))
        invalid = {**self.payload, 'request_id': '../x'}
        self.assertEqual(self.client.post('/api/voice-design', json=invalid).status_code, 422)
        self.assertEqual(self.work, [])

    def test_public_records_do_not_expose_private_request_state(self):
        self.create()
        row = self.client.get('/api/voice-design').json()['voices'][0]
        self.assertNotIn('_request', row)
        self.assertNotIn('audio_url', row)

    def test_dead_supervisor_fails_without_relaunching_generation(self):
        row = self.create()
        path = voices.folder(1, row['id'])
        record = voices.read(path)
        record['_supervisor'] = {'pid': 123, 'start_ticks': 'original'}
        voices.save(path, record)
        with patch.object(Path, 'is_dir', return_value=True), patch.object(voices, 'process_identity', return_value='reused'):
            result = self.client.get('/api/voice-design/' + row['id']).json()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(len(self.work), 1)
        self.assertEqual(self.create()['status'], 'failed')

    def test_live_supervisor_survives_http_process_restart(self):
        row = self.create()
        path = voices.folder(1, row['id'])
        record = voices.read(path)
        record.update(_supervisor={'pid': 123, 'start_ticks': 'original'}, created_at=0)
        voices.save(path, record)
        with patch.object(Path, 'is_dir', return_value=True), patch.object(voices, 'process_identity', return_value='original'):
            result = self.client.get('/api/voice-design/' + row['id']).json()
        self.assertEqual(result['status'], 'generating')
        self.assertEqual(len(self.work), 1)

    def test_library_archive_is_reversible_and_keeps_bound_reference_bytes(self):
        row, path = self.ready_voice()
        self.client.post('/api/voice-design/' + row['id'] + '/adopt')
        original = (path / 'design/chunk-0001.wav').read_bytes()
        url = '/api/voice-design/' + row['id'] + '/library'
        result = self.client.patch(url, json={'favorite': True, 'archived': True})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['status'], 'saved')
        self.assertEqual(self.client.get('/api/voice-design/' + row['id'] + '/audio').content, original)
        reference, _ = qwen3_local.resolve_saved_voice('qwen3:' + row['id'], 1)
        self.assertEqual(reference.read_bytes(), original)
        restored = self.client.patch(url, json={'archived': False}).json()
        self.assertTrue(restored['favorite'])
        self.assertFalse(restored['archived'])
        self.owner = 2
        self.assertEqual(self.client.patch(url, json={'favorite': False}).status_code, 404)

    def test_generating_metadata_is_not_overwritten_by_library_actions(self):
        row = self.create()
        self.assertEqual(self.client.patch('/api/voice-design/' + row['id'] + '/library', json={'favorite': True}).status_code, 409)

    def test_design_source_is_validated_and_saved_without_binding(self):
        source = dict(project_id='b'*32, character_id='pc_'+'c'*32, project_title='故事', character_name='角色')
        self.payload.update(project_id=source['project_id'], character_id=source['character_id'])
        with patch('backend.app.voice_design_agent.project_context', return_value=({'source': source}, {})) as context:
            row = self.create()
        context.assert_called_once_with(1, source['project_id'], source['character_id'])
        self.assertEqual(row['source'], source)
        self.assertEqual(row['status'], 'generating')

    def test_foreign_character_rejected_before_voice_generation(self):
        self.payload.update(project_id='b'*32, character_id='pc_'+'c'*32)
        with patch('backend.app.voice_design_agent.project_context', side_effect=ValueError('角色不属于当前项目')):
            response = self.client.post('/api/voice-design', json=self.payload)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.work, [])


if __name__ == '__main__':
    unittest.main()
