"""Native batch failures retain exact per-shot results and cannot borrow images."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import module4_video_render as visual
from backend.app import openai_image_transport as transport
from tests.test_openai_image_transport import Response, Session


class NativeBatchTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.mapping = [{'macro_scene_id': 'good', 'image_prompt': 'good', 'reference_image_paths': []},
                        {'macro_scene_id': 'failed', 'image_prompt': 'failed', 'reference_image_paths': []}]
        self.config = {'protocol': 'openai_images', 'base_url': 'https://fixture.example/v1',
                       'endpoint': '/images/generations', 'reference_endpoint': '/images/edits',
                       'api_key': 'test-key', 'account_label': 'test-account', 'model': 'fixture-model',
                       'ratio': '16:9', 'resolution': '1k', 'reference_images': True}

    def output(self, macro):
        return self.root / (macro['macro_scene_id'] + '.jpg')

    def test_unknown_native_failure_does_not_copy_neighbor_or_repost_on_resume(self):
        captured = []
        call_lock = threading.Lock()
        class RoutedSession(Session):
            def post(inner, url, **kwargs):
                prompt = kwargs['json']['prompt'].split('\n\n', 1)[1]
                with call_lock:
                    captured.append(prompt)
                inner.result = Response(status=503 if prompt == 'failed' else 200)
                return super().post(url, **kwargs)
        saved_success = Mock()
        with patch.object(visual, '_poster_output_path', side_effect=self.output), \
             patch.object(visual, '_record_partial_poster_success', saved_success), \
             patch.object(visual, '_hydrate_cloud_retry_context', side_effect=lambda macro: macro), \
             patch.object(transport.requests, 'Session', side_effect=RoutedSession), \
             patch.dict('os.environ', {'RUNNINGHUB_ALLOW_NEIGHBOR_FALLBACK': '1',
                                       'RUNNINGHUB_PER_KEY_CONCURRENCY': '2'}):
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, '海报任务生成失败'):
                    visual.render_posters_concurrently(self.mapping, [self.config])
        self.assertCountEqual(captured, ['good', 'failed'])
        self.assertTrue((self.root / 'good.jpg').exists())
        self.assertFalse((self.root / 'failed.jpg').exists())
        self.assertEqual(json.loads(transport.ledger_path(self.root / 'failed.jpg').read_text())['status'], 'unknown')
        self.assertTrue(saved_success.called)
        self.assertTrue(all(call.args[1] == 0 for call in saved_success.call_args_list))

    def test_rejected_native_failure_has_no_automatic_retry_or_neighbor_substitution(self):
        calls = []
        class RoutedSession(Session):
            def post(inner, url, **kwargs):
                prompt = kwargs['json']['prompt'].split('\n\n', 1)[1]
                calls.append(prompt)
                inner.result = Response(status=429 if prompt == 'failed' else 200)
                return super().post(url, **kwargs)
        with patch.object(visual, '_poster_output_path', side_effect=self.output), \
             patch.object(visual, '_record_partial_poster_success'), \
             patch.object(visual, '_hydrate_cloud_retry_context', side_effect=lambda macro: macro), \
             patch.object(transport.requests, 'Session', side_effect=RoutedSession), \
             patch.dict('os.environ', {'RUNNINGHUB_ALLOW_NEIGHBOR_FALLBACK': '1'}):
            with self.assertRaisesRegex(RuntimeError, 'HTTP_429_REJECTED'):
                visual.render_posters_concurrently(self.mapping, [self.config])
        self.assertCountEqual(calls, ['good', 'failed'])
        self.assertFalse((self.root / 'failed.jpg').exists())
        self.assertEqual(json.loads(transport.ledger_path(self.root / 'failed.jpg').read_text())['status'], 'rejected')

    def test_native_validation_failure_never_overwrites_existing_failed_shot_asset(self):
        good = self.root / 'good.jpg'
        failed = self.root / 'failed.jpg'
        good.write_bytes(b'new good image')
        failed.write_bytes(b'old failed-shot version')
        def render(macro, _pool):
            if macro['macro_scene_id'] == 'failed':
                raise transport.OpenAIImageError('OPENAI_IMAGE_REFERENCE_IMAGE_INVALID')
            return good
        with patch.object(visual, '_render_poster_with_retry', side_effect=render), \
             patch.object(visual, '_poster_output_path', side_effect=self.output), \
             patch.object(visual, '_record_partial_poster_success'), \
             patch.dict('os.environ', {'RUNNINGHUB_ALLOW_NEIGHBOR_FALLBACK': '1'}):
            with self.assertRaises(RuntimeError):
                visual.render_posters_concurrently(self.mapping, [self.config])
        self.assertEqual(failed.read_bytes(), b'old failed-shot version')

    def test_legacy_async_batch_retains_its_existing_configurable_neighbor_policy(self):
        good = self.root / 'good.jpg'
        good.write_bytes(b'legacy result')
        def render(macro, _pool):
            if macro['macro_scene_id'] == 'failed':
                raise RuntimeError('legacy terminal error')
            return good
        with patch.object(visual, '_render_poster_with_retry', side_effect=render), \
             patch.object(visual, '_poster_output_path', side_effect=self.output), \
             patch.object(visual, '_record_partial_poster_success'), \
             patch.dict('os.environ', {'RUNNINGHUB_ALLOW_NEIGHBOR_FALLBACK': '1'}):
            paths = visual.render_posters_concurrently(self.mapping, [{**self.config, 'protocol': 'async_task'}])
        self.assertEqual(paths, [good, self.root / 'failed.jpg'])
        self.assertEqual(paths[1].read_bytes(), good.read_bytes())


if __name__ == '__main__':
    unittest.main()
