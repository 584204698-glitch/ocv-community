"""Native image transport at persisted project and renderer boundaries."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException
from PIL import Image
import requests
from starlette.requests import Request

import module4_video_render as visual
from backend.app import image_profiles, image_studio, video_studio
from backend.app import openai_image_transport as transport
from tests.test_openai_image_transport import Session, Response


CONFIG = dict(protocol='openai_images', base_url='https://image.example/v1',
              model='test-image', api_key='test-private-key', ratio='16:9', resolution='1k',
              endpoint='/images/generations', reference_endpoint='/images/edits', reference_images=True)


class NativeRendererTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)
        self.output = self.path / 'image.jpg'
        self.macro = {'macro_scene_id': 'shot01', 'image_prompt': 'Opening frame', 'reference_image_paths': []}

    def test_dispatch_uses_edit_bytes_in_exact_material_order(self):
        for name, color in [('first.png', 'red'), ('third.png', 'blue')]:
            Image.new('RGB', (8, 8), color).save(self.path / name)
        macro = {**self.macro, 'reference_image_ids': ['图3', '图1'], 'image_prompt': '图3 ahead of 图1'}
        session = Session()
        with patch.object(visual, '_poster_output_path', return_value=self.output), \
                patch.object(visual, '_reference_image_catalog', return_value={
                    '图1': str(self.path / 'first.png'), '图3': str(self.path / 'third.png')}), \
                patch.object(transport.requests, 'Session', return_value=session), \
                patch.object(visual, '_submit_poster_request') as legacy:
            task = visual._submit_poster(macro, CONFIG)
        legacy.assert_not_called()
        self.assertIsNone(task.task_id)
        self.assertEqual(len(session.calls), 1)
        self.assertTrue(session.calls[0][0].endswith('/images/edits'))
        sent = session.calls[0][1]['files']
        self.assertEqual([row[1][1] for row in sent],
                         [(self.path / name).read_bytes() for name in ('third.png', 'first.png')])

    def test_native_rejects_legacy_cached_image_without_receipt(self):
        Image.new('RGB', (32, 18)).save(self.output)
        before = self.output.read_bytes()
        with patch.object(visual, '_poster_output_path', return_value=self.output), \
                patch.object(transport.requests, 'Session') as session:
            with self.assertRaises(transport.OpenAIImageError):
                visual._submit_poster(self.macro, CONFIG)
        session.assert_not_called()
        self.assertEqual(self.output.read_bytes(), before)

    def test_timeout_is_one_submission_and_blocks_later_replay(self):
        session = Session(requests.Timeout('provider timeout'))
        pool = visual.RunningHubAccountPool([CONFIG, {**CONFIG, 'api_key': 'second-key'}])
        with patch.object(visual, '_poster_output_path', return_value=self.output), \
                patch.object(transport.requests, 'Session', return_value=session):
            for _ in range(2):
                with self.assertRaises(transport.OpenAIImageError):
                    visual._render_poster_with_retry(self.macro, pool)
        self.assertEqual(len(session.calls), 1)
        receipt = json.loads(self.output.with_name('image.jpg.openai-image.json').read_text())
        self.assertEqual(receipt['status'], 'unknown')

    def test_model_change_gets_a_different_shared_pool(self):
        namespace = 'native-integration-' + self.path.name
        first = visual.shared_runninghub_account_pool([CONFIG], namespace=namespace)
        second = visual.shared_runninghub_account_pool([{**CONFIG, 'model': 'other'}], namespace=namespace)
        self.assertIsNot(first, second)

    def test_profile_environment_selects_native_and_preserves_false_reference_capability(self):
        with patch.object(image_profiles, 'profile_provider_configs', return_value=[{**CONFIG, 'query_url': '', 'reference_images': False}]):
            env = image_profiles.profile_environment({})
        with patch.dict(os.environ, env, clear=True), patch.object(visual, '_load_runninghub_env_from_file'):
            configs = visual._provider_configs()
        self.assertEqual(configs[0]['protocol'], 'openai_images')
        self.assertEqual(configs[0]['model'], CONFIG['model'])
        self.assertIs(configs[0]['reference_images'], False)

    def test_studio_native_result_is_served_and_uncertain_is_not_retried(self):
        for name, result in [('success', Response()), ('timeout', requests.Timeout())]:
            with self.subTest(name=name):
                path = self.path / name
                path.mkdir()
                record = dict(id=name, prompt='A frame', references=[], status='running')
                session = Session(result)
                with patch.object(transport.requests, 'Session', return_value=session):
                    image_studio.ACTIVE.add(123)
                    image_studio.execute(123, path, record, CONFIG)
                self.assertNotIn(123, image_studio.ACTIVE)
                saved = json.loads((path / 'record.json').read_text(encoding='utf-8'))
                self.assertNotIn(CONFIG['api_key'], json.dumps(saved))
                self.assertEqual(len(session.calls), 1)
                if name == 'success':
                    self.assertEqual(saved['status'], 'completed')
                    self.assertEqual((saved['width'], saved['height']), (32, 18))
                    self.assertTrue((path / 'result.png').is_file())
                else:
                    self.assertEqual(saved['status'], 'failed')
                    with patch.object(transport.requests, 'Session', return_value=session):
                        image_studio.execute(123, path, record, CONFIG)
                    self.assertEqual(len(session.calls), 1)


class ProjectImageSelectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.identity = 'a' * 32
        for name, value in [('ROOT', self.root), ('require_user', lambda _: {'id': 1}), ('IMAGE_EDITS', set())]:
            handle = patch.object(video_studio, name, value)
            handle.start()
            self.addCleanup(handle.stop)
        self.path = video_studio.directory(1, self.identity)
        self.record = dict(id=self.identity, revision=5, status='storyboard_review', logs=[], error='',
            settings=dict(name='Test', style='', characters='', world='', ratio='16:9', dynamic_text_mode='visual_first'),
            creation_parameters=dict(dynamic_text_mode='visual_first', scene_references_enabled=False,
                                     image_profile_id='old', image_profile_snapshot={'id': 'old', 'model_id': 'old-model'}),
            shots=[dict(id='shot01', image='first.jpg', video='clip.mp4', image_status='completed', video_status='completed')],
            scenes=[], context={'summary': 'Keep the approved storyboard'}, references=[])
        video_studio.save(self.path, self.record)

    def edit(self, **parameters):
        data = video_studio.ProjectSettingsEdit(revision=5, name='Test', scene_references_enabled=False, parameters=parameters)
        return video_studio.edit_project_settings(self.identity, data, Request({'type': 'http', 'headers': []}))

    def test_switch_model_updates_snapshot_and_preserves_planning_and_media(self):
        snapshot = {'id': 'new', 'model_id': 'new-model', 'resolution': '1k'}
        with patch.object(image_profiles, 'profile_snapshot', return_value=snapshot) as resolver:
            record = self.edit(image_profile_id='new', image_resolution='1k', image_profile_snapshot={'id': 'injected'})
        resolver.assert_called_once_with('new', '1k')
        self.assertEqual(record['creation_parameters']['image_profile_snapshot'], snapshot)
        self.assertEqual(record['shots'], self.record['shots'])
        self.assertEqual(record['context'], self.record['context'])
        self.assertEqual(record['status'], 'storyboard_review')
        with patch.object(image_profiles, 'profile_provider_configs', return_value=[CONFIG]) as configs:
            self.assertEqual(video_studio._image_configs(record)[0]['model'], CONFIG['model'])
        configs.assert_called_once_with(snapshot)

    def test_invalid_selection_is_atomic(self):
        before = (self.path / 'record.json').read_bytes()
        with patch.object(image_profiles, 'profile_snapshot', side_effect=ValueError('Invalid model')):
            with self.assertRaises(HTTPException) as caught:
                self.edit(image_profile_id='missing')
        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual((self.path / 'record.json').read_bytes(), before)

    def test_provider_save_rebases_only_current_director_context(self):
        settings = self.record['settings']
        old = video_studio.planning_fingerprint(self.record['scenes'], settings['style'], settings['characters'],
            settings['world'], [], video_studio.planning_parameters(self.record))
        for current in (True, False):
            with self.subTest(current=current):
                self.record['context']['production_fingerprint'] = old if current else 'stale-cast'
                self.record['planning_state'] = {'fingerprint': old if current else 'stale-cast'}
                video_studio.save(self.path, self.record)
                with patch.object(image_profiles, 'profile_snapshot', return_value={'id': 'new'}):
                    record = self.edit(image_profile_id='new')
                expected = video_studio.planning_fingerprint(record['scenes'], settings['style'], settings['characters'],
                    settings['world'], [], video_studio.planning_parameters(record))
                self.assertNotEqual(expected, old)
                self.assertEqual(record['context']['production_fingerprint'], expected if current else 'stale-cast')
                self.assertEqual(record['planning_state']['fingerprint'], expected if current else 'stale-cast')

    def test_switch_from_pool_to_native_applies_personal_profile(self):
        self.record['creation_parameters']['use_cloud_image_pool'] = True
        video_studio.save(self.path, self.record)
        snapshot = {'id': 'native', 'model_id': 'test-image', 'resolution': '1k'}
        with patch.object(image_profiles, 'profile_snapshot', return_value=snapshot):
            record = self.edit(use_cloud_image_pool=False, image_profile_id='native', image_resolution='1k')
        with patch.object(image_profiles, 'profile_provider_configs', return_value=[CONFIG]) as configs, \
                patch('backend.app.cloud_client.cloud_client_for') as cloud:
            video_studio._image_configs(record, 1)
        configs.assert_called_once_with(snapshot)
        cloud.assert_not_called()
        self.assertEqual(record['shots'], self.record['shots'])

    def test_pool_selection_does_not_require_unused_personal_credentials(self):
        with patch.object(image_profiles, 'profile_snapshot') as resolver:
            record = self.edit(use_cloud_image_pool=True, image_profile_id='unused', image_resolution='4k')
        resolver.assert_not_called()
        self.assertTrue(record['creation_parameters']['use_cloud_image_pool'])
        self.assertNotIn('image_profile_snapshot', record['creation_parameters'])
        self.assertEqual(record['shots'], self.record['shots'])

    def test_legacy_null_profile_does_not_block_pool_switch(self):
        self.record['creation_parameters'].update(image_profile_id=None, image_resolution=None)
        video_studio.save(self.path, self.record)
        record = self.edit(use_cloud_image_pool=True)
        self.assertTrue(record['creation_parameters']['use_cloud_image_pool'])
        self.assertEqual(record['shots'], self.record['shots'])

    def test_legacy_model_applies_saved_resolution(self):
        record = self.edit(image_profile_id='', image_resolution='2k')
        with patch.object(visual, '_provider_configs', return_value=[{'resolution': '1k', 'api_key': 'fixture'}]):
            config = video_studio._image_configs(record)[0]
        self.assertEqual(config['resolution'], '2k')

    def test_clear_profile_removes_old_snapshot(self):
        with patch.object(image_profiles, 'profile_snapshot') as resolver:
            record = self.edit(image_profile_id='')
        resolver.assert_not_called()
        self.assertNotIn('image_profile_snapshot', record['creation_parameters'])
        self.assertEqual(record['shots'], self.record['shots'])

    def test_unrelated_edit_does_not_refresh_pinned_profile(self):
        with patch.object(image_profiles, 'profile_snapshot') as resolver:
            record = self.edit(subtitle_size=55)
        resolver.assert_not_called()
        self.assertEqual(record['creation_parameters']['image_profile_snapshot'], self.record['creation_parameters']['image_profile_snapshot'])

    def test_native_redraw_resolution_is_rejected_before_worker_or_revision_change(self):
        self.record['status'] = 'image_review'
        Image.new('RGB', (32, 18)).save(self.path / 'first.jpg')
        video_studio.save(self.path, self.record)
        before = (self.path / 'record.json').read_bytes()
        data = video_studio.StoryboardRedraw(revision=5, prompt='New frame', image_resolution='4k', use_scene_reference=False)
        with patch.object(video_studio, '_image_configs', return_value=[CONFIG]), \
                patch.object(video_studio, '_start_storyboard_redraw') as worker:
            with self.assertRaises(HTTPException) as caught:
                video_studio.redraw_storyboard_image(self.identity, 'shot01', data, Request({'type': 'http', 'headers': []}))
        self.assertEqual(caught.exception.status_code, 400)
        worker.assert_not_called()
        self.assertEqual((self.path / 'record.json').read_bytes(), before)

    def test_redraw_without_a_successful_core_fails_before_paid_generation(self):
        self.record['status'] = 'image_review'
        self.record['shots'][0].update(image_status='failed', image='')
        video_studio.save(self.path, self.record)
        before = (self.path / 'record.json').read_bytes()
        data = video_studio.StoryboardRedraw(revision=5, prompt='New frame', use_scene_reference=False)
        with patch.object(video_studio, '_image_configs') as configs, \
                patch.object(video_studio, '_start_storyboard_redraw') as worker:
            with self.assertRaises(HTTPException) as caught:
                video_studio.redraw_storyboard_image(self.identity, 'shot01', data, Request({'type': 'http', 'headers': []}))
        self.assertEqual(caught.exception.status_code, 409)
        worker.assert_not_called()
        configs.assert_not_called()
        self.assertEqual((self.path / 'record.json').read_bytes(), before)

    def test_opening_frame_omits_only_leading_whole_shot_intent(self):
        self.record['context']['story_handoff'] = {'status': 'legacy_body_only'}
        shot = {'reference_ids': []}
        prompt = '【本图旨在】A writes then B enters.\n【画面内容】Only A writing. Quoted label: 【本图旨在】.'
        original = copy.deepcopy(shot)
        cleaned, _, _ = video_studio._image_inputs(self.path, self.record, shot, prompt)
        self.assertEqual(cleaned, '【画面内容】Only A writing. Quoted label: 【本图旨在】.')
        self.assertEqual(shot, original)
        manual, _, _ = video_studio._image_inputs(self.path, self.record, shot, prompt, references=[])
        self.assertEqual(manual, prompt)
        self.record['context'] = {}
        legacy, _, _ = video_studio._image_inputs(self.path, self.record, shot, prompt)
        self.assertEqual(legacy, prompt)


if __name__ == '__main__':
    unittest.main()
