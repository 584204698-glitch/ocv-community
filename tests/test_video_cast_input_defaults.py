"""Cast-plan defaults supply real video inputs without inventing visual review."""
import copy
import unittest
from unittest.mock import patch

from PIL import Image

from backend.app import video_inputs as inputs, video_studio as studio, video_generation as generation
from backend.app import video_cast_planning as planning, video_prompt_inputs as prompts
from backend.app.video_project_cast import build_project_cast
from tests import test_video_character_coverage as fixtures
from tests.test_video_inputs import h3_prompt


class CastInputDefaultsTest(unittest.TestCase):
    setUp = fixtures.VideoCharacterCoverageTest.setUp

    @property
    def shot(self):
        return self.record['shots'][0]

    def prepare(self, indices=(0, 1)):
        self.record['creation_parameters']['comfyui_profile_id'] = 'composition_reference'
        self.record['project_cast'] = build_project_cast(1, self.path, self.record, inputs.asset_rows(self.record), [
            {'name': 'Opening', 'reference_id': 'actor-a', 'constraints': 'No visible ears.'},
            {'name': 'Later', 'reference_id': 'actor-b', 'constraints': 'Green coat.'},
            {'name': 'Other scene', 'reference_id': 'tail'},
        ])
        self.plan(indices)
        # Image references deliberately exclude the later character and include
        # a scene asset. They must never become the full video identity list.
        self.shot['reference_ids'] = ['actor-a', 'tail']
        studio.save(self.path, self.record)

    def plan(self, indices):
        snapshot = planning.saved_planning_cast(self.record)
        self.shot['cast_plan'] = planning.bind_planned_characters(snapshot, [
            {'project_character_id': snapshot['characters'][index]['project_character_id'],
             'presence': 'opening' if index == 0 else 'later', 'basis': 'Explicit narrative timing.'}
            for index in indices])

    def test_view_basis_coverage_and_freeze_share_real_order_without_adoption(self):
        self.prepare()
        before = copy.deepcopy(self.record)
        saved_bytes = (self.path / 'record.json').read_bytes()
        response = self.client.get(self.url + '/video-inputs')
        self.assertEqual(response.status_code, 200, response.text)
        view = response.json()
        selection, images = inputs.resolve_video_inputs(self.path, self.record, self.shot)
        self.assertEqual(view['selection'], selection)
        self.assertEqual(selection['input_mode'], 'composition_reference')
        self.assertEqual(selection['reference_ids'], ['actor-a', 'actor-b'])
        self.assertEqual([row['id'] for row in images], ['shot-A', 'actor-a', 'actor-b'])
        self.assertEqual([row['label'] for row in images], ['<Picture 1>', '<Picture 2>', '<Picture 3>'])
        self.assertEqual(view['blockers'], [])
        self.assertEqual(view['coverage']['status'], 'unknown')
        self.assertEqual(view['prompt_sync']['status'], 'unknown')
        basis = prompts.prompt_input_basis(1, self.path, self.record, self.shot)
        self.assertEqual([row['id'] for row in basis['images']], [row['id'] for row in images])
        self.assertEqual([row['input_status'] for row in basis['project_characters']],
                         ['identity_reference_selected', 'identity_reference_selected', 'project_bound_only'])
        self.assertEqual(self.record, before)
        self.assertEqual((self.path / 'record.json').read_bytes(), saved_bytes)
        context = generation._confirmed_character_inputs(1, self.path, self.record, self.shot, selection, images)
        self.assertEqual(context['status'], 'unknown')
        self.assertEqual(context['characters'], [])
        generation._freeze_local_request(self.path, self.record, self.shot, 'composition_reference',
                                         character_inputs=context)
        frozen = self.shot['video_request']
        self.assertEqual(frozen['input_mode'], selection['input_mode'])
        self.assertEqual([row['id'] for row in frozen['input_manifest']], [row['id'] for row in images])
        self.assertEqual(frozen['character_inputs']['status'], 'unknown')
        for field in ('video_input_selection', 'video_character_review', 'video_prompt_input_basis'):
            self.assertNotIn(field, self.shot)

    def test_plan_order_and_shared_reference_dedupe_are_stable(self):
        self.prepare((1, 0))
        self.assertEqual(inputs.effective_selection(self.path, self.record, self.shot)['reference_ids'],
                         ['actor-b', 'actor-a'])
        rows = self.record['project_cast']['characters']
        declarations = [{key: row[key] for key in ('id', 'name', 'reference_id', 'constraints')} for row in rows]
        declarations[0]['reference_id'] = 'actor-b'
        self.record['project_cast'] = build_project_cast(1, self.path, self.record, inputs.asset_rows(self.record), declarations)
        self.plan((1, 0))
        self.assertEqual(inputs.effective_selection(self.path, self.record, self.shot)['reference_ids'], ['actor-b'])

    def test_saved_choices_including_empty_default_are_never_replaced(self):
        self.prepare()
        choices = [{}, {'input_mode': 'single_first', 'profile_id': 'single_first'},
                   {'input_mode': 'first_last', 'profile_id': 'first_last', 'end_frame_reference_id': 'tail'},
                   {'input_mode': 'composition_reference', 'reference_ids': ['actor-b', 'actor-a']}]
        for choice in choices:
            with self.subTest(choice=choice):
                self.shot['video_input_selection'] = choice
                with patch.object(inputs, 'verified_planning_cast', side_effect=AssertionError('manual choice was replaced')):
                    self.assertEqual(inputs.effective_selection(self.path, self.record, self.shot),
                                     inputs.VideoInputSelection.model_validate(choice).model_dump())

    def test_first_manual_default_selection_is_persisted_instead_of_ignored(self):
        self.prepare()
        self.record['creation_parameters']['comfyui_profile_id'] = 'single_first'
        studio.save(self.path, self.record)
        response = self.client.put(self.url + '/video-inputs', json={'revision': 1})
        self.assertEqual(response.status_code, 200, response.text)
        saved = response.json()
        self.assertEqual(saved['revision'], 2)
        self.assertEqual(saved['shots'][0]['video_input_selection'], inputs.VideoInputSelection().model_dump())
        saved['creation_parameters']['comfyui_profile_id'] = 'composition_reference'
        self.assertEqual(inputs.effective_selection(self.path, saved, saved['shots'][0])['input_mode'], 'single_first')

    def test_first_manual_pin_of_derived_inputs_is_saved_and_repeat_is_noop(self):
        self.prepare()
        selection = inputs.effective_selection(self.path, self.record, self.shot)
        response = self.client.put(self.url + '/video-inputs', json={'revision': 1, **selection})
        self.assertEqual(response.status_code, 200, response.text)
        saved = response.json()
        self.assertEqual(saved['shots'][0]['video_input_selection'], selection)
        record_bytes = (self.path / 'record.json').read_bytes()
        response = self.client.put(self.url + '/video-inputs', json={'revision': saved['revision'], **selection})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((self.path / 'record.json').read_bytes(), record_bytes)
        self.assertNotIn('video_character_review', saved['shots'][0])
        self.assertNotIn('video_prompt_input_basis', saved['shots'][0])

    def test_empty_plan_is_valid_but_never_claims_empty_visual_review(self):
        self.prepare(())
        view = inputs.input_view(1, self.path, self.record, self.shot)
        self.assertEqual(view['selection']['input_mode'], 'composition_reference')
        self.assertEqual(view['selection']['reference_ids'], [])
        self.assertEqual(view['blockers'], [])
        self.assertEqual(view['coverage']['status'], 'unknown')

    def pin_frozen(self, project_status, video_status, *, profile_id=''):
        self.prepare()
        generation._freeze_local_request(self.path, self.record, self.shot, 'composition_reference')
        self.record.update(status=project_status, export={'file': 'existing-film.mp4'})
        self.shot.update(video_status=video_status, video_task_id='existing-task',
                         h3_prompt_source='existing-converter', video_resume_available=video_status == 'failed')
        (self.path / self.shot['video']).write_bytes(b'existing clip')
        prompts.adopt_prompt_basis(self.shot, prompts.prompt_input_basis(1, self.path, self.record, self.shot))
        studio.save(self.path, self.record)
        before = copy.deepcopy(self.record)
        selection = inputs.effective_selection(self.path, self.record, self.shot)
        selection['profile_id'] = profile_id
        response = self.client.put(self.url + '/video-inputs', json={'revision': 1, **selection})
        self.assertEqual(response.status_code, 200, response.text)
        return before, response.json(), selection

    def test_pinning_identical_inputs_preserves_completed_clip_and_export(self):
        before, saved, selection = self.pin_frozen('completed', 'completed', profile_id='composition_reference')
        expected = copy.deepcopy(before['shots'][0])
        expected['video_input_selection'] = selection
        self.assertEqual(saved['shots'][0], expected)
        self.assertEqual(saved['status'], 'completed')
        self.assertEqual(saved['export'], before['export'])
        self.assertEqual((self.path / saved['shots'][0]['video']).read_bytes(), b'existing clip')
        self.assertEqual(saved['shots'][1], before['shots'][1])

    def test_pinning_identical_inputs_preserves_pending_frozen_task_and_prompt_basis(self):
        before, saved, selection = self.pin_frozen('video_review', 'failed')
        expected = copy.deepcopy(before['shots'][0])
        expected['video_input_selection'] = selection
        self.assertEqual(saved['shots'][0], expected)
        self.assertEqual(saved['shots'][0]['video_task_id'], 'existing-task')
        self.assertTrue(saved['shots'][0]['video_resume_available'])
        self.assertEqual(saved['export'], before['export'])

    def test_real_profile_change_still_invalidates_even_with_identical_images(self):
        alternate = copy.deepcopy(self.profiles[2])
        alternate['id'] = 'alternate-composition'
        self.profiles.append(alternate)
        before, saved, _ = self.pin_frozen('completed', 'completed', profile_id='alternate-composition')
        self.assertEqual(saved['status'], 'video_review')
        self.assertEqual(saved['shots'][0]['video_status'], 'pending')
        self.assertNotIn('video', saved['shots'][0])
        self.assertNotIn('export', saved)
        self.assertEqual(saved['shots'][0]['video_history'][0]['video_request'], before['shots'][0]['video_request'])

    def test_legacy_non_comfyui_and_non_composition_inputs_keep_their_defaults(self):
        before = copy.deepcopy(self.record)
        self.assertEqual(inputs.effective_selection(self.path, self.record, self.shot), inputs.selection_for(self.shot))
        self.prepare()
        for key, value in [('video_generation_backend', 'api'), ('comfyui_profile_id', 'single_first')]:
            with self.subTest(key=key):
                record = copy.deepcopy(self.record)
                record['creation_parameters'][key] = value
                self.assertEqual(inputs.effective_selection(self.path, record, record['shots'][0]),
                                 inputs.selection_for(record['shots'][0]))
        before['creation_parameters']['comfyui_profile_id'] = 'composition_reference'
        with patch.object(inputs, 'video_profile', side_effect=AssertionError('legacy selection must not inspect profiles')):
            self.assertEqual(inputs.effective_selection(self.path, before, before['shots'][0])['input_mode'], 'single_first')

    def test_invalid_binding_or_plan_blocks_get_and_new_work_without_mutation(self):
        self.prepare()
        baseline = copy.deepcopy(self.record)
        cases = {
            'missing plan': lambda row: row['shots'][0].pop('cast_plan'),
            'broken plan': lambda row: row['shots'][0].update(cast_plan={}),
            'foreign plan': lambda row: row['shots'][0]['cast_plan'].update(project_id='d' * 32),
            'stale plan': lambda row: row['shots'][0]['cast_plan']['characters'][1].update(identity_sha256='0' * 64),
            'foreign owner': lambda row: row['project_cast'].update(owner_id=2),
            'foreign cast project': lambda row: row['project_cast'].update(project_id='d' * 32),
            'removed cast': lambda row: row.pop('project_cast'),
            'changed identity': lambda row: row['project_cast']['characters'][1].update(constraints='Changed coat.'),
            'duplicate image ID': lambda row: row['references'].append(copy.deepcopy(row['references'][2])),
            'missing image': lambda row: row['references'][2].update(file='missing.png'),
            'outside image': lambda row: row['references'][2].update(file='../outside.png'),
        }
        for name, alter in cases.items():
            with self.subTest(name=name):
                record = copy.deepcopy(baseline)
                alter(record)
                record['status'] = 'video_generation_ready'
                studio.save(self.path, record)
                record_bytes = (self.path / 'record.json').read_bytes()
                response = self.client.get(self.url + '/video-inputs')
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(response.json()['blockers'])
                self.assertFalse(response.json()['prompt_sync']['can_sync'])
                self.assertEqual(response.json()['coverage']['status'], 'unknown')
                with patch.object(generation.threading, 'Thread') as thread:
                    response = self.client.post(self.base + '/videos/generate', json={'revision': 1, 'shot_ids': ['shot-A']})
                    self.assertEqual(response.status_code, 400, response.text)
                    thread.assert_not_called()
                self.assertEqual((self.path / 'record.json').read_bytes(), record_bytes)
                self.assertFalse((self.path / 'assets/videos/shot-A').exists())

    def test_changed_picture_bytes_fail_existing_sha_verification(self):
        self.prepare()
        Image.new('RGB', (18, 16), 'purple').save(self.path / 'actor-b.png')
        with self.assertRaisesRegex(ValueError, '角色图片或资料已变化'):
            inputs.resolve_video_inputs(self.path, self.record, self.shot)
        self.assertTrue(inputs.input_view(1, self.path, self.record, self.shot)['blockers'])

    def test_workflow_lookup_uses_directory_owner_before_rejecting_foreign_cast(self):
        self.prepare()
        self.record['project_cast']['owner_id'] = 2
        with patch.object(inputs, 'video_profile', return_value=self.profiles[2]) as profile:
            with self.assertRaisesRegex(ValueError, '账号或项目不一致'):
                inputs.effective_selection(self.path, self.record, self.shot)
            profile.assert_called_once_with(1, 'composition_reference')

    def test_h3_slots_expand_and_eight_reference_limit_never_truncates(self):
        self.prepare()
        original = copy.deepcopy(self.record)
        self.profiles[2] = fixtures.profile('composition_reference', capacity=1)
        with patch.object(inputs, 'video_profile', return_value=inputs.expand_h3_reference_capacity(self.profiles[2])):
            selection, assets = inputs.resolve_video_inputs(self.path, self.record, self.shot)
        self.assertEqual(selection['reference_ids'], ['actor-a', 'actor-b'])
        self.assertEqual(len(assets), 3)
        self.assertEqual(self.record, original)
        view = inputs.input_view(1, self.path, self.record, self.shot)
        self.assertEqual(next(row for row in view['profiles'] if row['id'] == 'composition_reference')['reference_capacity'], 8)
        rows = []
        for index in range(9):
            identity = f'identity-{index}'
            self.record['references'].append({'id': identity, 'name': identity, 'file': 'actor-a.png'})
            rows.append({'name': identity, 'reference_id': identity})
        self.record['project_cast'] = build_project_cast(1, self.path, self.record, inputs.asset_rows(self.record), rows)
        self.plan(range(9))
        with self.assertRaisesRegex(ValueError, '9 张身份参考.*8 张上限'):
            inputs.resolve_video_inputs(self.path, self.record, self.shot)
        self.assertEqual(len(self.shot['cast_plan']['characters']), 9)
        self.assertNotIn('video_input_selection', self.shot)

    def test_existing_prompt_basis_becomes_stale_and_unknown_is_not_synchronized(self):
        self.prepare((0,))
        prompts.adopt_prompt_basis(self.shot, prompts.prompt_input_basis(1, self.path, self.record, self.shot))
        self.plan((0, 1))
        view = inputs.input_view(1, self.path, self.record, self.shot)
        self.assertEqual(view['prompt_sync']['status'], 'stale')
        self.assertTrue(view['blockers'])
        self.assertEqual(view['coverage']['status'], 'unknown')
        with self.assertRaises(ValueError):
            prompts.assert_prompt_inputs_current(1, self.path, self.record, self.shot)

    def test_explicit_user_coverage_uses_derived_later_reference(self):
        self.prepare()
        cast = self.record['project_cast']['characters'][1]
        response = self.client.put(self.url + '/character-coverage', json={'revision': 1, 'characters': [
            {'name': cast['name'], 'presence': 'later', 'reference_id': cast['reference_id'],
             'project_character_id': cast['id'], 'in_last_frame': False}]})
        self.assertEqual(response.status_code, 200, response.text)
        view = self.client.get(self.url + '/video-inputs').json()
        self.assertEqual(view['coverage']['status'], 'covered')
        self.assertEqual(view['selection']['reference_ids'], ['actor-a', 'actor-b'])
        self.assertNotIn('video_input_selection', response.json()['shots'][0])
        self.assertEqual(response.json()['shots'][0]['video_character_review']['source'], 'user_confirmed')

    def test_worker_h3_and_frozen_submission_receive_the_same_later_identity(self):
        self.prepare()
        self.record['status'] = 'video_generation_ready'
        self.record['creation_parameters']['comfyui_h3_prompt_agent'] = True
        studio.save(self.path, self.record)

        def convert(shot, duration, **kwargs):
            self.assertEqual(kwargs['input_mode'], 'composition_reference')
            self.assertEqual([row['id'] for row in kwargs['reference_images']], ['actor-a', 'actor-b'])
            self.assertEqual(shot['_confirmed_character_inputs']['status'], 'unknown')
            return h3_prompt('composition_reference'), 'synthetic-converter'

        def run(*args, **kwargs):
            self.assertEqual(kwargs['input_mode'], 'composition_reference')
            self.assertEqual(len(kwargs['reference_image_paths']), 2)
            self.assertTrue(all(path.is_file() for path in kwargs['reference_image_paths']))
            kwargs['output_path'].write_bytes(b'synthetic completed clip')

        with patch.object(generation.threading, 'Thread') as thread, \
                patch.object(generation, 'convert_for_h3', side_effect=convert) as converter, \
                patch.object(generation, 'run_video_profile', side_effect=run) as bridge:
            response = self.client.post(self.base + '/videos/generate', json={'revision': 1, 'shot_ids': ['shot-A']})
            self.assertEqual(response.status_code, 200, response.text)
            thread.call_args.kwargs['target']()
            self.assertEqual(converter.call_count, 1)
            self.assertEqual(bridge.call_count, 1)
        saved = studio.read(self.path)
        shot = saved['shots'][0]
        self.assertEqual(shot['video_status'], 'completed', saved['logs'])
        self.assertEqual([row['id'] for row in shot['video_request']['input_manifest']], ['shot-A', 'actor-a', 'actor-b'])
        self.assertNotIn('video_character_review', shot)
        self.assertNotIn('video_prompt_input_basis', shot)

    def test_frozen_task_recovery_ignores_now_invalid_cast_and_deleted_originals(self):
        self.prepare()
        self.record['status'] = 'video_review'
        generation._freeze_local_request(self.path, self.record, self.shot, 'composition_reference')
        frozen = copy.deepcopy(self.shot['video_request'])
        self.shot.update(video_status='failed', video_task_id='original-task')
        self.record['project_cast']['owner_id'] = 2
        studio.save(self.path, self.record)
        self.profiles.clear()
        for name in ('core', 'actor-a', 'actor-b'):
            (self.path / f'{name}.png').unlink()

        def run(*args, **kwargs):
            self.assertEqual(kwargs['existing_prompt_id'], 'original-task')
            self.assertEqual(kwargs['input_mode'], 'composition_reference')
            self.assertEqual(len(kwargs['reference_image_paths']), 2)
            self.assertTrue(all(path.is_file() for path in kwargs['reference_image_paths']))
            kwargs['output_path'].write_bytes(b'synthetic recovered clip')

        with patch.object(generation.threading, 'Thread') as thread, \
                patch.object(generation, 'video_task_state', return_value='completed'), \
                patch.object(generation, 'convert_for_h3') as converter, \
                patch.object(generation, 'run_video_profile', side_effect=run) as bridge:
            response = self.client.post(self.base + '/videos/generate', json={'revision': 1, 'shot_ids': ['shot-A']})
            self.assertEqual(response.status_code, 200, response.text)
            thread.call_args.kwargs['target']()
            converter.assert_not_called()
            self.assertEqual(bridge.call_count, 1)
        saved = studio.read(self.path)
        self.assertEqual(saved['shots'][0]['video_status'], 'completed', saved['logs'])
        self.assertEqual(saved['shots'][0]['video_request'], frozen)
        self.assertEqual(saved['shots'][0]['video_attempt'], 1)
        self.assertFalse((self.path / 'assets/videos/shot-A/attempt_002').exists())


if __name__ == '__main__':
    unittest.main()
