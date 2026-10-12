import json
import unittest
from unittest.mock import Mock, patch

import requests

from backend.app import gemini_client as client
from backend.app.video_agents import ask_json


def packet(content=None, finish=None):
    return 'data: ' + json.dumps({'choices': [{'index': 0, 'delta': {'content': content},
                                              'finish_reason': finish}]}, ensure_ascii=False)


def response_for(events):
    response = Mock(ok=True)
    response.iter_lines.return_value = iter([line for event in events for line in (event, '')])
    return response


class LanguageStreamTests(unittest.TestCase):
    env = {'LANGUAGE_PROVIDER': 'custom', 'CUSTOM_LLM_API_KEY': 'test-key',
           'CUSTOM_LLM_API_BASE': 'https://example.invalid/v1', 'CUSTOM_LLM_MODEL': 'test-model',
           'GEMINI_RETRY_COUNT': '3'}

    def call(self, response):
        with patch.dict('os.environ', self.env, clear=True), \
                patch.object(client.requests, 'post', return_value=response) as post:
            value = ask_json('Return JSON', {'story': '小熊'}, stream=True)
        self.assertEqual(post.call_count, 1)
        self.assertTrue(post.call_args.kwargs['stream'])
        self.assertTrue(post.call_args.kwargs['json']['stream'])
        response.close.assert_called_once()
        return value

    def test_chinese_chunks_heartbeats_usage_and_end_are_preserved(self):
        response = response_for([': heartbeat', packet('{"story":"小'), packet('熊"}'),
                                 packet(finish='stop'), 'data: {"choices":[],"usage":{"total_tokens":12}}',
                                 'data: [DONE]'])
        self.assertEqual(self.call(response), {'story': '小熊'})
        self.assertEqual(response.encoding, 'utf-8')

    def test_partial_response_cannot_be_used_or_paid_repaired(self):
        for events in ([packet('{"story":"小熊"}')],
                       [packet('{"story":"小熊"}'), 'data: [DONE]'],
                       [packet('{"story":"小熊"}'), packet(finish='stop')],
                       [packet('{"story":"小熊"}'), packet(finish='content_filter'), 'data: [DONE]'],
                       ['data: {"error":{"message":"provider failure"}}']):
            response = response_for(events)
            with self.subTest(events=events), patch.dict('os.environ', self.env, clear=True), \
                    patch.object(client.requests, 'post', return_value=response) as post:
                with self.assertRaises(client.GeminiError):
                    ask_json('Return JSON', {}, stream=True)
                self.assertEqual(post.call_count, 1)
                response.close.assert_called_once()

    def test_truncation_retains_distinct_exception(self):
        response = response_for([packet('{"story":'), packet(finish='length')])
        with self.assertRaises(client.GeminiOutputTruncated):
            self.call(response)

    def test_empty_and_unfinished_answers_have_distinct_safe_diagnostics(self):
        for events, expected in (
            (['data: [DONE]'], '未返回正文'),
            ([packet('private draft'), 'data: [DONE]'], '已收到 13 字符'),
        ):
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(client.GeminiError, expected) as error:
                    self.call(response_for(events))
                self.assertNotIn('private draft', str(error.exception))

    def test_midstream_disconnect_never_retries(self):
        def chunks():
            yield packet('{"story":')
            yield ''
            raise requests.ConnectionError('disconnected')
        response = Mock(ok=True)
        response.iter_lines.return_value = chunks()
        with patch.dict('os.environ', self.env, clear=True), \
                patch.object(client.requests, 'post', return_value=response) as post:
            with self.assertRaisesRegex(client.GeminiError, '未自动重复请求'):
                ask_json('Return JSON', {}, stream=True)
            self.assertEqual(post.call_count, 1)
            response.close.assert_called_once()

    def test_uncertain_initial_timeout_never_retries(self):
        with patch.dict('os.environ', self.env, clear=True), \
                patch.object(client.requests, 'post', side_effect=requests.ReadTimeout) as post:
            with self.assertRaisesRegex(client.GeminiError, '未自动重复请求'):
                ask_json('Return JSON', {}, stream=True)
            self.assertEqual(post.call_count, 1)

    def test_invalid_event_is_not_forwarded_to_paid_json_repair(self):
        response = response_for(['data: {secret provider body'])
        with patch.dict('os.environ', self.env, clear=True), \
                patch.object(client.requests, 'post', return_value=response) as post:
            with self.assertRaises(client.GeminiError) as error:
                ask_json('Return JSON', {}, stream=True)
            self.assertNotIn('secret provider body', str(error.exception))
            self.assertEqual(post.call_count, 1)

    def test_default_request_remains_nonstreaming(self):
        response = Mock(ok=True)
        response.json.return_value = {'choices': [{'message': {'content': '{}'}, 'finish_reason': 'stop'}]}
        with patch.dict('os.environ', self.env, clear=True), \
                patch.object(client.requests, 'post', return_value=response) as post:
            self.assertEqual(ask_json('Return JSON', {}), {})
            self.assertNotIn('stream', post.call_args.kwargs)
            self.assertNotIn('stream', post.call_args.kwargs['json'])

    def test_full_plan_can_request_larger_output_without_changing_default(self):
        response = response_for([packet('{}'), packet(finish='stop'), 'data: [DONE]'])
        with patch.dict('os.environ', self.env, clear=True), \
                patch.object(client.requests, 'post', return_value=response) as post:
            self.assertEqual(ask_json('Return JSON', {}, stream=True, max_output_tokens=24000), {})
            self.assertEqual(post.call_args.kwargs['json']['max_tokens'], 24000)
            self.assertEqual(post.call_count, 1)
