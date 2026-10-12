"""Exercise sync persistence and submission boundaries with local provider doubles."""
import copy
import hashlib
import json
import unittest
from contextlib import nullcontext
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from backend.app import video_generation as generation
from backend.app import video_inputs as inputs
from backend.app import video_prompt_inputs as prompt_inputs
from backend.app import video_prompt_refresh as prompt_refresh
from backend.app import video_studio as studio
from backend.app.video_motion_plan import render_motion_action
from backend.app.video_agents import VideoAgentJSONError
from tests import test_video_inputs as input_fixture


class VideoPromptSyncApiTest(unittest.TestCase):
    def setUp(self):
        input_fixture.VideoInputTest.setUp(self)
        self.url = self.base + '/shots/shot-A'
        self.record['scenes'] = [{'slide_id': 's1', 'text': 'A waits. B enters.', 'start': 0, 'end': 4}]
        self.record['settings']['style'] = 'original style'
        shot = self.record['shots'][0]
        shot.update(slide_ids=['s1'], start=0, end=4, intent='A waits before B enters.',
            action='A waits, B enters. Old input availability note.', image_prompt='  Original image prompt.\n',
            reference_ids=['character'], video_prompt_inputs_stale=True,
            motion_plan={'version': 2, 'scene_anchor': 'The study', 'participants': ['A', 'B'],
                'beats': [{'action': 'A waits.', 'texts': []},
                          {'action': 'B enters. Old input availability note.', 'texts': []}],
                'reference_beat': 1, 'reference_visual': 'A waits at the desk.',
                'reference_participants': ['A'], 'reference_texts': []},
            video_input_selection={'input_mode': 'composition_reference', 'profile_id': 'composition_reference',
                                   'reference_ids': ['character', 'tail'], 'end_frame_reference_id': ''})
        studio.save(self.path, self.record)
        scope = patch.object(studio, 'project_language_scope', side_effect=lambda *args: nullcontext())
        scope.start()
        self.addCleanup(scope.stop)
        app = FastAPI()
        for router in (studio.router, inputs.router, generation.router):
            app.include_router(router)
        self.client = TestClient(app)

    def read(self):
        return studio.read(self.path)

    def sync(self, **kwargs):
        return self.client.post(self.url + '/refresh-prompts', json={
            'revision': self.read()['revision'], 'basis': 'video_inputs', **kwargs})

    def view(self):
        response = self.client.get(self.url + '/video-inputs')
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    @staticmethod
    def refreshed(context, style, shot, references, **kwargs):
        updated = copy.deepcopy(shot)
        updated['motion_plan']['beats'][1]['action'] = 'B enters after A has waited.'
        updated['action'] = render_motion_action(updated['motion_plan'])
        updated['video_prompt'] = 'Use picture 1 as composition; A uses picture 2; B uses picture 3. A waits, then B enters.'
        updated['video_prompt_warnings'] = []
        return updated, None

    def test_sync_uses_server_manifest_and_stored_prose_without_touching_images_or_other_shots(self):
        before = self.read()
        original_bytes = (self.path / 'core.png').read_bytes()
        with patch.object(prompt_refresh, 'refresh', side_effect=self.refreshed) as refresh, \
                patch.object(studio, '_normalize_rgb_image') as normalize:
            response = self.sync(action='client must not replace stored action', image_prompt='client must not redraw',
                                 video_inputs={'images': [{'path': 'C:/outside.png'}]})
        self.assertEqual(response.status_code, 200, response.text)
        refresh.assert_called_once()
        normalize.assert_not_called()
        args, kwargs = refresh.call_args
        self.assertEqual(kwargs['basis'], 'video_inputs')
        self.assertEqual(kwargs['action'], before['shots'][0]['action'])
        self.assertEqual(kwargs['image_prompt'], before['shots'][0]['image_prompt'])
        actual = kwargs['video_inputs']
        self.assertEqual(actual['input_mode'], 'composition_reference')
        self.assertEqual([row['id'] for row in actual['images']], ['shot-A', 'character', 'tail'])
        self.assertEqual([row['label'] for row in actual['images']], ['<Picture 1>', '<Picture 2>', '<Picture 3>'])
        self.assertEqual([row['role'] for row in actual['images']], ['composition', 'identity_reference', 'identity_reference'])
        self.assertEqual(actual['images'][0]['sha256'], hashlib.sha256(original_bytes).hexdigest())
        self.assertTrue(all('path' not in row for row in actual['images']))
        self.assertFalse(kwargs.get('force_vision'))
        after = response.json()
        shot = after['shots'][0]
        for key in ('image', 'image_prompt', 'image_status', 'reference_ids', 'slide_ids', 'start', 'end', 'duration',
                    'generation_duration', 'intent', 'video_input_selection'):
            self.assertEqual(shot[key], before['shots'][0][key], key)
        self.assertEqual(after['shots'][1], before['shots'][1])
        self.assertEqual(after['scenes'], before['scenes'])
        self.assertEqual((self.path / 'core.png').read_bytes(), original_bytes)
        self.assertEqual(shot['action'], render_motion_action(shot['motion_plan']))
        self.assertNotIn('Old input availability note.', shot['action'])
        self.assertNotIn('video_prompt_inputs_stale', shot)
        self.assertEqual(shot['video_prompt_input_basis']['source'], 'synchronized')
        self.assertEqual(self.view()['prompt_sync']['status'], 'current')
        self.assertEqual(after['revision'], before['revision'] + 1)

    def test_refresh_failure_keeps_entire_original_and_new_selection_stale(self):
        before = self.read()
        with patch.object(prompt_refresh, 'refresh', side_effect=RuntimeError('synthetic model failure')):
            response = self.sync()
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.read(), before)
        self.assertTrue(self.read()['shots'][0]['video_prompt_inputs_stale'])
        self.assertEqual(self.view()['prompt_sync']['status'], 'stale')
        self.assertFalse(studio.project_has_image_edits(self.identity))

    def test_revision_race_does_not_overwrite_the_concurrent_record(self):
        concurrent = None
        def race(*args, **kwargs):
            nonlocal concurrent
            concurrent = self.read()
            concurrent['revision'] += 1
            concurrent['logs'].append('An independent concurrent edit.')
            studio.save(self.path, concurrent)
            concurrent = self.read()
            return self.refreshed(*args, **kwargs)
        with patch.object(prompt_refresh, 'refresh', side_effect=race):
            response = self.sync()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.read(), concurrent)
        self.assertNotIn('video_prompt_input_basis', self.read()['shots'][0])

    def test_failed_model_json_has_private_evidence_without_changing_the_record(self):
        before = self.read()
        raw = '{"shots":[]} {"shots":[]}'
        failure = VideoAgentJSONError('repaired_content', json.JSONDecodeError('extra', raw, 13))
        failure.model_outputs = {'original': raw, 'repaired': raw}
        with patch.object(prompt_refresh, 'refresh', side_effect=failure):
            response = self.sync()
        self.assertEqual(response.status_code, 400)
        self.assertNotIn(raw, response.text)
        self.assertEqual(self.read(), before)
        self.assertFalse(studio.project_has_image_edits(self.identity))
        files = list((self.path / 'diagnostics' / 'prompt-refresh').glob('*.json'))
        self.assertEqual(len(files), 1)
        saved = json.loads(files[0].read_text(encoding='utf-8'))
        self.assertEqual(saved['model_outputs'], failure.model_outputs)
        self.assertEqual(saved['shot_id'], 'shot-A')
        self.assertEqual(saved['revision'], before['revision'])

    def test_diagnostic_write_failure_does_not_hide_error_or_leave_edit_locked(self):
        before = self.read()
        (self.path / 'diagnostics').write_text('file blocks directory creation')
        failure = VideoAgentJSONError('repaired_content', json.JSONDecodeError('extra', '{}{}', 2))
        failure.model_outputs = {'original': '{}{}', 'repaired': '{}{}'}
        with patch.object(prompt_refresh, 'refresh', side_effect=failure):
            response = self.sync()
        self.assertEqual(response.status_code, 400)
        self.assertIn('repaired_content', response.json()['detail'])
        self.assertEqual(self.read(), before)
        self.assertFalse(studio.project_has_image_edits(self.identity))

    def test_provider_envelopes_are_never_persisted_as_model_diagnostics(self):
        before = self.read()
        failure = VideoAgentJSONError('provider_response', json.JSONDecodeError('private', 'private-envelope', 0))
        with patch.object(prompt_refresh, 'refresh', side_effect=failure):
            response = self.sync()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.read(), before)
        self.assertFalse((self.path / 'diagnostics').exists())

    def test_image_byte_race_without_revision_change_rejects_stale_model_result(self):
        before = self.read()
        def replace_reference(*args, **kwargs):
            Image.new('RGB', (16, 16), 'purple').save(self.path / 'character.png')
            return self.refreshed(*args, **kwargs)
        with patch.object(prompt_refresh, 'refresh', side_effect=replace_reference):
            response = self.sync()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.read(), before)
        self.assertNotIn('video_prompt_input_basis', self.read()['shots'][0])

    def test_real_old_coverage_is_preserved_but_becomes_stale_after_text_sync(self):
        confirm = self.client.put(self.url + '/character-coverage', json={
            'revision': self.read()['revision'],
            'characters': [{'name': 'A', 'presence': 'opening'},
                           {'name': 'B', 'presence': 'later', 'reference_id': 'tail'}]})
        self.assertEqual(confirm.status_code, 200, confirm.text)
        before = self.read()
        self.assertEqual(self.view()['coverage']['status'], 'covered')
        with patch.object(prompt_refresh, 'refresh', side_effect=self.refreshed):
            response = self.sync()
        self.assertEqual(response.status_code, 200, response.text)
        after = self.read()
        self.assertEqual(after['shots'][0]['video_character_review'], before['shots'][0]['video_character_review'])
        self.assertEqual(self.view()['prompt_sync']['status'], 'current')
        self.assertEqual(self.view()['coverage']['status'], 'stale')

    def test_sync_does_not_create_coverage_when_no_pixels_have_been_reviewed(self):
        with patch.object(prompt_refresh, 'refresh', side_effect=self.refreshed):
            response = self.sync()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn('video_character_review', response.json()['shots'][0])
        self.assertEqual(self.view()['coverage']['status'], 'unknown')

    def test_sync_allowed_local_completed_stage_preserves_old_clip_in_history(self):
        record = self.read()
        record['status'] = 'completed'
        record['shots'][0].update(video='old.mp4', video_status='completed', video_version='old-version')
        studio.save(self.path, record)
        with patch.object(prompt_refresh, 'refresh', side_effect=self.refreshed):
            response = self.sync()
        self.assertEqual(response.status_code, 200, response.text)
        after = response.json()
        self.assertEqual(after['status'], 'video_review')
        self.assertEqual(after['shots'][0]['video_history'][-1]['video'], 'old.mp4')
        self.assertNotIn('video', after['shots'][0])
        self.assertEqual(after['shots'][0]['image'], 'core.png')

    def test_sync_invalid_stage_backend_and_stale_revision_stop_before_provider(self):
        before = self.read()
        for setting, value, expected in (('status', 'storyboard_review', 409),
                                         ('backend', 'cloud', 400), ('revision', 0, 409)):
            current = copy.deepcopy(before)
            if setting == 'status':
                current['status'] = value
            elif setting == 'backend':
                current['creation_parameters']['video_generation_backend'] = value
            studio.save(self.path, current)
            snapshot = self.read()
            with self.subTest(setting=setting), patch.object(prompt_refresh, 'refresh') as refresh:
                response = self.sync(**({'revision': value} if setting == 'revision' else {}))
                self.assertEqual(response.status_code, expected, response.text)
                refresh.assert_not_called()
            self.assertEqual(self.read(), snapshot)

    def test_manual_confirmation_records_only_prose_basis_without_generation_or_coverage(self):
        before = self.read()
        with patch.object(prompt_refresh, 'refresh') as refresh, patch.object(generation, '_start_local_worker') as worker:
            response = self.client.post(self.url + '/video-inputs/confirm-prompts', json={'revision': before['revision']})
        self.assertEqual(response.status_code, 200, response.text)
        refresh.assert_not_called()
        worker.assert_not_called()
        shot = self.read()['shots'][0]
        for key in ('action', 'motion_plan', 'video_prompt', 'image_prompt', 'video_input_selection', 'reference_ids'):
            self.assertEqual(shot[key], before['shots'][0][key])
        self.assertEqual(shot['video_prompt_input_basis']['source'], 'manual_review')
        self.assertNotIn('video_character_review', shot)
        self.assertNotIn('video_prompt_inputs_stale', shot)
        self.assertEqual(self.view()['prompt_sync']['status'], 'current')

    def test_manual_confirmation_rejects_revision_conflict_and_missing_images(self):
        before = self.read()
        conflict = self.client.post(self.url + '/video-inputs/confirm-prompts', json={'revision': 0})
        self.assertEqual(conflict.status_code, 409, conflict.text)
        self.assertEqual(self.read(), before)
        (self.path / 'character.png').unlink()
        missing = self.client.post(self.url + '/video-inputs/confirm-prompts', json={'revision': before['revision']})
        self.assertEqual(missing.status_code, 400, missing.text)
        self.assertEqual(self.read(), before)

    def test_current_basis_is_idempotent_for_both_manual_confirmation_and_sync(self):
        first = self.client.post(self.url + '/video-inputs/confirm-prompts', json={'revision': self.read()['revision']})
        self.assertEqual(first.status_code, 200, first.text)
        before = self.read()
        with patch.object(prompt_refresh, 'refresh') as refresh:
            again = self.client.post(self.url + '/video-inputs/confirm-prompts', json={'revision': before['revision']})
            self.assertEqual(again.status_code, 200, again.text)
            synced = self.sync()
            self.assertEqual(synced.status_code, 200, synced.text)
        refresh.assert_not_called()
        self.assertEqual(self.read(), before)

    def test_manual_ack_of_changed_input_bytes_invalidates_old_clip_even_when_text_is_identical(self):
        record = self.read()
        record['status'] = 'completed'
        shot = record['shots'][0]
        prompt_inputs.adopt_prompt_basis(shot, prompt_inputs.prompt_input_basis(1, self.path, record, shot))
        shot.update(video='old.mp4', video_status='completed', video_version='old-version')
        studio.save(self.path, record)
        Image.new('RGB', (16, 16), 'purple').save(self.path / 'character.png')
        self.assertEqual(self.view()['prompt_sync']['status'], 'stale')
        response = self.client.post(self.url + '/video-inputs/confirm-prompts', json={'revision': self.read()['revision']})
        self.assertEqual(response.status_code, 200, response.text)
        after = self.read()
        self.assertEqual(after['status'], 'video_review')
        self.assertNotIn('video', after['shots'][0])
        self.assertEqual(after['shots'][0]['video_history'][-1]['video'], 'old.mp4')
        self.assertEqual(after['shots'][0]['video_prompt'], shot['video_prompt'])
        self.assertEqual(self.view()['prompt_sync']['status'], 'current')

    def test_auto_sync_of_changed_input_bytes_invalidates_old_clip_even_when_text_is_identical(self):
        record = self.read()
        record['status'] = 'completed'
        shot = record['shots'][0]
        prompt_inputs.adopt_prompt_basis(shot, prompt_inputs.prompt_input_basis(1, self.path, record, shot))
        shot.update(video='old.mp4', video_status='completed', video_version='old-version')
        studio.save(self.path, record)
        Image.new('RGB', (16, 16), 'purple').save(self.path / 'character.png')
        self.assertEqual(self.view()['prompt_sync']['status'], 'stale')
        with patch.object(prompt_refresh, 'refresh', side_effect=lambda *args, **kwargs: (copy.deepcopy(args[2]), None)):
            response = self.sync()
        self.assertEqual(response.status_code, 200, response.text)
        after = self.read()
        self.assertEqual(after['status'], 'video_review')
        self.assertNotIn('video', after['shots'][0])
        self.assertEqual(after['shots'][0]['video_history'][-1]['video'], 'old.mp4')
        self.assertEqual(after['shots'][0]['video_prompt'], shot['video_prompt'])
        self.assertEqual(self.view()['prompt_sync']['status'], 'current')

    def test_partial_character_coverage_can_sync_without_manufacturing_a_binding(self):
        confirm = self.client.put(self.url + '/character-coverage', json={
            'revision': self.read()['revision'], 'characters': [{'name': 'B', 'presence': 'later'}]})
        self.assertEqual(confirm.status_code, 200, confirm.text)
        before = self.read()['shots'][0]['video_character_review']
        self.assertEqual(self.view()['coverage']['status'], 'partial')
        with patch.object(prompt_refresh, 'refresh', side_effect=self.refreshed):
            response = self.sync()
        self.assertEqual(response.status_code, 200, response.text)
        after = self.read()['shots'][0]['video_character_review']
        self.assertEqual(after, before)
        self.assertFalse(after['characters'][0]['reference_id'])
        self.assertEqual(self.view()['coverage']['status'], 'stale')

    def test_stale_prose_cannot_start_a_new_local_video_task(self):
        record = self.read()
        record['status'] = 'video_generation_ready'
        studio.save(self.path, record)
        before = self.read()
        with patch.object(generation, '_start_local_worker') as worker:
            response = self.client.post(self.base + '/videos/generate', json={'revision': before['revision'], 'shot_ids': ['shot-A']})
        self.assertEqual(response.status_code, 400, response.text)
        worker.assert_not_called()
        self.assertEqual(self.read(), before)
        self.assertNotIn('video_request', self.read()['shots'][0])

    def test_legacy_unknown_prose_keeps_existing_generation_path(self):
        record = self.read()
        record['status'] = 'video_generation_ready'
        record['shots'][0].pop('video_prompt_inputs_stale')
        studio.save(self.path, record)
        self.assertEqual(self.view()['prompt_sync']['status'], 'unknown')
        with patch.object(generation, '_start_local_worker') as worker:
            response = self.client.post(self.base + '/videos/generate', json={'revision': record['revision'], 'shot_ids': ['shot-A']})
        self.assertEqual(response.status_code, 200, response.text)
        worker.assert_called_once()

    def test_existing_frozen_task_can_resume_even_when_current_prose_basis_is_stale(self):
        record = self.read()
        record['status'] = 'video_review'
        shot = record['shots'][0]
        generation._freeze_local_request(self.path, record, shot, 'composition_reference')
        shot.update(video_status='failed', video_task_id='already-accepted-task')
        original_request = copy.deepcopy(shot['video_request'])
        studio.save(self.path, record)
        with patch.object(generation, '_start_local_worker') as worker, \
                patch.object(prompt_inputs, 'assert_prompt_inputs_current') as guard:
            response = self.client.post(self.base + '/videos/generate', json={'revision': record['revision'], 'shot_ids': ['shot-A']})
        self.assertEqual(response.status_code, 200, response.text)
        worker.assert_called_once()
        guard.assert_not_called()
        self.assertEqual(self.read()['shots'][0]['video_request'], original_request)


if __name__ == '__main__':
    unittest.main()
