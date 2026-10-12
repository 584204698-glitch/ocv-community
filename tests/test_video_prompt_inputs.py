"""Video prose must track actual selected image bytes without inferring pixel review."""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from backend.app import video_generation as generation, video_inputs as inputs
from backend.app import video_prompt_inputs as prompts, video_studio as studio
from backend.app.video_project_cast import build_project_cast
from tests.test_video_inputs import profile


class VideoPromptInputsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.identity = 'a' * 32
        self.path = self.root / '1' / self.identity
        self.path.mkdir(parents=True)
        for name, color in (('core', 'white'), ('tail', 'blue'), ('rabbit', 'red')):
            Image.new('RGB', (20, 16), color).save(self.path / f'{name}.png')
        self.record = {
            'id': self.identity, 'revision': 1, 'status': 'image_review', 'logs': [],
            'settings': {'name': 'Synthetic input basis', 'ratio': '16:9'},
            'creation_parameters': {'video_generation_backend': 'comfyui', 'comfyui_profile_id': 'single_first'},
            'references': [{'id': name, 'name': name, 'file': f'{name}.png'} for name in ('tail', 'rabbit')],
            'shots': [{'id': 'shot-a', 'kind': 'video', 'image': 'core.png', 'image_status': 'completed',
                       'image_version': 'unchanged-cache-token', 'duration': 4, 'generation_duration': 4,
                       'action': '主角放下纸张。', 'intent': '访客来到门口。', 'video_prompt': 'A sets down the paper.'},
                      {'id': 'shot-b', 'kind': 'video', 'image': 'core.png', 'image_status': 'completed',
                       'duration': 4, 'generation_duration': 4, 'video_prompt': 'The room remains quiet.'}],
        }
        self.profiles = [profile('single_first'), profile('first_last'), profile('composition_reference', 2)]
        alternate = copy.deepcopy(self.profiles[0])
        alternate.update(id='alternate-first', name='Alternative first-frame workflow')
        self.profiles.append(alternate)
        for target, name, value in (
            (studio, 'ROOT', self.root),
            (studio, 'require_user', lambda request: {'id': 1}),
            (inputs, '_profiles', lambda owner: self.profiles),
            (inputs, 'video_profile', lambda owner, identity: next(row for row in self.profiles if row['id'] == identity)),
        ):
            replacement = patch.object(target, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)
        studio.save(self.path, self.record)
        app = FastAPI()
        app.include_router(inputs.router)
        app.include_router(generation.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.base = f'/api/video-studio/{self.identity}'

    @property
    def shot(self):
        return self.record['shots'][0]

    @staticmethod
    def selection(mode='single_first', **changes):
        return {'input_mode': mode, 'profile_id': '', 'end_frame_reference_id': '', 'reference_ids': [], **changes}

    def choose(self, mode='single_first', **changes):
        self.shot['video_input_selection'] = self.selection(mode, **changes)

    def basis(self):
        return prompts.prompt_input_basis(1, self.path, self.record, self.shot)

    def synchronize(self):
        basis = self.basis()
        prompts.adopt_prompt_basis(self.shot, basis)
        return basis

    def state(self):
        return prompts.prompt_sync_view(1, self.path, self.record, self.shot)

    def bind_rabbit(self, **changes):
        previous = (self.record.get('project_cast') or {}).get('characters', [])
        declaration = {'id': previous[0]['id'] if previous else '', 'name': '小兔',
                       'reference_id': 'rabbit', 'constraints': '保持绿色衣裙。', **changes}
        self.record['project_cast'] = build_project_cast(1, self.path, self.record,
            inputs.asset_rows(self.record), [declaration])

    def test_basis_matches_all_three_input_modes_and_explicit_picture_order(self):
        cases = [
            ('single_first', {}, ['shot-a'], ['first_frame']),
            ('first_last', {'end_frame_reference_id': 'tail'}, ['shot-a', 'tail'], ['first_frame', 'last_frame']),
            ('composition_reference', {'reference_ids': ['rabbit', 'tail']},
             ['shot-a', 'rabbit', 'tail'], ['composition', 'identity_reference', 'identity_reference']),
        ]
        for mode, changes, identities, roles in cases:
            with self.subTest(mode=mode):
                self.choose(mode, **changes)
                before = copy.deepcopy(self.record)
                basis = self.basis()
                self.assertEqual(basis['input_mode'], mode)
                self.assertEqual([row['id'] for row in basis['images']], identities)
                self.assertEqual([row['role'] for row in basis['images']], roles)
                self.assertEqual([row['label'] for row in basis['images']],
                                 [f'<Picture {index}>' for index in range(1, len(identities) + 1)])
                for row in basis['images']:
                    filename = 'core.png' if row['id'] == 'shot-a' else row['id'] + '.png'
                    self.assertEqual(row['sha256'], hashlib.sha256((self.path / filename).read_bytes()).hexdigest())
                    self.assertNotIn('file', row)
                    self.assertNotIn('path', row)
                self.assertEqual(self.record, before, 'constructing evidence must not update the project')

    def test_identity_binding_does_not_claim_the_role_is_visible_or_select_its_image(self):
        self.bind_rabbit()
        self.shot['video_character_review'] = {'sentinel': 'visibility is a separate declaration'}
        baseline_review = copy.deepcopy(self.shot['video_character_review'])
        bound = self.basis()['project_characters'][0]
        self.assertEqual(bound['input_status'], 'project_bound_only')
        self.assertEqual(bound['video_picture_labels'], [])
        self.assertNotIn('presence', bound)
        self.assertNotIn('covered', bound)
        self.choose('composition_reference', reference_ids=['tail', 'rabbit'])
        selected = self.basis()['project_characters'][0]
        self.assertEqual(selected['id'], bound['id'])
        self.assertEqual(selected['video_picture_labels'], ['<Picture 3>'])
        self.assertEqual(selected['input_status'], 'identity_reference_selected')
        self.synchronize()
        self.assertEqual(self.shot['video_character_review'], baseline_review)

    def test_selected_file_bytes_expire_saved_basis_without_a_path_or_version_change(self):
        self.choose('composition_reference', reference_ids=['rabbit'])
        original = self.synchronize()
        for filename in ('core.png', 'rabbit.png'):
            with self.subTest(filename=filename):
                old_bytes = (self.path / filename).read_bytes()
                Image.new('RGB', (20, 16), 'green').save(self.path / filename)
                self.assertEqual(self.shot['image_version'], 'unchanged-cache-token')
                self.assertNotEqual(prompts.input_fingerprint(self.basis()), prompts.input_fingerprint(original))
                self.assertEqual(self.state()['status'], 'stale')
                with self.assertRaises(ValueError):
                    prompts.assert_prompt_inputs_current(1, self.path, self.record, self.shot)
                (self.path / filename).write_bytes(old_bytes)
                self.assertEqual(self.state()['status'], 'current')

    def test_mode_or_picture_order_changes_expire_basis_but_workflow_identity_does_not(self):
        self.choose('composition_reference', profile_id='composition_reference', reference_ids=['rabbit', 'tail'])
        original = self.synchronize()
        self.shot['video_input_selection']['profile_id'] = 'another-equivalent-workflow'
        self.assertEqual(self.basis(), original)
        self.assertEqual(self.state()['status'], 'current')
        self.shot['video_input_selection']['reference_ids'] = ['tail', 'rabbit']
        self.assertEqual(self.state()['status'], 'stale')
        self.choose('single_first')
        self.assertEqual(self.state()['status'], 'stale')

    def test_resaved_project_identity_changes_expire_prompt_basis(self):
        self.bind_rabbit()
        self.choose('composition_reference', reference_ids=['rabbit'])
        original = self.synchronize()
        self.bind_rabbit(constraints='始终穿绿色衣裙，并保留白色袖口。')
        changed = self.basis()
        self.assertEqual(changed['images'], original['images'])
        self.assertEqual(changed['project_characters'][0]['id'], original['project_characters'][0]['id'])
        self.assertNotEqual(changed['project_characters'][0]['identity_sha256'], original['project_characters'][0]['identity_sha256'])
        self.assertEqual(self.state()['status'], 'stale')

    def test_changed_bound_identity_bytes_are_unavailable_until_the_binding_is_reviewed(self):
        self.bind_rabbit()
        self.synchronize()
        Image.new('RGB', (20, 16), 'green').save(self.path / 'rabbit.png')
        state = self.state()
        self.assertEqual(state['status'], 'unavailable')
        self.assertFalse(state['can_sync'])
        with self.assertRaises(ValueError):
            prompts.assert_prompt_inputs_current(1, self.path, self.record, self.shot)

    def test_prose_changes_expire_basis_while_logs_and_project_revision_do_not(self):
        self.synchronize()
        self.record['revision'] = 90
        self.record['logs'].append('Harmless progress information')
        self.assertEqual(self.state()['status'], 'current')
        changes = {'action': '先抬手再放下纸张。', 'motion_plan': {'beats': ['enter', 'stop']},
                   'video_prompt': 'The rabbit enters.', 'intent': '转而等待访客。', 'duration': 5,
                   'cast_plan': {'characters': []}}
        for key, value in changes.items():
            with self.subTest(field=key):
                prior = copy.deepcopy(self.shot)
                self.shot[key] = value
                self.assertEqual(self.state()['status'], 'stale')
                self.record['shots'][0] = prior
        self.assertEqual(self.state()['status'], 'current')

    def test_legacy_unknown_remains_compatible_but_explicit_stale_requires_sync(self):
        self.assertEqual(self.state()['status'], 'unknown')
        self.assertTrue(self.state()['can_sync'])
        prompts.assert_prompt_inputs_current(1, self.path, self.record, self.shot)
        self.shot['video_prompt_inputs_stale'] = True
        self.assertEqual(self.state()['status'], 'stale')
        with self.assertRaises(ValueError):
            prompts.assert_prompt_inputs_current(1, self.path, self.record, self.shot)
        self.synchronize()
        self.assertNotIn('video_prompt_inputs_stale', self.shot)
        self.assertEqual(self.state()['status'], 'current')
        prompts.assert_prompt_inputs_current(1, self.path, self.record, self.shot)

    def test_corrupt_basis_duplicate_ids_and_nonimage_bytes_cannot_claim_current(self):
        self.synchronize()
        self.shot['video_prompt_input_basis']['version'] = 99
        self.assertEqual(self.state()['status'], 'stale')
        self.choose('composition_reference', reference_ids=['rabbit'])
        self.record['video_input_assets'] = [copy.deepcopy(self.record['references'][1])]
        self.assertEqual(self.state()['status'], 'unavailable')
        self.record.pop('video_input_assets')
        (self.path / 'rabbit.png').write_bytes(b'not-an-image')
        self.assertEqual(self.state()['status'], 'unavailable')
        self.assertFalse(self.state()['can_sync'])

    def test_stale_new_generation_is_rejected_before_worker_or_frozen_assets(self):
        self.synchronize()
        self.shot['action'] = '正文已修改但尚未确认输入依据。'
        self.record['status'] = 'video_generation_ready'
        studio.save(self.path, self.record)
        before = (self.path / 'record.json').read_bytes()
        with patch.object(generation, '_start_local_worker') as worker, \
             patch.object(generation, '_freeze_local_request') as freeze, \
             patch.object(generation, 'convert_for_h3') as convert:
            response = self.client.post(self.base + '/videos/generate', json={'revision': 1, 'shot_ids': ['shot-a']})
            self.assertEqual(response.status_code, 400, response.text)
            worker.assert_not_called()
            freeze.assert_not_called()
            convert.assert_not_called()
        self.assertEqual((self.path / 'record.json').read_bytes(), before)
        self.assertFalse((self.path / 'assets' / 'videos').exists())

    def test_legacy_unknown_new_generation_still_enters_existing_worker_path(self):
        self.record['status'] = 'video_generation_ready'
        studio.save(self.path, self.record)
        with patch.object(generation, '_start_local_worker') as worker:
            response = self.client.post(self.base + '/videos/generate', json={'revision': 1, 'shot_ids': ['shot-a']})
            self.assertEqual(response.status_code, 200, response.text)
            worker.assert_called_once()
            self.assertEqual(worker.call_args.args[2], ['shot-a'])

    def test_put_adopts_without_model_calls_preserves_prose_and_marks_changed_images_stale(self):
        self.synchronize()
        studio.save(self.path, self.record)
        original_shot = copy.deepcopy(self.shot)
        with patch('backend.app.video_agents.generate_gemini_text', side_effect=AssertionError('PUT must not call a model')) as director, \
             patch('backend.app.h3_prompt_agent.generate_gemini_text', side_effect=AssertionError('PUT must not call a model')) as h3, \
             patch.object(generation, '_start_local_worker') as worker:
            response = self.client.put(self.base + '/shots/shot-a/video-inputs', json={
                'revision': 1, **self.selection('composition_reference', profile_id='composition_reference', reference_ids=['rabbit'])})
            self.assertEqual(response.status_code, 200, response.text)
            director.assert_not_called()
            h3.assert_not_called()
            worker.assert_not_called()
        saved = response.json()
        self.assertEqual(saved['revision'], 2)
        self.assertEqual(saved['shots'][1], self.record['shots'][1])
        for field in ('action', 'intent', 'video_prompt', 'image_prompt', 'video_prompt_input_basis'):
            self.assertEqual(saved['shots'][0].get(field), original_shot.get(field))
        self.assertTrue(saved['shots'][0]['video_prompt_inputs_stale'])
        view = self.client.get(self.base + '/shots/shot-a/video-inputs').json()
        self.assertEqual(view['prompt_sync']['status'], 'stale')
        self.assertTrue(view['prompt_sync']['can_sync'])
        self.assertIn(view['prompt_sync']['message'], view['blockers'])

    def test_put_same_selection_is_idempotent_including_record_bytes_and_logs(self):
        self.choose('first_last', profile_id='first_last', end_frame_reference_id='tail')
        self.synchronize()
        studio.save(self.path, self.record)
        before = (self.path / 'record.json').read_bytes()
        response = self.client.put(self.base + '/shots/shot-a/video-inputs', json={
            'revision': 1, **self.shot['video_input_selection']})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['revision'], 1)
        self.assertEqual((self.path / 'record.json').read_bytes(), before)

    def test_put_workflow_only_change_keeps_current_prose_basis(self):
        self.choose()
        self.synchronize()
        studio.save(self.path, self.record)
        original_basis = copy.deepcopy(self.shot['video_prompt_input_basis'])
        response = self.client.put(self.base + '/shots/shot-a/video-inputs', json={
            'revision': 1, **self.selection(profile_id='alternate-first')})
        self.assertEqual(response.status_code, 200, response.text)
        saved = response.json()
        self.assertEqual(saved['revision'], 2)
        self.assertFalse(saved['shots'][0].get('video_prompt_inputs_stale', False))
        self.assertEqual(saved['shots'][0]['video_prompt_input_basis'], original_basis)
        view = self.client.get(self.base + '/shots/shot-a/video-inputs').json()
        self.assertEqual(view['prompt_sync']['status'], 'current')
        self.assertNotIn(view['prompt_sync']['message'], view['blockers'])

    def test_input_view_requires_idle_local_video_and_valid_workflow_for_sync(self):
        self.assertTrue(inputs.input_view(1, self.path, self.record, self.shot)['prompt_sync']['can_sync'])
        cases = [
            ('running', lambda: self.record.update(status='video_generating')),
            ('cloud', lambda: self.record['creation_parameters'].update(video_generation_backend='api')),
            ('static', lambda: self.shot.update(kind='static')),
            ('wrong-workflow-mode', lambda: self.choose('first_last', profile_id='single_first', end_frame_reference_id='tail')),
        ]
        for label, change in cases:
            with self.subTest(case=label):
                original = copy.deepcopy(self.record)
                change()
                view = inputs.input_view(1, self.path, self.record, self.shot)
                self.assertFalse(view['prompt_sync']['can_sync'])
                if label == 'wrong-workflow-mode':
                    self.assertTrue(view['blockers'])
                self.record = original


if __name__ == '__main__':
    unittest.main()
