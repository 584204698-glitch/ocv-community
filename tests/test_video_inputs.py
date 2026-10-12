import copy
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from backend.app import video_inputs as inputs, video_studio as studio, video_generation as generation


def h3_prompt(mode='single_first'):
    body = ('[Shot 1] Keep the illustrated clothing and room from <Picture 1>. '
            'A lowers a paper onto the table during four seconds. The camera stays fixed. '
            'A brief paper rustle coincides with contact.')
    if mode == 'composition_reference':
        visual = ('subject_definitions: <Picture 1> is the composition anchor. '
                  '<Subject 1> uses <Picture 2>; <Subject 2> uses <Picture 3>.\n\n'
                  'summary: [reference generation] A lowers a paper.\n\n'
                  'retention_analysis: Preserve the room and character appearances.\n\n'
                  'detailed_description: ' + body)
    else:
        visual = 'integrated_multimodal_description: ' + body
    return visual + '\n\noverall_soundscape: A brief paper rustle.\n\nnon_diegetic_music: N/A'


def profile(mode, extras=0):
    graph = {'1': {'class_type': 'LoadImage', 'inputs': {'image': 'old.jpg'}},
             '9': {'class_type': 'Text', 'inputs': {'text': ''}}}
    bindings = {'image': {'node_id': '1', 'input_name': 'image'}, 'prompt': {'node_id': '9', 'input_name': 'text'}}
    media = {'first_frame': ['1', 0]}
    if mode == 'first_last':
        graph['2'] = {'class_type': 'LoadImage', 'inputs': {'image': 'old-tail.jpg'}}
        bindings['last_image'] = {'node_id': '2', 'input_name': 'image'}
        media['last_frame'] = ['2', 0]
    elif mode == 'composition_reference':
        media = {'ref_images.ref_image_0': ['1', 0]}
        bindings['reference_images'] = []
        for index in range(extras):
            node = str(index + 2)
            graph[node] = {'class_type': 'LoadImage', 'inputs': {'image': f'old-{node}.jpg'}}
            bindings['reference_images'].append({'node_id': node, 'input_name': 'image'})
            media[f'ref_images.ref_image_{index+1}'] = [node, 0]
    graph['h3'] = {'class_type': 'MiniMaxH3ReferenceToVideo' if mode == 'composition_reference' else 'MiniMaxH3ImageToVideo', 'inputs': media}
    return {'id': mode, 'name': mode, 'kind': 'video', 'video_input_mode': mode, 'workflow': graph, 'mappings': bindings}


class VideoInputTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.identity = 'a' * 32
        self.path = self.root / '1' / self.identity
        self.path.mkdir(parents=True)
        for name, color in [('core', 'white'), ('tail', 'blue'), ('character', 'red')]:
            Image.new('RGB', (16, 16), color).save(self.path / f'{name}.png')
        self.record = {'id': self.identity, 'status': 'image_review', 'revision': 1,
                       'settings': {'name': 'Synthetic project', 'ratio': '16:9'}, 'logs': [],
                       'creation_parameters': {'video_generation_backend': 'comfyui', 'comfyui_profile_id': 'single_first'},
                       'references': [{'id': name, 'name': name, 'file': name + '.png'} for name in ['tail', 'character']],
                       'shots': [{'id': 'shot-A', 'kind': 'video', 'image': 'core.png', 'image_status': 'completed',
                                  'duration': 4, 'generation_duration': 4, 'video_prompt': h3_prompt()},
                                 {'id': 'shot-B', 'kind': 'video', 'image': 'core.png', 'image_status': 'completed',
                                  'video_status': 'pending', 'duration': 4, 'generation_duration': 4, 'video_prompt': 'B waits.'}]}
        studio.save(self.path, self.record)
        self.profiles = [profile('single_first'), profile('first_last'), profile('composition_reference', 2)]
        for target, name, value in [(studio, 'ROOT', self.root), (studio, 'require_user', lambda request: {'id': 1}),
                                    (generation, '_connection', lambda owner: {'base_url': 'http://127.0.0.1:8188', 'prompt_path': '/prompt'}),
                                    (inputs, '_profiles', lambda owner: self.profiles),
                                    (inputs, 'video_profile', lambda owner, identity: next(p for p in self.profiles if p['id'] == identity))]:
            mock = patch.object(target, name, value)
            mock.start()
            self.addCleanup(mock.stop)
        app = FastAPI()
        app.include_router(inputs.router)
        app.include_router(generation.router)
        self.client = TestClient(app)
        self.base = f'/api/video-studio/{self.identity}'

    def selection(self, mode, **kwargs):
        return {'revision': 1, 'input_mode': mode, 'profile_id': mode,
                'end_frame_reference_id': '', 'reference_ids': [], **kwargs}

    def freeze_for_history(self, backend, shot):
        if backend == 'comfyui':
            generation._freeze_local_request(self.path, self.record, shot, 'single_first')
        else:
            generation._freeze_request(self.path, self.record, shot, {
                'resolution': '720p', 'base_url': 'https://synthetic.invalid',
                'submit_path': '/submit', 'api_key': 'synthetic-test-key',
            })

    def test_invalidate_then_freeze_archives_original_attempt_exactly_once(self):
        for backend in ('comfyui', 'api'):
            with self.subTest(backend=backend):
                shot = copy.deepcopy(self.record['shots'][0])
                shot['id'] = backend
                self.freeze_for_history(backend, shot)
                original_video = shot['video']
                (self.path / original_video).write_bytes(b'original synthetic clip')
                shot.update(video_status='completed', video_task_id='original-task',
                            video_version='original-version')
                original_request = copy.deepcopy(shot['video_request'])

                self.assertTrue(studio._invalidate_shot_video(self.record, shot, 'changed inputs'))
                archived = copy.deepcopy(shot['video_history'])
                self.freeze_for_history(backend, shot)

                self.assertEqual(shot['video_attempt'], 2)
                self.assertEqual(shot['video_history'], archived)
                self.assertEqual(len(archived), 1)
                self.assertEqual(archived[0]['video'], original_video)
                self.assertEqual(archived[0]['video_request'], original_request)
                self.assertEqual(archived[0]['video_task_id'], 'original-task')
                self.assertEqual((self.path / original_video).read_bytes(), b'original synthetic clip')
                self.assertTrue((self.path / shot['video']).parent.joinpath('request.json').is_file())

    def test_freeze_preserves_failed_attempt_evidence_without_requiring_media(self):
        evidence = {'video_request': {'reference_files': ['lost-reference.jpg']},
                    'video_task_id': 'paid-task', 'video_state_file': 'lost-task.json',
                    'video': 'lost-clip.mp4'}
        for backend in ('comfyui', 'api'):
            for key, value in evidence.items():
                with self.subTest(backend=backend, evidence=key):
                    shot = copy.deepcopy(self.record['shots'][0])
                    shot.update(id=f'{backend}-{key}', video_attempt=4, video_status='failed',
                                video_error='previous failure', **{key: copy.deepcopy(value)})
                    previous_value = shot[key]
                    self.freeze_for_history(backend, shot)
                    self.assertEqual(shot['video_attempt'], 5)
                    self.assertEqual(len(shot['video_history']), 1)
                    archived = shot['video_history'][0]
                    self.assertEqual(archived['video_attempt'], 4)
                    self.assertEqual(archived[key], value)
                    self.assertEqual(archived['video_error'], 'previous failure')
                    if key == 'video_request':
                        previous_value['reference_files'].append('later-mutation.jpg')
                        self.assertEqual(archived[key], value)

    def test_freeze_does_not_archive_attempt_counter_or_error_without_evidence(self):
        for backend in ('comfyui', 'api'):
            with self.subTest(backend=backend):
                shot = copy.deepcopy(self.record['shots'][0])
                shot.update(id=backend, video_attempt=4, video_status='failed',
                            video_error='preparation failed')
                self.freeze_for_history(backend, shot)
                self.assertEqual(shot['video_attempt'], 5)
                self.assertEqual(shot.get('video_history', []), [])

    def test_save_tail_invalidates_only_this_shot_and_preserves_history(self):
        self.record['shots'][0].update(video='old-A.mp4', video_status='completed', video_version='old')
        studio.save(self.path, self.record)
        response = self.client.put(self.base + '/shots/shot-A/video-inputs', json=self.selection('first_last', end_frame_reference_id='tail'))
        self.assertEqual(response.status_code, 200, response.text)
        record = response.json()
        self.assertEqual(record['shots'][1], self.record['shots'][1])
        self.assertEqual(record['shots'][0]['video_history'][0]['video'], 'old-A.mp4')
        self.assertEqual(record['shots'][0]['video_status'], 'pending')
        self.assertEqual(record['shots'][0]['image'], 'core.png')

    def test_missing_tail_duplicate_references_and_stale_revision_do_not_mutate(self):
        for payload, status in [(self.selection('first_last'), 400),
                                (self.selection('composition_reference', reference_ids=['character', 'character']), 422),
                                (self.selection('first_last', end_frame_reference_id='tail', revision=0), 409),
                                (self.selection('first_last', end_frame_reference_id='outside'), 400)]:
            with self.subTest(payload=payload):
                response = self.client.put(self.base + '/shots/shot-A/video-inputs', json=payload)
                self.assertEqual(response.status_code, status, response.text)
                self.assertEqual(studio.read(self.path)['revision'], 1)

    def test_account_ownership_controls_both_input_and_media_access(self):
        with patch.object(studio, 'require_user', return_value={'id': 2}):
            self.assertEqual(self.client.get(self.base + '/shots/shot-A/video-inputs').status_code, 404)
            self.assertEqual(self.client.get(self.base + '/video-input-assets/character').status_code, 404)

    def test_upload_is_only_a_candidate_until_explicit_adoption(self):
        response = self.client.post(self.base + '/video-input-assets', data={'revision': '1'},
                                    files={'file': ('tail.png', (self.path / 'tail.png').read_bytes(), 'image/png')})
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertNotIn('video_input_selection', payload['project']['shots'][0])
        self.assertEqual(self.client.get(payload['asset']['url']).status_code, 200)
        adopt = self.client.put(self.base + '/shots/shot-A/video-inputs', json=self.selection('first_last', revision=2, end_frame_reference_id=payload['asset']['id']))
        self.assertEqual(adopt.status_code, 200, adopt.text)

    def test_first_last_snapshot_freezes_both_images_and_their_content_hashes(self):
        shot = self.record['shots'][0]
        shot['video_input_selection'] = self.selection('first_last', end_frame_reference_id='tail')
        shot['video_input_selection'].pop('revision')
        generation._freeze_local_request(self.path, self.record, shot, 'first_last')
        saved = shot['video_request']
        self.assertEqual(saved['input_mode'], 'first_last')
        self.assertEqual([row['role'] for row in saved['input_manifest']], ['first_frame', 'last_frame'])
        self.assertEqual(len(saved['reference_files']), 1)
        for row in saved['input_manifest']:
            self.assertEqual(hashlib.sha256((self.path / row['file']).read_bytes()).hexdigest(), row['sha256'])
        before = (self.path / saved['last_image_file']).read_bytes()
        Image.new('RGB', (16, 16), 'green').save(self.path / 'tail.png')
        self.assertEqual((self.path / saved['last_image_file']).read_bytes(), before)

    def test_reference_snapshot_preserves_explicit_order_and_never_replaces_core(self):
        shot = self.record['shots'][0]
        shot['video_input_selection'] = {key: value for key, value in self.selection('composition_reference', reference_ids=['character', 'tail']).items() if key != 'revision'}
        generation._freeze_local_request(self.path, self.record, shot, 'composition_reference')
        manifest = shot['video_request']['input_manifest']
        self.assertEqual([row['id'] for row in manifest], ['shot-A', 'character', 'tail'])
        self.assertEqual([row['label'] for row in manifest], ['<Picture 1>', '<Picture 2>', '<Picture 3>'])
        self.assertEqual(shot['image'], 'core.png')

    def test_frozen_jpeg_materializes_exif_orientation(self):
        source = Image.new('RGB', (24, 12), 'blue')
        orientation = source.getexif()
        orientation[274] = 6
        source.save(self.path / 'rotated.jpg', exif=orientation)
        shot = self.record['shots'][0]
        shot['image'] = 'rotated.jpg'
        generation._freeze_local_request(self.path, self.record, shot, 'single_first')
        with Image.open(self.path / shot['video_request']['reference_files'][0]) as frozen:
            self.assertEqual(frozen.size, (12, 24))
            self.assertNotIn(274, frozen.getexif())
        with Image.open(self.path / 'rotated.jpg') as original:
            self.assertEqual(original.size, (24, 12))
            self.assertEqual(original.getexif()[274], 6)

    def test_generation_preflight_rejects_unbound_tail_before_worker_or_gpu(self):
        self.record['status'] = 'video_generation_ready'
        self.record['shots'][0]['video_input_selection'] = {'input_mode': 'first_last', 'profile_id': 'single_first', 'end_frame_reference_id': 'tail'}
        studio.save(self.path, self.record)
        with patch.object(generation, '_start_local_worker') as worker:
            response = self.client.post(self.base + '/videos/generate', json={'revision': 1, 'shot_ids': ['shot-A']})
            self.assertEqual(response.status_code, 400, response.text)
            worker.assert_not_called()
        self.assertEqual(studio.read(self.path)['status'], 'video_generation_ready')

    def test_generation_queues_only_selected_shot_with_its_own_profile(self):
        self.record['status'] = 'video_generation_ready'
        self.record['shots'][0]['video_input_selection'] = {'input_mode': 'first_last', 'profile_id': 'first_last', 'end_frame_reference_id': 'tail'}
        studio.save(self.path, self.record)
        with patch.object(generation, '_start_local_worker') as worker:
            response = self.client.post(self.base + '/videos/generate', json={'revision': 1, 'shot_ids': ['shot-A']})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(worker.call_args.args[2], ['shot-A'])
            self.assertEqual(response.json()['shots'][1], self.record['shots'][1])

    def test_worker_passes_frozen_tail_and_selected_profile_to_the_bridge(self):
        self.record['status'] = 'video_generation_ready'
        self.record['shots'][0]['video_input_selection'] = {'input_mode': 'first_last', 'profile_id': 'first_last', 'end_frame_reference_id': 'tail'}
        studio.save(self.path, self.record)
        def finish(*args, **kwargs):
            kwargs['output_path'].write_bytes(b'synthetic media output')
            kwargs['on_submitted']('synthetic-prompt-id')
        with patch.object(generation.threading, 'Thread') as thread, patch.object(generation, 'run_video_profile', side_effect=finish) as bridge:
            response = self.client.post(self.base + '/videos/generate', json={'revision': 1, 'shot_ids': ['shot-A']})
            self.assertEqual(response.status_code, 200, response.text)
            thread.call_args.kwargs['target']()
            self.assertEqual(bridge.call_count, 1)
            args, kwargs = bridge.call_args
            self.assertEqual(args[1], 'first_last')
            self.assertEqual(kwargs['input_mode'], 'first_last')
            self.assertTrue(kwargs['last_image_path'].is_file())
            self.assertEqual(kwargs['reference_image_paths'], [])
        record = studio.read(self.path)
        self.assertEqual(record['shots'][0]['video_status'], 'completed', record['logs'])
        self.assertEqual(record['shots'][1], self.record['shots'][1])

    def test_worker_passes_the_same_order_to_h3_vision_and_frozen_video_inputs(self):
        self.record['status'] = 'video_generation_ready'
        self.record['creation_parameters']['comfyui_h3_prompt_agent'] = True
        self.record['shots'][0]['video_input_selection'] = {'input_mode': 'composition_reference', 'profile_id': 'composition_reference', 'reference_ids': ['character', 'tail']}
        studio.save(self.path, self.record)
        def finish(*args, **kwargs):
            kwargs['output_path'].write_bytes(b'synthetic media output')
        with patch.object(generation.threading, 'Thread') as thread, patch.object(generation, 'run_video_profile', side_effect=finish) as bridge, patch.object(generation, 'convert_for_h3', return_value=(h3_prompt('composition_reference'), 'source-hash')) as convert:
            response = self.client.post(self.base + '/videos/generate', json={'revision': 1, 'shot_ids': ['shot-A']})
            self.assertEqual(response.status_code, 200, response.text)
            thread.call_args.kwargs['target']()
            self.assertEqual(convert.call_count, 1)
            self.assertEqual([row['id'] for row in convert.call_args.kwargs['reference_images']], ['character', 'tail'])
            self.assertEqual(bridge.call_count, 1)
            kwargs = bridge.call_args.kwargs
            self.assertEqual(kwargs['input_mode'], 'composition_reference')
            self.assertEqual(len(kwargs['reference_image_paths']), 2)
        record = studio.read(self.path)
        self.assertEqual(record['shots'][0]['video_status'], 'completed', record['logs'])
        self.assertEqual(record['shots'][0]['video_request']['prompt_format'], 'h3_composition_reference')

    def test_manual_h3_freeze_preserves_effects_and_records_prompt_only_policy(self):
        shot = self.record['shots'][0]
        shot['video_prompt'] = h3_prompt().replace('non_diegetic_music: N/A',
                                                'non_diegetic_music: Soft piano score.')
        generation._freeze_local_request(self.path, self.record, shot, 'single_first',
                                         prompt_format='h3_single_first', h3_effects_only=True)
        saved = shot['video_request']
        self.assertIn('A brief paper rustle.', saved['prompt'])
        self.assertTrue(saved['prompt'].endswith('non_diegetic_music: N/A'))
        self.assertNotIn('Soft piano score', saved['prompt'])
        self.assertEqual(saved['source_prompt'], shot['video_prompt'])
        self.assertEqual(saved['audio_policy'], {'mode': 'effects_only', 'music': 'N/A',
                                                'enforcement': 'prompt', 'version': 1})
        request_file = self.path / shot['video']
        self.assertEqual(json.loads((request_file.parent / 'request.json').read_text(encoding='utf-8')), saved)

    def test_manual_h3_bad_structure_stops_before_attempt_or_gpu_submission(self):
        self.record['status'] = 'video_generation_ready'
        self.record['shots'][0]['video_prompt'] = 'A moves. Keep natural sound.'
        studio.save(self.path, self.record)
        with patch.object(generation.threading, 'Thread') as thread, \
                patch.object(generation, 'run_video_profile') as bridge:
            response = self.client.post(self.base + '/videos/generate',
                                        json={'revision': 1, 'shot_ids': ['shot-A']})
            self.assertEqual(response.status_code, 200, response.text)
            thread.call_args.kwargs['target']()
            bridge.assert_not_called()
        shot = studio.read(self.path)['shots'][0]
        self.assertEqual(shot['video_status'], 'failed')
        self.assertFalse(shot.get('video_attempt'))
        self.assertFalse((self.path / 'assets/videos/shot-A').exists())

    def test_manual_h3_does_not_receive_stale_tts_reference_option(self):
        self.record['status'] = 'video_generation_ready'
        self.record['creation_parameters']['comfyui_reference_audio'] = True
        studio.save(self.path, self.record)
        def finish(*args, **kwargs):
            kwargs['output_path'].write_bytes(b'synthetic output')
        with patch.object(generation.threading, 'Thread') as thread, \
                patch.object(generation, 'run_video_profile', side_effect=finish) as bridge:
            response = self.client.post(self.base + '/videos/generate',
                                        json={'revision': 1, 'shot_ids': ['shot-A']})
            self.assertEqual(response.status_code, 200, response.text)
            thread.call_args.kwargs['target']()
            self.assertEqual(bridge.call_count, 1)
            self.assertIsNone(bridge.call_args.kwargs['audio_path'])
        saved = studio.read(self.path)['shots'][0]['video_request']
        self.assertEqual(saved['audio_policy']['enforcement'], 'prompt')
        self.assertNotIn('reference_audio_file', saved)

    def test_confirmed_characters_use_actual_picture_order_in_prompt_and_snapshot(self):
        shot = self.record['shots'][0]
        shot['video_input_selection'] = {'input_mode': 'composition_reference',
            'profile_id': 'composition_reference', 'reference_ids': ['tail', 'character']}
        studio.save(self.path, self.record)
        response = self.client.put(self.base + '/shots/shot-A/character-coverage', json={
            'revision': 1, 'characters': [
                {'name': 'Opening character', 'presence': 'opening', 'reference_id': 'character'},
                {'name': 'Later character', 'presence': 'later', 'reference_id': 'tail'},
            ],
        })
        self.assertEqual(response.status_code, 200, response.text)
        record = response.json()
        shot = record['shots'][0]
        selection, images = inputs.resolve_video_inputs(self.path, record, shot)
        context = generation._confirmed_character_inputs(1, self.path, record, shot, selection, images)
        self.assertEqual(context['status'], 'covered')
        self.assertEqual(context['characters'][0]['picture_labels'], ['<Picture 1>', '<Picture 3>'])
        self.assertEqual(context['characters'][1]['picture_labels'], ['<Picture 2>'])
        generation._freeze_local_request(self.path, record, shot, 'composition_reference',
                                         character_inputs=context)
        context['characters'][0]['name'] = 'Unadopted edit'
        self.assertEqual(shot['video_request']['character_inputs']['characters'][0]['name'], 'Opening character')
        shot['action'] = 'Changed action after confirmation'
        stale = generation._confirmed_character_inputs(1, self.path, record, shot, selection, images)
        self.assertEqual(stale['status'], 'stale')
        self.assertEqual(stale['characters'], [])

    def test_recovery_uses_frozen_request_after_original_media_and_profile_are_removed(self):
        self.record['status'] = 'video_review'
        self.record['creation_parameters']['comfyui_h3_prompt_agent'] = True
        shot = self.record['shots'][0]
        shot['video_input_selection'] = {'input_mode': 'first_last', 'profile_id': 'first_last', 'end_frame_reference_id': 'tail'}
        generation._freeze_local_request(self.path, self.record, shot, 'first_last', output_node_id='saved-output')
        saved = copy.deepcopy(shot['video_request'])
        shot.update(video_status='failed', video_task_id='existing-task-id')
        studio.save(self.path, self.record)
        self.profiles.clear()
        for name in ('core.png', 'tail.png'):
            (self.path / name).unlink()

        def recover(*args, **kwargs):
            self.assertEqual(kwargs['existing_prompt_id'], 'existing-task-id')
            self.assertEqual(kwargs['existing_output_node_id'], 'saved-output')
            self.assertEqual(kwargs['input_mode'], 'first_last')
            self.assertTrue(kwargs['last_image_path'].is_file())
            kwargs['output_path'].write_bytes(b'recovered synthetic output')

        with patch.object(generation.threading, 'Thread') as thread, \
                patch.object(generation, 'video_task_state', return_value='completed'), \
                patch.object(generation, 'convert_for_h3') as convert, \
                patch.object(generation, 'run_video_profile', side_effect=recover) as bridge:
            response = self.client.post(self.base + '/videos/generate', json={'revision': 1, 'shot_ids': ['shot-A']})
            self.assertEqual(response.status_code, 200, response.text)
            thread.call_args.kwargs['target']()
            convert.assert_not_called()
            self.assertEqual(bridge.call_count, 1)
        recovered = studio.read(self.path)
        self.assertEqual(recovered['shots'][0]['video_status'], 'completed', recovered['logs'])
        self.assertEqual(recovered['shots'][0]['video_attempt'], 1)
        self.assertEqual(recovered['shots'][0]['video_request'], saved)
        self.assertEqual(recovered['shots'][1], self.record['shots'][1])

    def test_missing_recovered_task_still_validates_inputs_before_new_submission(self):
        self.record['status'] = 'video_review'
        shot = self.record['shots'][0]
        generation._freeze_local_request(self.path, self.record, shot, 'single_first')
        shot.update(video_status='failed', video_task_id='missing-task-id')
        studio.save(self.path, self.record)
        (self.path / 'core.png').unlink()
        with patch.object(generation.threading, 'Thread') as thread, \
                patch.object(generation, 'video_task_state', return_value='missing'), \
                patch.object(generation, 'run_video_profile') as bridge:
            response = self.client.post(self.base + '/videos/generate', json={'revision': 1, 'shot_ids': ['shot-A']})
            self.assertEqual(response.status_code, 200, response.text)
            thread.call_args.kwargs['target']()
            bridge.assert_not_called()
        failed = studio.read(self.path)
        self.assertEqual(failed['shots'][0]['video_status'], 'failed', failed['logs'])
        self.assertEqual(failed['shots'][0]['video_attempt'], 1)
        self.assertEqual(failed['shots'][0]['video_abandoned_task_ids'], ['missing-task-id'])

    def test_guard_receipt_recovers_after_web_process_lost_submission_callback(self):
        self.record['status'] = 'video_review'
        shot = self.record['shots'][0]
        generation._freeze_local_request(self.path, self.record, shot, 'single_first', output_node_id='saved-output')
        snapshot = copy.deepcopy(shot['video_request'])
        shot.update(video_status='failed', video_task_id='')
        studio.save(self.path, self.record)
        self.profiles.clear()
        (self.path / 'core.png').unlink()

        def recover(*args, **kwargs):
            self.assertEqual(kwargs['existing_prompt_id'], 'guard-recovered-id')
            self.assertEqual(kwargs['existing_output_node_id'], 'saved-output')
            kwargs['output_path'].write_bytes(b'recovered output')

        with patch.object(generation, 'get_guard_prompt_id', return_value='guard-recovered-id') as receipt, \
                patch.object(generation.threading, 'Thread') as thread, \
                patch.object(generation, 'video_task_state', return_value='completed'), \
                patch.object(generation, 'run_video_profile', side_effect=recover) as bridge:
            response = self.client.post(self.base + '/videos/generate', json={'revision': 1, 'shot_ids': ['shot-A']})
            self.assertEqual(response.status_code, 200, response.text)
            receipt.assert_called_once_with((self.path / shot['video']).resolve().parent / 'comfyui_submission',
                                            require_resolved=True)
            thread.call_args.kwargs['target']()
            self.assertEqual(bridge.call_count, 1)
        record = studio.read(self.path)
        self.assertEqual(record['shots'][0]['video_status'], 'completed', record['logs'])
        self.assertEqual(record['shots'][0]['video_attempt'], 1)
        self.assertEqual(record['shots'][0]['video_request'], snapshot)
        self.assertEqual(record['shots'][1], self.record['shots'][1])

    def test_guard_receipt_never_revives_an_explicitly_abandoned_task(self):
        shot = self.record['shots'][0]
        generation._freeze_local_request(self.path, self.record, shot, 'single_first')
        shot['video_abandoned_task_ids'] = ['old-guard-task']
        with patch.object(generation, 'get_guard_prompt_id', return_value='old-guard-task'):
            self.assertEqual(generation._recover_local_prompt_id(self.record, shot, path=self.path), '')

    def test_unresolved_guard_receipt_blocks_before_new_attempt_is_frozen(self):
        self.record['status'] = 'video_review'
        shot = self.record['shots'][0]
        generation._freeze_local_request(self.path, self.record, shot, 'single_first')
        shot.update(video_status='failed', video_task_id='')
        studio.save(self.path, self.record)
        with patch.object(generation, 'get_guard_prompt_id', side_effect=ValueError('尚不能确认任务编号')), \
                patch.object(generation.threading, 'Thread') as thread:
            response = self.client.post(self.base + '/videos/generate', json={'revision': 1, 'shot_ids': ['shot-A']})
            self.assertEqual(response.status_code, 400, response.text)
            thread.assert_not_called()
        record = studio.read(self.path)
        self.assertEqual(record['shots'][0]['video_attempt'], 1)
        self.assertFalse((self.path / 'assets/videos/shot-A/attempt_002').exists())

    def test_changed_guard_connection_cannot_declare_original_task_missing(self):
        self.record['status'] = 'video_review'
        shot = self.record['shots'][0]
        generation._freeze_local_request(self.path, self.record, shot, 'single_first')
        shot.update(video_status='failed', video_task_id='original-task')
        studio.save(self.path, self.record)
        with patch.object(generation, 'validate_guard_connection', side_effect=ValueError('服务器与原任务不一致')), \
                patch.object(generation, 'video_task_state') as state, \
                patch.object(generation.threading, 'Thread') as thread:
            response = self.client.post(self.base + '/videos/generate', json={'revision': 1, 'shot_ids': ['shot-A']})
            self.assertEqual(response.status_code, 400, response.text)
            state.assert_not_called()
            thread.assert_not_called()
        current = studio.read(self.path)['shots'][0]
        self.assertEqual(current['video_task_id'], 'original-task')
        self.assertNotIn('video_abandoned_task_ids', current)

    def test_explicit_reroll_cannot_archive_unresolved_guard_attempt(self):
        self.record['status'] = 'video_review'
        shot = self.record['shots'][0]
        generation._freeze_local_request(self.path, self.record, shot, 'single_first')
        shot.update(video_status='failed', video_task_id='')
        studio.save(self.path, self.record)
        with patch.object(generation, '_has_guard_receipt', return_value=True), \
                patch.object(generation, 'get_guard_prompt_id', side_effect=ValueError('提交尚未确认')), \
                patch.object(studio, '_invalidate_shot_video') as invalidate:
            response = self.client.post(self.base + '/videos/generate', json={
                'revision': 1, 'shot_ids': ['shot-A'], 'regenerate_completed': True})
            self.assertEqual(response.status_code, 409, response.text)
            invalidate.assert_not_called()
        current = studio.read(self.path)['shots'][0]
        self.assertEqual(current['video_attempt'], 1)
        self.assertNotIn('video_history', current)

    def test_explicit_reroll_reconciles_remote_completed_stopped_attempt(self):
        self.record['status']='video_review'
        shot=self.record['shots'][0]
        generation._freeze_local_request(self.path,self.record,shot,'single_first')
        shot.update(video_status='stopped',video_task_id='old-task',video_resume_available=True)
        original_request=copy.deepcopy(shot['video_request'])
        studio.save(self.path,self.record)
        def retrieve(*args,**kwargs):
            self.assertEqual(kwargs['existing_prompt_id'],'old-task')
            kwargs['output_path'].write_bytes(b'paid-original-result')
        with patch.object(generation,'_has_guard_receipt',return_value=True), \
             patch.object(generation,'get_guard_prompt_id',return_value='old-task'), \
             patch.object(generation,'video_task_state',return_value='completed'), \
             patch('backend.app.comfyui_guard.assert_submission_released'), \
             patch.object(generation,'run_video_profile',side_effect=retrieve), \
             patch.object(generation,'_start_local_worker') as worker:
            response=self.client.post(self.base+'/videos/generate',json={
                'revision':1,'shot_ids':['shot-A'],'regenerate_completed':True})
        self.assertEqual(response.status_code,200,response.text)
        worker.assert_called_once()
        current=studio.read(self.path)['shots'][0]
        previous=current['video_history'][-1]
        self.assertEqual(previous['video_request'],original_request)
        self.assertEqual(previous['video_status'],'completed')
        self.assertEqual((self.path/previous['video']).read_bytes(),b'paid-original-result')
        self.assertEqual(studio.read(self.path)['shots'][1],self.record['shots'][1])

    def test_background_guard_wait_stop_remains_resumable(self):
        self.record['status'] = 'video_generation_ready'
        studio.save(self.path, self.record)

        def stop_waiting(*args, **kwargs):
            directory = kwargs['output_path'].parent / 'comfyui_submission'
            directory.mkdir()
            (directory / 'receipt.json').write_text('{}', encoding='utf-8')
            kwargs['on_submitted']('background-guard-task')
            running = studio.read(self.path)['shots'][0]
            self.assertFalse(running['video_terminal'])
            self.assertTrue(running['video_resume_available'])
            raise generation.ComfyUIStopped('已停止本地等待；后台任务仍在运行，可继续查询')

        with patch.object(generation.threading, 'Thread') as thread, \
                patch.object(generation, 'run_video_profile', side_effect=stop_waiting):
            response = self.client.post(self.base + '/videos/generate', json={'revision': 1, 'shot_ids': ['shot-A']})
            self.assertEqual(response.status_code, 200, response.text)
            thread.call_args.kwargs['target']()
        shot = studio.read(self.path)['shots'][0]
        self.assertEqual(shot['video_status'], 'stopped')
        self.assertEqual(shot['video_task_id'], 'background-guard-task')
        self.assertTrue(shot['video_resume_available'])
        self.assertFalse(shot['video_terminal'])

    def test_background_guard_wait_pauses_batch_until_same_task_can_resume(self):
        self.record['status'] = 'video_generation_ready'
        self.record['shots'][1]['video_prompt'] = h3_prompt()
        studio.save(self.path, self.record)
        frozen = {}

        def unresolved_teardown(*args, **kwargs):
            self.assertEqual(kwargs['output_path'].parent.parent.name, 'shot-A')
            directory = kwargs['output_path'].parent / 'comfyui_submission'
            directory.mkdir()
            (directory / 'receipt.json').write_text(json.dumps({
                'version': 1, 'task_id': 'c' * 32, 'graph_sha256': 'b' * 64,
                'base_url': 'http://127.0.0.1:8188', 'prompt_path': '/prompt',
                'state': 'submitted', 'prompt_id': 'original-guard-task',
            }), encoding='utf-8')
            kwargs['on_submitted']('original-guard-task')
            kwargs['output_path'].write_bytes(b'original downloaded clip')
            frozen.update(copy.deepcopy(studio.read(self.path)['shots'][0]))
            self.assertFalse(studio.CANCEL_EVENTS[str(self.path)].is_set())
            raise generation.ComfyUIStopped('Guard teardown could not yet be confirmed')

        with patch.object(generation.threading, 'Thread') as thread, \
                patch.object(generation, 'run_video_profile', side_effect=unresolved_teardown) as bridge:
            response = self.client.post(self.base + '/videos/generate', json={
                'revision': 1, 'shot_ids': ['shot-A', 'shot-B']})
            self.assertEqual(response.status_code, 200, response.text)
            thread.call_args.kwargs['target']()
            self.assertEqual(bridge.call_count, 1)

        paused = studio.read(self.path)
        first, second = paused['shots']
        self.assertEqual(first['video_status'], 'stopped')
        self.assertEqual(first['video_task_id'], 'original-guard-task')
        self.assertTrue(first['video_resume_available'])
        self.assertFalse(first['video_terminal'])
        self.assertEqual(first['video_request'], frozen['video_request'])
        self.assertEqual(first['video'], frozen['video'])
        self.assertEqual((self.path / first['video']).read_bytes(), b'original downloaded clip')
        self.assertEqual(second['video_status'], 'pending')
        self.assertNotIn('video_request', second)
        self.assertFalse((self.path / 'assets/videos/shot-B').exists())
        self.assertNotIn(str(self.path), studio.ACTIVE)
        self.assertNotIn(str(self.path), generation.LOCAL_VIDEO_QUEUES)

        visited = []

        def complete(*args, **kwargs):
            identity = kwargs['output_path'].parent.parent.name
            visited.append(identity)
            if identity == 'shot-A':
                self.assertEqual(kwargs['existing_prompt_id'], 'original-guard-task')
                self.assertEqual(kwargs['output_path'].read_bytes(), b'original downloaded clip')
            else:
                self.assertEqual(kwargs['existing_prompt_id'], '')
                kwargs['on_submitted']('next-guard-task')
                kwargs['output_path'].write_bytes(b'next completed clip')

        with patch.object(generation, 'video_task_state', return_value='unknown'), \
                patch.object(generation.threading, 'Thread') as thread, \
                patch.object(generation, 'run_video_profile', side_effect=complete):
            response = self.client.post(self.base + '/videos/generate', json={
                'revision': paused['revision'], 'shot_ids': ['shot-A', 'shot-B']})
            self.assertEqual(response.status_code, 200, response.text)
            thread.call_args.kwargs['target']()

        completed = studio.read(self.path)
        self.assertEqual(visited, ['shot-A', 'shot-B'])
        self.assertEqual([shot['video_status'] for shot in completed['shots']], ['completed', 'completed'])
        self.assertEqual(completed['shots'][0]['video_attempt'], 1)
        self.assertEqual(completed['shots'][0]['video_request'], frozen['video_request'])
        self.assertEqual(completed['shots'][0]['video_task_id'], 'original-guard-task')
        self.assertFalse((self.path / 'assets/videos/shot-A/attempt_002').exists())

    def test_web_restart_and_missing_clip_preserve_local_task_recovery(self):
        shot = self.record['shots'][0]
        shot.update(video_backend='comfyui', video_status='running', video_task_id='saved-local-task')
        generation.recover_interrupted(self.path, self.record)
        self.assertTrue(shot['video_resume_available'])
        self.assertFalse(shot['video_terminal'])
        shot.update(video_status='completed', video='assets/videos/missing.mp4')
        self.assertTrue(generation.recover_missing_clips(self.path, self.record))
        self.assertTrue(shot['video_resume_available'])
        self.assertFalse(shot['video_terminal'])

    def test_new_attempt_never_recovers_an_older_task_from_shot_logs(self):
        shot = self.record['shots'][0]
        old_id = 'abcdef12-1234-1234-1234-123456789abc'
        generation._freeze_local_request(self.path, self.record, shot, 'single_first')
        shot.update(video_task_id=old_id, video_status='completed')
        self.record['logs'].append(f'shot-A：ComfyUI 已接收任务 {old_id}，正在生成')
        studio._invalidate_shot_video(self.record, shot, 'new video inputs')
        generation._freeze_local_request(self.path, self.record, shot, 'single_first')
        self.assertEqual(generation._recover_local_prompt_id(self.record, shot), '')
        self.assertEqual(shot['video_attempt'], 2)
        # Even if older history has been pruned, a new request never uses logs.
        shot['video_history'] = []
        self.assertEqual(generation._recover_local_prompt_id(self.record, shot), '')

    def test_legacy_log_recovery_skips_archived_tasks(self):
        shot = self.record['shots'][0]
        old_id = 'abcdef12-1234-1234-1234-123456789abc'
        shot['video_request'] = {'backend': 'comfyui', 'reference_files': ['core.png']}
        self.record['logs'].append(f'shot-A：ComfyUI 已接收任务 {old_id}，正在生成')
        shot['video_history'] = [{'video_task_id': old_id}]
        self.assertEqual(generation._recover_local_prompt_id(self.record, shot), '')
        valid_id = 'abcdef12-1234-1234-1234-123456789def'
        self.record['logs'].append(f'shot-A：ComfyUI 已接收任务 {valid_id}，正在生成')
        self.assertEqual(generation._recover_local_prompt_id(self.record, shot), valid_id)
        self.assertEqual(shot['video_task_id'], valid_id)

    def test_missing_or_outside_reference_cannot_enter_frozen_request(self):
        shot = self.record['shots'][0]
        shot['video_input_selection'] = {'input_mode': 'composition_reference', 'reference_ids': ['character']}
        self.record['references'][1]['file'] = '../outside.png'
        with self.assertRaises(ValueError):
            generation._freeze_local_request(self.path, self.record, shot, 'composition_reference')
        self.assertNotIn('video_request', shot)


if __name__ == '__main__':
    unittest.main()
