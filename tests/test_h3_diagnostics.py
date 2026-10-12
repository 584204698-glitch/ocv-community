"""H3 validation failures remain inspectable without starting video generation."""
import copy
import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from PIL import Image

from backend.app import h3_prompt_agent as agent
from backend.app import video_generation as generation
from backend.app import video_inputs as inputs
from backend.app import video_studio as studio


class H3DiagnosticTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.identity = 'a' * 32
        self.path = self.root / '1' / self.identity
        self.path.mkdir(parents=True)
        Image.new('RGB', (16, 16), 'blue').save(self.path / 'core.png')
        self.record = {
            'id': self.identity, 'revision': 1, 'status': 'video_generation_ready',
            'settings': {'name': 'Synthetic H3 diagnostic project', 'ratio': '16:9'},
            'creation_parameters': {
                'video_generation_backend': 'comfyui', 'comfyui_profile_id': 'synthetic-h3',
                'comfyui_h3_prompt_agent': True,
            },
            'logs': [], 'references': [],
            'shots': [
                {'id': 'shot-A', 'kind': 'video', 'image': 'core.png',
                 'image_status': 'completed', 'video_status': 'pending',
                 'duration': 4, 'generation_duration': 4,
                 'video_prompt': 'A gently lowers a letter onto the table.'},
                {'id': 'shot-B', 'kind': 'video', 'image': 'core.png',
                 'image_status': 'completed', 'video_status': 'completed',
                 'duration': 4, 'generation_duration': 4,
                 'video': 'previous-B.mp4', 'video_task_id': 'unchanged-task-B',
                 'video_prompt': 'B remains beside the window.'},
            ],
        }
        (self.path / 'previous-B.mp4').write_bytes(b'unchanged synthetic companion video')
        studio.save(self.path, self.record)
        self.profile = {
            'id': 'synthetic-h3', 'name': 'Synthetic H3', 'kind': 'video',
            'video_input_mode': 'single_first',
            'workflow': {
                '1': {'class_type': 'LoadImage', 'inputs': {'image': 'unused.png'}},
                '9': {'class_type': 'Text', 'inputs': {'text': ''}},
                'h3': {'class_type': 'MiniMaxH3ImageToVideo', 'inputs': {'first_frame': ['1', 0]}},
            },
            'mappings': {'image': {'node_id': '1', 'input_name': 'image'},
                         'prompt': {'node_id': '9', 'input_name': 'text'}},
        }
        self.raw_marker = 'PRIVATE_RAW_DIAGNOSTIC_793e'
        self.draft = 'PRIVATE_DRAFT_DIAGNOSTIC_d9c1: invalid H3 structure'
        self.raw = json.dumps({'h3_prompt': self.draft, 'diagnostic_marker': self.raw_marker})
        replacements = [
            patch.object(studio, 'ROOT', self.root),
            patch.object(studio, 'require_user', return_value={'id': 1}),
            patch.object(studio, 'project_language_scope', side_effect=lambda *args: nullcontext()),
            patch.object(inputs, '_profiles', return_value=[self.profile]),
            patch.object(inputs, 'video_profile', return_value=self.profile),
            patch.object(generation, '_connection', return_value={'base_url': 'http://127.0.0.1:1', 'prompt_path': '/prompt'}),
            patch.object(generation, 'video_task_state', side_effect=AssertionError('No real task lookup is permitted')),
            patch.object(generation, 'run_video_profile', side_effect=AssertionError('No video submission is permitted')),
            patch.object(agent, 'generate_gemini_text', side_effect=AssertionError('No model call is permitted')),
        ]
        for replacement in replacements:
            replacement.start()
            self.addCleanup(replacement.stop)
        app = FastAPI()
        app.include_router(generation.router)
        app.include_router(studio.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.base = f'/api/video-studio/{self.identity}'
        self.url = self.base + '/shots/shot-A/h3-diagnostic'

    def validation_failure(self):
        with patch.object(agent, 'generate_gemini_text', return_value=self.raw):
            with self.assertRaises(agent.H3PromptValidationError) as raised:
                agent.convert_for_h3(self.record['shots'][0], 4, image_path=self.path / 'core.png')
        self.assertEqual(raised.exception.diagnostic['raw_response'], self.raw)
        self.assertEqual(raised.exception.diagnostic['draft_prompt'], self.draft)
        return raised.exception

    def run_worker_failure(self, failure, *, direct=False):
        """Execute the actual worker synchronously, with every paid boundary mocked."""
        expected_prior = generation._load_h3_diagnostic(self.path, studio.read(self.path)['shots'][0])
        with patch.object(generation.threading, 'Thread') as thread, \
                patch.object(generation, 'convert_for_h3', side_effect=failure) as convert, \
                patch.object(generation, '_freeze_local_request') as freeze, \
                patch.object(generation, 'run_video_profile') as bridge:
            if direct:
                generation._start_local_worker(self.path, studio.read(self.path), ['shot-A'], 1,
                                               'synthetic-h3', use_h3_agent=True)
            else:
                response = self.client.post(self.base + '/videos/generate', json={
                    'revision': studio.read(self.path)['revision'], 'shot_ids': ['shot-A'],
                })
                self.assertEqual(response.status_code, 200, response.text)
                self.assertNotIn(self.raw_marker, response.text)
                self.assertNotIn(self.draft, response.text)
            thread.assert_called_once()
            thread.call_args.kwargs['target']()
            convert.assert_called_once()
            self.assertEqual(convert.call_args.kwargs['previous_diagnostic'], expected_prior)
            freeze.assert_not_called()
            bridge.assert_not_called()
        current = studio.read(self.path)
        self.assertEqual(current['shots'][0]['video_status'], 'failed', current['logs'])
        self.assertEqual(current['shots'][1], self.record['shots'][1])
        self.assertNotIn(str(self.path), studio.ACTIVE)
        self.assertNotIn(str(self.path), generation.LOCAL_VIDEO_QUEUES)
        return current

    def saved_diagnostic(self):
        failure = self.validation_failure()
        record = studio.read(self.path)
        generation._save_h3_diagnostic(self.path, record['shots'][0], failure)
        studio.save(self.path, record)
        return failure, record['shots'][0]['h3_prompt_diagnostic']

    def test_local_batch_stops_before_next_shot_after_preparation_failure(self):
        record = studio.read(self.path)
        pending = copy.deepcopy(record['shots'][0])
        pending['id'] = 'shot-C'
        record['shots'].append(pending)
        studio.save(self.path, record)
        with patch.object(generation.threading, 'Thread') as thread, \
                patch.object(generation, 'convert_for_h3', side_effect=RuntimeError('language connection failed')) as convert, \
                patch.object(generation, '_freeze_local_request') as freeze, \
                patch.object(generation, 'run_video_profile') as bridge:
            generation._start_local_worker(self.path, record, ['shot-A', 'shot-C'], 1,
                                           'synthetic-h3', use_h3_agent=True)
            thread.call_args.kwargs['target']()
        convert.assert_called_once()
        freeze.assert_not_called()
        bridge.assert_not_called()
        current = studio.read(self.path)
        self.assertEqual(current['status'], 'video_review')
        self.assertEqual(current['shots'][0]['video_status'], 'failed')
        self.assertEqual(current['shots'][0]['video_error'],
                         'H3 提示词转换或本地请求准备失败：language connection failed')
        self.assertTrue(current['shots'][0]['video_terminal'])
        self.assertNotIn('snapshot', current.get('error', ''))
        self.assertEqual(current['shots'][1], self.record['shots'][1])
        self.assertEqual(current['shots'][2], pending)

    def test_request_freeze_failure_preserves_original_error_and_assets(self):
        core = (self.path / 'core.png').read_bytes()
        with patch.object(generation.threading, 'Thread') as thread, \
                patch.object(generation, 'convert_for_h3', return_value=('draft', 'test')), \
                patch.object(generation, '_freeze_local_request',
                             side_effect=ValueError('TTS preparation unavailable')), \
                patch.object(generation, 'run_video_profile') as bridge:
            generation._start_local_worker(self.path, studio.read(self.path), ['shot-A'], 1,
                                           'synthetic-h3', use_h3_agent=True)
            thread.call_args.kwargs['target']()
        bridge.assert_not_called()
        current = studio.read(self.path)
        self.assertEqual(current['shots'][0]['video_status'], 'failed')
        self.assertIn('TTS preparation unavailable', current['shots'][0]['video_error'])
        self.assertTrue(current['shots'][0]['video_terminal'])
        self.assertFalse(current['shots'][0]['video_execution_started'])
        self.assertNotIn('snapshot', current.get('error', ''))
        self.assertEqual(current['shots'][1], self.record['shots'][1])
        self.assertEqual((self.path / 'core.png').read_bytes(), core)

    def test_cuda_failure_keeps_task_id_and_does_not_submit_following_shot(self):
        record = studio.read(self.path)
        pending = copy.deepcopy(record['shots'][0])
        pending['id'] = 'shot-C'
        record['shots'].append(pending)
        studio.save(self.path, record)
        def freeze(path, current, shot, *args, **kwargs):
            shot.update(video='clip.mp4', video_request={'profile_id': 'synthetic-h3', 'prompt': 'p',
                        'reference_files': ['core.png'], 'duration': 4, 'ratio': '16:9'})
        def fail_cuda(*args, **kwargs):
            kwargs['on_submitted']('owned-failed-task')
            raise RuntimeError('Triton Error [CUDA]: an illegal memory access was encountered')
        with patch.object(generation.threading, 'Thread') as thread, \
                patch.object(generation, '_freeze_local_request', side_effect=freeze), \
                patch.object(generation, 'run_video_profile', side_effect=fail_cuda) as bridge:
            generation._start_local_worker(self.path, record, ['shot-A', 'shot-C'], 1,
                                           'synthetic-h3', use_h3_agent=False)
            thread.call_args.kwargs['target']()
        bridge.assert_called_once()
        current = studio.read(self.path)
        self.assertEqual(current['shots'][0]['video_status'], 'failed')
        self.assertEqual(current['shots'][0]['video_task_id'], 'owned-failed-task')
        self.assertEqual(current['shots'][1], self.record['shots'][1])
        self.assertEqual(current['shots'][2], pending)

    def assert_no_raw_in_project(self, record):
        serialized = json.dumps(record, ensure_ascii=False)
        self.assertNotIn(self.raw_marker, serialized)
        self.assertNotIn(self.draft, serialized)
        self.assertNotIn('raw_response', serialized)
        self.assertNotIn('draft_prompt', serialized)

    def test_explicit_retry_forwards_saved_draft_and_keeps_original_evidence(self):
        failure, descriptor = self.saved_diagnostic()
        old_file = self.path / descriptor['file']
        old_bytes = old_file.read_bytes()
        self.assertEqual(generation._load_h3_diagnostic(self.path, studio.read(self.path)['shots'][0]),
                         failure.diagnostic)
        current = self.run_worker_failure(failure, direct=True)
        self.assertEqual(old_file.read_bytes(), old_bytes)
        self.assertNotEqual(current['shots'][0]['h3_prompt_diagnostic']['file'], descriptor['file'])
        self.assert_no_raw_in_project(current)

    def test_unreadable_or_mismatched_prior_diagnostic_is_ignored(self):
        _, descriptor = self.saved_diagnostic()
        shot = studio.read(self.path)['shots'][0]
        file = self.path / descriptor['file']
        for content in (b'{bad json', b'[]', b'{}', b'x' * (512 * 1024 + 1)):
            with self.subTest(size=len(content)):
                file.write_bytes(content)
                self.assertIsNone(generation._load_h3_diagnostic(self.path, shot))
        shot['h3_prompt_diagnostic']['file'] = 'record.json'
        self.assertIsNone(generation._load_h3_diagnostic(self.path, shot))

    def test_accepted_retry_batch_immediately_clears_only_selected_failure_labels(self):
        _, descriptor = self.saved_diagnostic()
        record = studio.read(self.path)
        record['status'] = 'video_review'
        first = record['shots'][0]
        first.update(video_status='failed', video_error='previous failure', video_terminal=True)
        for identity in ('shot-C', 'shot-D'):
            other = copy.deepcopy(first)
            other['id'] = identity
            record['shots'].append(other)
        untouched = copy.deepcopy([record['shots'][1], record['shots'][3]])
        studio.save(self.path, record)
        with patch.object(generation, '_start_local_worker') as worker, \
                patch.object(generation, '_freeze_local_request') as freeze:
            response = self.client.post(self.base + '/videos/generate', json={
                'revision': record['revision'], 'shot_ids': ['shot-A', 'shot-C'], 'retry_failed': True})
            self.assertEqual(response.status_code, 200, response.text)
            worker.assert_called_once()
            freeze.assert_not_called()
        current = studio.read(self.path)
        self.assertEqual([current['shots'][1], current['shots'][3]], untouched)
        for index in (0, 2):
            shot = current['shots'][index]
            self.assertEqual(shot['video_status'], 'pending')
            self.assertEqual(shot['video_error'], '')
            self.assertFalse(shot['video_terminal'])
            self.assertEqual(shot['h3_prompt_diagnostic'], descriptor)
            self.assertFalse(shot.get('video_task_id'))

    def test_invalid_retry_batch_does_not_erase_failure_labels(self):
        record = studio.read(self.path)
        record['status'] = 'video_review'
        record['shots'][0].update(video_status='failed', video_error='previous failure',
                                  video_terminal=True, video_prompt='')
        studio.save(self.path, record)
        before = (self.path/'record.json').read_bytes()
        with patch.object(generation, '_start_local_worker') as worker:
            response = self.client.post(self.base + '/videos/generate', json={
                'revision': record['revision'], 'shot_ids': ['shot-A'], 'retry_failed': True})
            self.assertEqual(response.status_code, 400, response.text)
            worker.assert_not_called()
        self.assertEqual((self.path/'record.json').read_bytes(), before)

    def test_worker_validation_failure_saves_diagnostic_before_any_freeze_or_submission(self):
        failure = self.validation_failure()
        current = self.run_worker_failure(failure)
        shot = current['shots'][0]
        self.assertIn(str(failure), shot['video_error'])
        self.assertFalse(shot.get('video_execution_started'))
        self.assertNotIn('video_attempt', shot)
        self.assertNotIn('video_request', shot)
        self.assertNotIn('video_task_id', shot)
        self.assertFalse((self.path / 'assets/videos/shot-A').exists())
        descriptor = shot['h3_prompt_diagnostic']
        self.assertEqual(set(descriptor), {'file', 'source_fingerprint', 'error'})
        self.assertRegex(descriptor['file'], r'^assets/h3_diagnostics/[a-f0-9]{32}\.json$')
        self.assertEqual(descriptor['source_fingerprint'], failure.diagnostic['source_fingerprint'])
        self.assertEqual(descriptor['error'], failure.diagnostic['error'])
        diagnostic = json.loads((self.path / descriptor['file']).read_text(encoding='utf-8'))
        for key, value in failure.diagnostic.items():
            self.assertEqual(diagnostic[key], value)
        self.assert_no_raw_in_project(current)
        response = self.client.get(self.base)
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_no_raw_in_project(response.json())

    def test_diagnostic_write_failure_retains_validation_error_and_prevents_submission(self):
        failure = self.validation_failure()
        with patch.object(generation, '_save_h3_diagnostic', side_effect=OSError('synthetic disk full')) as save:
            current = self.run_worker_failure(failure)
            save.assert_called_once()
        shot = current['shots'][0]
        self.assertIn(str(failure), shot['video_error'])
        self.assertNotEqual(shot['video_error'], 'synthetic disk full')
        self.assertNotIn('h3_prompt_diagnostic', shot)
        self.assertNotIn('video_request', shot)
        self.assertNotIn('video_attempt', shot)
        self.assert_no_raw_in_project(current)

    def test_validation_failure_preserves_existing_video_task_request_and_history(self):
        shot = self.record['shots'][0]
        old_fields = {
            'video': 'assets/videos/previous.mp4', 'video_task_id': 'previous-task-A',
            'video_attempt': 3, 'video_version': 'previous-version-A',
            'video_request': {'backend': 'comfyui', 'profile_id': 'old-profile', 'prompt': 'original frozen prompt'},
            'video_history': [{'video': 'older.mp4', 'video_task_id': 'older-task-A', 'video_attempt': 2}],
        }
        shot.update(copy.deepcopy(old_fields))
        old_media = self.path / old_fields['video']
        old_media.parent.mkdir(parents=True)
        old_media.write_bytes(b'existing synthetic video bytes')
        studio.save(self.path, self.record)
        failure = self.validation_failure()
        # Identity recovery is a separate contract: this case starts after it
        # has determined that no active task should bypass prompt conversion.
        with patch.object(generation, '_recover_local_prompt_id', return_value=''):
            current = self.run_worker_failure(failure, direct=True)
        for key, value in old_fields.items():
            self.assertEqual(current['shots'][0][key], value, key)
        self.assertEqual(old_media.read_bytes(), b'existing synthetic video bytes')
        self.assertIn('h3_prompt_diagnostic', current['shots'][0])
        self.assert_no_raw_in_project(current)

    def test_network_failure_does_not_masquerade_as_a_prompt_validation_diagnostic(self):
        with patch.object(generation, '_save_h3_diagnostic') as save:
            current = self.run_worker_failure(TimeoutError('synthetic language request timeout'))
            save.assert_not_called()
        self.assertIn('synthetic language request timeout', current['shots'][0]['video_error'])
        self.assertNotIn('h3_prompt_diagnostic', current['shots'][0])
        self.assertFalse((self.path / 'assets/h3_diagnostics').exists())

    def test_failed_atomic_replacement_keeps_the_prior_diagnostic_and_cleans_temporary_file(self):
        failure, descriptor = self.saved_diagnostic()
        shot = studio.read(self.path)['shots'][0]
        before = copy.deepcopy(shot)
        old_file = self.path / descriptor['file']
        old_content = old_file.read_bytes()
        with patch.object(generation.os, 'replace', side_effect=OSError('synthetic atomic replace failure')):
            with self.assertRaises(OSError):
                generation._save_h3_diagnostic(self.path, shot, failure)
        self.assertEqual(shot, before)
        self.assertEqual(old_file.read_bytes(), old_content)
        self.assertEqual(list(old_file.parent.iterdir()), [old_file])

    def test_download_is_a_private_json_attachment_and_the_only_response_with_raw_content(self):
        failure, descriptor = self.saved_diagnostic()
        before = (self.path / 'record.json').read_bytes()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.headers['content-type'].startswith('application/json'))
        self.assertIn('attachment', response.headers['content-disposition'].lower())
        self.assertIn('.json', response.headers['content-disposition'].lower())
        self.assertIn('private', response.headers['cache-control'].lower())
        self.assertIn('no-store', response.headers['cache-control'].lower())
        self.assertEqual(response.json()['raw_response'], failure.diagnostic['raw_response'])
        self.assertEqual(response.json()['draft_prompt'], failure.diagnostic['draft_prompt'])
        self.assertEqual(response.content, (self.path / descriptor['file']).read_bytes())
        project_response = self.client.get(self.base)
        self.assertEqual(project_response.status_code, 200, project_response.text)
        self.assert_no_raw_in_project(project_response.json())
        self.assertEqual((self.path / 'record.json').read_bytes(), before)

    def test_download_requires_authentication_and_never_uses_another_owners_project(self):
        self.saved_diagnostic()
        with patch.object(studio, 'require_user', side_effect=HTTPException(401, '需要登录')):
            response = self.client.get(self.url)
            self.assertEqual(response.status_code, 401, response.text)
        with patch.object(studio, 'require_user', return_value={'id': 2}):
            response = self.client.get(self.url)
            self.assertEqual(response.status_code, 404, response.text)
            self.assertNotIn(self.raw_marker, response.text)
            self.assertNotIn(self.draft, response.text)

    def test_diagnostic_is_scoped_to_its_shot_and_project(self):
        _, descriptor = self.saved_diagnostic()
        response = self.client.get(self.base + '/shots/shot-B/h3-diagnostic')
        self.assertEqual(response.status_code, 404, response.text)
        response = self.client.get(self.base + '/shots/missing-shot/h3-diagnostic')
        self.assertEqual(response.status_code, 404, response.text)
        other_identity = 'b' * 32
        other = copy.deepcopy(self.record)
        other['id'] = other_identity
        other['shots'][0]['h3_prompt_diagnostic'] = copy.deepcopy(descriptor)
        studio.save(self.root / '1' / other_identity, other)
        response = self.client.get(f'/api/video-studio/{other_identity}/shots/shot-A/h3-diagnostic')
        self.assertEqual(response.status_code, 404, response.text)
        self.assertNotIn(self.raw_marker, response.text)

    def test_absent_diagnostic_and_missing_diagnostic_file_return_not_found(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 404, response.text)
        record = studio.read(self.path)
        record['shots'][0]['h3_prompt_diagnostic'] = {
            'file': 'assets/h3_diagnostics/' + 'c' * 32 + '.json',
            'source_fingerprint': 'd' * 64, 'error': 'synthetic missing diagnostic',
        }
        studio.save(self.path, record)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 404, response.text)

    def test_non_diagnostic_paths_are_rejected_even_when_the_target_exists(self):
        invalid = [
            'record.json', '../record.json',
            'assets/h3_diagnostics/../../record.json',
            'assets/h3_diagnostics/' + 'a' * 31 + '.json',
            'assets/h3_diagnostics/' + 'A' * 32 + '.json',
            'assets/h3_diagnostics/' + 'a' * 32 + '.txt',
            'assets/h3_diagnostics/nested/' + 'a' * 32 + '.json',
            'assets\\h3_diagnostics\\' + 'a' * 32 + '.json',
            str((self.path / 'record.json').resolve()),
        ]
        for relative in invalid:
            with self.subTest(relative=relative):
                record = studio.read(self.path)
                record['shots'][0]['h3_prompt_diagnostic'] = {
                    'file': relative, 'source_fingerprint': 'd' * 64, 'error': 'synthetic',
                }
                studio.save(self.path, record)
                response = self.client.get(self.url)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertNotIn('Synthetic H3 diagnostic project', response.text)

    def test_canonical_diagnostic_path_cannot_resolve_to_another_project_asset(self):
        _, descriptor = self.saved_diagnostic()
        requested = self.path / descriptor['file']
        target = self.path / 'private-unrelated.json'
        target.write_text(json.dumps({'raw_response': self.raw_marker}), encoding='utf-8')
        original_resolve = Path.resolve

        def redirected(path, *args, **kwargs):
            if path == requested:
                return original_resolve(target)
            return original_resolve(path, *args, **kwargs)

        # Exercise the canonical-path guard even on Windows hosts that cannot
        # create native symlinks; the native filesystem case is also below.
        with patch.object(Path, 'resolve', redirected):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertNotIn(self.raw_marker, response.text)

    def test_project_directory_alias_cannot_download_another_project_diagnostic(self):
        _, descriptor = self.saved_diagnostic()
        other_identity = 'b' * 32
        other_path = self.root / '1' / other_identity
        other = copy.deepcopy(studio.read(self.path))
        other['id'] = other_identity
        studio.save(other_path, other)
        aliased_file = other_path / descriptor['file']
        aliased_file.parent.mkdir(parents=True)
        aliased_file.write_bytes((self.path / descriptor['file']).read_bytes())
        original_resolve = Path.resolve

        def redirected(path, *args, **kwargs):
            try:
                relative = path.relative_to(self.path)
            except ValueError:
                return original_resolve(path, *args, **kwargs)
            return original_resolve(other_path / relative, *args, **kwargs)

        with patch.object(Path, 'resolve', redirected):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 404, response.text)
        self.assertNotIn(self.raw_marker, response.text)

    def test_symlink_cannot_escape_diagnostic_directory_even_within_the_same_project(self):
        directory = self.path / 'assets/h3_diagnostics'
        directory.mkdir(parents=True)
        target = self.path / 'private-unrelated.json'
        target.write_text(json.dumps({'raw_response': self.raw_marker}), encoding='utf-8')
        relative = 'assets/h3_diagnostics/' + 'e' * 32 + '.json'
        try:
            (self.path / relative).symlink_to(target)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f'Symlink creation is unavailable: {exc}')
        record = studio.read(self.path)
        record['shots'][0]['h3_prompt_diagnostic'] = {
            'file': relative, 'source_fingerprint': 'd' * 64, 'error': 'synthetic',
        }
        studio.save(self.path, record)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertNotIn(self.raw_marker, response.text)


if __name__ == '__main__':
    unittest.main()
