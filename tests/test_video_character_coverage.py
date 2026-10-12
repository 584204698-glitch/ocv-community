import copy
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from backend.app import video_inputs as inputs, video_studio as studio, video_generation as generation


def profile(mode, capacity=3):
    graph = {'core': {'class_type': 'LoadImage', 'inputs': {'image': 'core.png'}},
             'text': {'class_type': 'Text', 'inputs': {'text': ''}}}
    mappings = {'image': {'node_id': 'core', 'input_name': 'image'},
                'prompt': {'node_id': 'text', 'input_name': 'text'}}
    media = {'first_frame': ['core', 0]}
    if mode == 'first_last':
        graph['tail'] = {'class_type': 'LoadImage', 'inputs': {'image': 'tail.png'}}
        mappings['last_image'] = {'node_id': 'tail', 'input_name': 'image'}
        media['last_frame'] = ['tail', 0]
    elif mode == 'composition_reference':
        media = {'ref_images.ref_image_0': ['core', 0]}
        mappings['reference_images'] = []
        for index in range(capacity):
            node = f'ref-{index}'
            graph[node] = {'class_type': 'LoadImage', 'inputs': {'image': 'candidate.png'}}
            mappings['reference_images'].append({'node_id': node, 'input_name': 'image'})
            media[f'ref_images.ref_image_{index + 1}'] = [node, 0]
    graph['h3'] = {'class_type': 'MiniMaxH3ReferenceToVideo' if mode == 'composition_reference'
                   else 'MiniMaxH3ImageToVideo', 'inputs': media}
    return {'id': mode, 'kind': 'video', 'name': mode, 'video_input_mode': mode,
            'workflow': graph, 'mappings': mappings}


class VideoCharacterCoverageTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.identity = 'c' * 32
        self.path = self.root / '1' / self.identity
        self.path.mkdir(parents=True)
        for name, color in [('core', 'white'), ('tail', 'blue'), ('actor-a', 'red'), ('actor-b', 'green')]:
            Image.new('RGB', (18, 16), color).save(self.path / f'{name}.png')
        self.record = {
            'id': self.identity, 'revision': 1, 'status': 'image_review', 'logs': [],
            'settings': {'name': 'Synthetic character review', 'ratio': '16:9'},
            'creation_parameters': {'video_generation_backend': 'comfyui', 'comfyui_profile_id': 'single_first'},
            'references': [{'id': name, 'name': name, 'file': f'{name}.png'}
                           for name in ('tail', 'actor-a', 'actor-b')],
            'shots': [
                {'id': 'shot-A', 'kind': 'video', 'image': 'core.png', 'image_status': 'completed',
                 'image_version': 'core-v1', 'duration': 4, 'generation_duration': 4,
                 'video_prompt': 'Actor A waits while Actor B enters.', 'action': 'A waits; B enters.'},
                {'id': 'shot-B', 'kind': 'video', 'image': 'core.png', 'image_status': 'completed',
                 'duration': 4, 'generation_duration': 4, 'video_prompt': 'An empty courtyard.'},
            ],
        }
        self.profiles = [profile(mode) for mode in ('single_first', 'first_last', 'composition_reference')]
        for target, name, value in (
            (studio, 'ROOT', self.root), (studio, 'require_user', lambda request: {'id': 1}),
            (inputs, '_profiles', lambda owner: self.profiles),
            (inputs, 'video_profile', lambda owner, identity: next(row for row in self.profiles if row['id'] == identity)),
            (generation, '_connection', lambda owner: {'base_url': 'http://127.0.0.1:8188', 'prompt_path': '/prompt'}),
        ):
            handle = patch.object(target, name, value)
            handle.start()
            self.addCleanup(handle.stop)
        studio.save(self.path, self.record)
        app = FastAPI()
        app.include_router(inputs.router)
        app.include_router(generation.router)
        self.client = TestClient(app)
        self.base = f'/api/video-studio/{self.identity}'
        self.url = self.base + '/shots/shot-A'

    def read(self):
        return studio.read(self.path)

    def coverage(self):
        response = self.client.get(self.url + '/video-inputs')
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['coverage']

    def confirm(self, characters, **kwargs):
        payload = {'revision': self.read()['revision'], 'characters': characters, **kwargs}
        return self.client.put(self.url + '/character-coverage', json=payload)

    def save_selection(self, mode, **kwargs):
        return self.client.put(self.url + '/video-inputs', json={
            'revision': self.read()['revision'], 'input_mode': mode, 'profile_id': mode,
            'reference_ids': [], 'end_frame_reference_id': '', **kwargs})

    @staticmethod
    def actor(name='Actor B', presence='later', reference_id='', **kwargs):
        return {'name': name, 'presence': presence, 'reference_id': reference_id,
                'in_last_frame': False, **kwargs}

    def assert_confirmed(self, characters):
        response = self.confirm(characters)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_legacy_missing_or_incomplete_review_is_unknown_not_empty(self):
        for legacy in (None, {}, {'characters': []}, {'version': 1, 'characters': []}):
            self.record['shots'][0]['video_character_review'] = legacy
            self.record['shots'][0]['motion_plan'] = {'version': 2, 'participants': ['Actor B'],
                                                      'reference_participants': []}
            studio.save(self.path, self.record)
            with self.subTest(legacy=legacy):
                view = self.coverage()
                self.assertEqual(view['status'], 'unknown')
                self.assertEqual(view['suggested_reference_ids'], [])
        response = self.client.put(self.url + '/character-coverage', json={'revision': 1})
        self.assertEqual(response.status_code, 422)

    def test_core_identity_is_explicit_opening_only_and_expires_with_image(self):
        self.assert_confirmed([self.actor(presence='opening')])
        self.assertFalse(self.coverage()['characters'][0]['core_identity_confirmed'])
        self.assertEqual(self.confirm([self.actor(core_identity_confirmed=True)]).status_code,422)
        self.assert_confirmed([self.actor(presence='opening',core_identity_confirmed=True)])
        self.assertTrue(self.coverage()['characters'][0]['core_identity_confirmed'])
        Image.new('RGB',(18,16),'yellow').save(self.path/'core.png')
        stale=self.coverage()
        self.assertEqual(stale['status'],'stale')
        self.assertFalse(stale['characters'][0]['core_identity_confirmed'])
        self.assertFalse(stale['review']['characters'][0]['core_identity_confirmed'])

    def test_explicit_empty_confirmation_is_empty_and_bound_to_source(self):
        record = self.assert_confirmed([])
        self.assertEqual(self.coverage()['status'], 'empty')
        self.assertEqual(record['shots'][0]['video_character_review']['characters'], [])
        record['shots'][0]['video_prompt'] = 'A new actor enters.'
        studio.save(self.path, record)
        self.assertEqual(self.coverage()['status'], 'stale')

    def test_opening_is_covered_by_actual_core_and_new_ids_are_returned(self):
        record = self.assert_confirmed([self.actor('Actor A', 'opening')])
        view = self.coverage()
        self.assertEqual(view['status'], 'covered')
        self.assertEqual(view['characters'][0]['via'], 'core')
        self.assertTrue(view['characters'][0]['id'].startswith('vc_'))
        saved = record['shots'][0]['video_character_review']
        self.assertEqual(saved['core_image']['sha256'], hashlib.sha256((self.path / 'core.png').read_bytes()).hexdigest())
        self.assertEqual(view['review']['characters'], saved['characters'])

    def test_later_character_requires_bound_selected_reference_and_can_be_repaired(self):
        self.assert_confirmed([self.actor(reference_id='actor-b')])
        view = self.coverage()
        self.assertEqual(view['status'], 'partial')
        self.assertTrue(view['characters'][0]['reference_available'])
        self.assertEqual(view['suggested_reference_ids'], ['actor-b'])
        # Saving inputs must remain possible even while coverage is partial.
        self.assertEqual(self.save_selection('composition_reference').status_code, 200)
        self.assertEqual(self.coverage()['status'], 'partial')
        self.assertEqual(self.save_selection('composition_reference', reference_ids=['actor-b']).status_code, 200)
        view = self.coverage()
        self.assertEqual(view['status'], 'covered')
        self.assertEqual(view['characters'][0]['via'], 'reference')
        self.assertEqual(view['suggested_reference_ids'], [])

    def test_same_names_and_scene_metadata_never_create_implicit_bindings(self):
        self.record['references'][1].update(name='Actor B', kind='character')
        self.record['references'][2].update(name='Actor B', kind='scene')
        studio.save(self.path, self.record)
        self.assertEqual(self.save_selection('composition_reference', reference_ids=['actor-b']).status_code, 200)
        self.assert_confirmed([self.actor()])
        view = self.coverage()
        self.assertEqual(view['status'], 'partial')
        self.assertFalse(view['characters'][0]['reference_available'])
        self.assertEqual(view['suggested_reference_ids'], [])

    def test_suggestions_follow_explicit_declaration_order_and_do_not_change_selection(self):
        self.assertEqual(self.save_selection('composition_reference', reference_ids=['tail']).status_code, 200)
        self.assert_confirmed([self.actor('B', reference_id='actor-b'),
                               self.actor('A', 'opening', reference_id='actor-a'),
                               self.actor('B alternate instance', reference_id='actor-b')])
        before = self.read()['shots'][0]['video_input_selection']
        self.assertEqual(self.coverage()['suggested_reference_ids'], ['actor-b', 'actor-a'])
        self.assertEqual(self.read()['shots'][0]['video_input_selection'], before)

    def test_tail_coverage_requires_saved_tail_and_explicit_presence(self):
        self.assertEqual(self.confirm([self.actor(in_last_frame=True)]).status_code, 400)
        self.assertEqual(self.save_selection('first_last', end_frame_reference_id='tail').status_code, 200)
        self.assert_confirmed([self.actor(in_last_frame=True)])
        view = self.coverage()
        self.assertEqual(view['status'], 'covered')
        self.assertEqual(view['characters'][0]['via'], 'last_frame')
        self.assertEqual(self.read()['shots'][0]['video_character_review']['last_frame']['reference_id'], 'tail')

    def test_character_leaving_before_final_frame_is_not_covered_by_arbitrary_tail(self):
        self.assertEqual(self.save_selection('first_last', end_frame_reference_id='tail').status_code, 200)
        self.assert_confirmed([self.actor(in_last_frame=False, reference_id='actor-b')])
        view = self.coverage()
        self.assertEqual(view['status'], 'partial')
        self.assertEqual(view['characters'][0]['via'], '')
        self.assertIn('中途', view['characters'][0]['reason'])

    def test_changed_declared_tail_file_or_selected_tail_makes_review_stale(self):
        for mutation in ('file', 'selection'):
            studio.save(self.path, copy.deepcopy(self.record))
            Image.new('RGB', (18, 16), 'blue').save(self.path / 'tail.png')
            self.assertEqual(self.save_selection('first_last', end_frame_reference_id='tail').status_code, 200)
            self.assert_confirmed([self.actor(in_last_frame=True)])
            if mutation == 'file':
                Image.new('RGB', (18, 16), 'yellow').save(self.path / 'tail.png')
            else:
                self.assertEqual(self.save_selection('first_last', end_frame_reference_id='actor-b').status_code, 200)
            with self.subTest(mutation=mutation):
                self.assertEqual(self.coverage()['status'], 'stale')

    def test_changing_to_portrait_references_reuses_valid_declaration_not_old_tail(self):
        self.assertEqual(self.save_selection('first_last', end_frame_reference_id='tail').status_code, 200)
        self.assert_confirmed([self.actor(reference_id='actor-b', in_last_frame=True)])
        self.assertEqual(self.save_selection('composition_reference', reference_ids=['actor-b']).status_code, 200)
        view = self.coverage()
        self.assertEqual(view['status'], 'covered')
        self.assertEqual(view['characters'][0]['via'], 'reference')
        self.assertEqual(self.save_selection('composition_reference').status_code, 200)
        self.assertEqual(self.coverage()['status'], 'partial')
        self.assertEqual(self.coverage()['characters'][0]['via'], '')

    def test_reconfirming_unchanged_characters_after_tail_change_refreshes_evidence(self):
        self.assertEqual(self.save_selection('first_last', end_frame_reference_id='tail').status_code, 200)
        self.assert_confirmed([self.actor(in_last_frame=True)])
        declaration = self.coverage()['review']['characters']
        self.assertEqual(self.save_selection('first_last', end_frame_reference_id='actor-b').status_code, 200)
        self.assertEqual(self.coverage()['status'], 'stale')
        self.assert_confirmed(declaration)
        self.assertEqual(self.coverage()['status'], 'covered')
        self.assertEqual(self.read()['shots'][0]['video_character_review']['last_frame']['reference_id'], 'actor-b')

    def test_prompt_action_plan_duration_and_image_basis_changes_stale_review(self):
        for field, value in (
            ('action', 'The actors leave.'), ('video_prompt', 'A changed action.'),
            ('motion_plan', {'version': 2, 'participants': ['New actor']}),
            ('image_prompt', 'A different composition.'), ('duration', 3),
            ('generation_duration', 5), ('image_version', 'core-v2'), ('design_needs_review', True),
        ):
            studio.save(self.path, copy.deepcopy(self.record))
            record = self.assert_confirmed([self.actor('A', 'opening')])
            record['shots'][0][field] = value
            studio.save(self.path, record)
            with self.subTest(field=field):
                self.assertEqual(self.coverage()['status'], 'stale')

    def test_unrelated_revision_logs_generation_status_and_other_shot_do_not_stale(self):
        record = self.assert_confirmed([self.actor('A', 'opening')])
        record['revision'] += 9
        record['logs'].append('Unrelated progress.')
        record['shots'][0]['video_status'] = 'failed'
        record['shots'][1]['action'] = 'Other shot changes.'
        record['settings']['name'] = 'Renamed project'
        studio.save(self.path, record)
        self.assertEqual(self.coverage()['status'], 'covered')

    def test_changed_or_missing_bound_character_image_never_remains_covered(self):
        self.assertEqual(self.save_selection('composition_reference', reference_ids=['actor-b']).status_code, 200)
        self.assert_confirmed([self.actor(reference_id='actor-b')])
        Image.new('RGB', (18, 16), 'orange').save(self.path / 'actor-b.png')
        view = self.coverage()
        self.assertEqual(view['status'], 'stale')
        self.assertFalse(view['characters'][0]['covered'])
        self.assertFalse(view['characters'][0]['reference_available'])
        self.assertEqual(view['suggested_reference_ids'], [])
        (self.path / 'actor-b.png').unlink()
        self.assertEqual(self.coverage()['status'], 'stale')

    def test_changed_core_bytes_stale_even_without_version_change(self):
        self.assert_confirmed([self.actor('A', 'opening')])
        Image.new('RGB', (18, 16), 'black').save(self.path / 'core.png')
        self.assertEqual(self.coverage()['status'], 'stale')

    def test_invalid_foreign_or_ambiguous_asset_is_rejected_without_mutation(self):
        (self.root / 'outside.png').write_bytes((self.path / 'core.png').read_bytes())
        cases = [
            ('foreign-id', []),
            ('outside', [{'id': 'outside', 'file': '../../outside.png'}]),
            ('actor-a', [{'id': 'actor-a', 'file': 'actor-b.png'}]),
            ('bad', [{'id': 'bad', 'file': 'not-an-image.txt'}]),
        ]
        (self.path / 'not-an-image.txt').write_text('not an image')
        for identity, added in cases:
            record = copy.deepcopy(self.record)
            record['references'].extend(added)
            studio.save(self.path, record)
            with self.subTest(identity=identity):
                self.assertEqual(self.confirm([self.actor(reference_id=identity)]).status_code, 400)
                self.assertEqual(self.read()['revision'], 1)
                self.assertNotIn('video_character_review', self.read()['shots'][0])

    def test_review_schema_and_edit_guards(self):
        for characters in ([self.actor()] * 17,
                           [self.actor(id='same'), self.actor(id='same')],
                           [self.actor(name=' ')], [self.actor(presence='unknown')],
                           [self.actor(in_last_frame='false')]):
            with self.subTest(characters=characters):
                self.assertEqual(self.confirm(characters).status_code, 422)
        self.assertEqual(self.confirm([], revision=0).status_code, 409)
        with patch.object(studio, 'require_user', return_value={'id': 2}):
            self.assertEqual(self.client.get(self.url + '/video-inputs').status_code, 404)
            self.assertEqual(self.client.put(self.url + '/character-coverage', json={'revision': 1, 'characters': []}).status_code, 404)
        self.record['shots'][0]['kind'] = 'static'
        studio.save(self.path, self.record)
        self.assertEqual(self.confirm([]).status_code, 400)
        self.record['shots'][0]['kind'] = 'video'
        self.record['shots'][0]['image_status'] = 'pending'
        studio.save(self.path, self.record)
        self.assertEqual(self.confirm([]).status_code, 400)

    def test_confirmation_does_not_invalidate_existing_clip_cache_or_other_shot(self):
        self.record['status'] = 'completed'
        self.record['shots'][0].update(video='old.mp4', video_status='completed', video_task_id='existing-id',
                                        video_request={'backend': 'comfyui'}, h3_prompt_source='cached-source')
        studio.save(self.path, self.record)
        saved = self.assert_confirmed([self.actor('A', 'opening')])
        self.assertEqual(saved['status'], 'completed')
        self.assertEqual(saved['shots'][1], self.record['shots'][1])
        before = self.record['shots'][0]
        self.assertEqual({key: saved['shots'][0][key] for key in before}, before)
        self.assertNotIn('video_input_selection', saved['shots'][0])

    def test_manual_prompt_warning_depends_on_agent_setting(self):
        self.assertTrue(self.coverage()['manual_prompt_warning'])
        self.record['creation_parameters']['comfyui_h3_prompt_agent'] = True
        studio.save(self.path, self.record)
        self.assertEqual(self.coverage()['manual_prompt_warning'], '')

    def test_current_partial_blocks_actual_new_generation_before_worker_or_freeze(self):
        record = self.assert_confirmed([self.actor()])
        record['status'] = 'video_generation_ready'
        studio.save(self.path, record)
        with patch.object(generation, '_start_local_worker') as worker:
            response = self.client.post(self.base + '/videos/generate', json={
                'revision': record['revision'], 'shot_ids': ['shot-A']})
            self.assertEqual(response.status_code, 400, response.text)
            worker.assert_not_called()
        self.assertNotIn('video_request', self.read()['shots'][0])

    def test_unknown_keeps_legacy_generation_available(self):
        self.record['status'] = 'video_generation_ready'
        studio.save(self.path, self.record)
        self.assertEqual(self.coverage()['status'], 'unknown')
        with patch.object(generation, '_start_local_worker') as worker:
            response = self.client.post(self.base + '/videos/generate', json={
                'revision': self.record['revision'], 'shot_ids': ['shot-A']})
            self.assertEqual(response.status_code, 200, response.text)
            worker.assert_called_once()

    def test_stale_source_or_unselected_bound_image_blocks_new_generation(self):
        for change in ('source', 'missing_reference', 'changed_reference'):
            studio.save(self.path, copy.deepcopy(self.record))
            Image.new('RGB', (18, 16), 'green').save(self.path / 'actor-b.png')
            record = self.assert_confirmed([self.actor(reference_id='actor-b')])
            record['status'] = 'video_generation_ready'
            if change == 'source':
                record['shots'][0]['action'] = 'Changed approved action.'
            elif change == 'missing_reference':
                (self.path / 'actor-b.png').unlink()
            else:
                Image.new('RGB', (18, 16), 'yellow').save(self.path / 'actor-b.png')
            studio.save(self.path, record)
            self.assertEqual(self.coverage()['status'], 'stale')
            with self.subTest(change=change), patch.object(generation, '_start_local_worker') as worker:
                response = self.client.post(self.base + '/videos/generate', json={
                    'revision': record['revision'], 'shot_ids': ['shot-A']})
                self.assertEqual(response.status_code, 400, response.text)
                worker.assert_not_called()
            self.assertNotIn('video_request', self.read()['shots'][0])

    def test_partial_review_does_not_block_retrieval_of_an_existing_task(self):
        record = self.assert_confirmed([self.actor()])
        record['status'] = 'video_review'
        shot = record['shots'][0]
        generation._freeze_local_request(self.path, record, shot, 'single_first')
        shot.update(video_status='failed', video_task_id='already-accepted-task')
        frozen = copy.deepcopy(shot['video_request'])
        studio.save(self.path, record)

        def retrieve(*args, **kwargs):
            self.assertEqual(kwargs['existing_prompt_id'], 'already-accepted-task')
            kwargs['output_path'].write_bytes(b'recovered media')

        with patch.object(generation.threading, 'Thread') as thread, \
                patch.object(generation, 'video_task_state', return_value='completed'), \
                patch.object(generation, 'run_video_profile', side_effect=retrieve) as bridge:
            response = self.client.post(self.base + '/videos/generate', json={
                'revision': record['revision'], 'shot_ids': ['shot-A']})
            self.assertEqual(response.status_code, 200, response.text)
            thread.call_args.kwargs['target']()
            bridge.assert_called_once()
        current = self.read()['shots'][0]
        self.assertEqual(current['video_status'], 'completed')
        self.assertEqual(current['video_attempt'], 1)
        self.assertEqual(current['video_request'], frozen)

    def test_missing_existing_task_must_pass_coverage_before_replacement(self):
        record = self.assert_confirmed([self.actor()])
        record['status'] = 'video_review'
        shot = record['shots'][0]
        generation._freeze_local_request(self.path, record, shot, 'single_first')
        shot.update(video_status='failed', video_task_id='missing-task')
        studio.save(self.path, record)
        with patch.object(generation.threading, 'Thread') as thread, \
                patch.object(generation, 'video_task_state', return_value='missing'), \
                patch.object(generation, 'run_video_profile') as bridge:
            response = self.client.post(self.base + '/videos/generate', json={
                'revision': record['revision'], 'shot_ids': ['shot-A']})
            self.assertEqual(response.status_code, 200, response.text)
            thread.call_args.kwargs['target']()
            bridge.assert_not_called()
        current = self.read()['shots'][0]
        self.assertEqual(current['video_status'], 'failed')
        self.assertEqual(current['video_attempt'], 1)
        self.assertIn('角色输入尚未覆盖', current['video_error'])

    def test_changed_or_missing_review_dependency_still_allows_existing_task_recovery(self):
        for change in ('changed', 'missing'):
            studio.save(self.path, copy.deepcopy(self.record))
            Image.new('RGB', (18, 16), 'green').save(self.path / 'actor-b.png')
            record = self.assert_confirmed([self.actor(reference_id='actor-b')])
            record['status'] = 'video_review'
            shot = record['shots'][0]
            # Use distinct historical attempt numbers without replacing prior evidence.
            shot['video_attempt'] = 0 if change == 'changed' else 1
            generation._freeze_local_request(self.path, record, shot, 'single_first')
            attempt = shot['video_attempt']
            shot.update(video_status='failed', video_task_id='existing-' + change)
            if change == 'missing':
                (self.path / 'actor-b.png').unlink()
            else:
                Image.new('RGB', (18, 16), 'yellow').save(self.path / 'actor-b.png')
            studio.save(self.path, record)
            self.assertEqual(self.coverage()['status'], 'stale')

            def retrieve(*args, **kwargs):
                self.assertEqual(kwargs['existing_prompt_id'], 'existing-' + change)
                kwargs['output_path'].write_bytes(b'recovered media')

            with self.subTest(change=change), patch.object(generation.threading, 'Thread') as thread, \
                    patch.object(generation, 'video_task_state', return_value='completed'), \
                    patch.object(generation, 'run_video_profile', side_effect=retrieve) as bridge:
                response = self.client.post(self.base + '/videos/generate', json={
                    'revision': record['revision'], 'shot_ids': ['shot-A']})
                self.assertEqual(response.status_code, 200, response.text)
                thread.call_args.kwargs['target']()
                bridge.assert_called_once()
            current = self.read()['shots'][0]
            self.assertEqual(current['video_status'], 'completed')
            self.assertEqual(current['video_attempt'], attempt)


if __name__ == '__main__':
    unittest.main()
