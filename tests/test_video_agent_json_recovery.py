"""Bounded syntax recovery at the real video Agent provider-call boundary."""
import copy
import json
import unittest
from unittest.mock import patch

from backend.app.gemini_client import GeminiError
from backend.app.video_agents import VideoAgentJSONError, ask_json


SYSTEM = '保留米粒无外耳、蓝背心；镜头按原文顺序。'
DATA = {'story_context': {'story_handoff': {'status': 'current', 'brief': {'idea': '不要换装。'}}},
        'shots': [{'id': 'shot1'}, {'id': 'shot2'}]}
EXPECTED = {'shots': [{'id': 'shot1', 'video_prompt': '米粒写信。'},
                      {'id': 'shot2', 'video_prompt': '小鹿后入场接信。'}]}


class VideoAgentJSONRecoveryTest(unittest.TestCase):
    def test_valid_original_response_calls_provider_once(self):
        raw = json.dumps(EXPECTED, ensure_ascii=False)
        with patch('backend.app.video_agents.generate_gemini_text', return_value=raw) as generate:
            self.assertEqual(ask_json(SYSTEM, DATA), EXPECTED)
        generate.assert_called_once_with(
            system_prompt=SYSTEM, user_prompt=json.dumps(DATA, ensure_ascii=False),
            temperature=0.15, max_output_tokens=10000,
            response_mime_type='application/json', json_root='object',
        )

    def test_array_roots_and_encoded_json_wrappers_remain_compatible(self):
        body = json.dumps(EXPECTED, ensure_ascii=False)
        candidates = [json.dumps(EXPECTED['shots'], ensure_ascii=False),
                      json.dumps(body, ensure_ascii=False),
                      json.dumps(json.dumps(body, ensure_ascii=False), ensure_ascii=False),
                      '```json\n' + body + '\n```', ' \n' + body + '\n ']
        for raw in candidates:
            with self.subTest(raw=raw), patch('backend.app.video_agents.generate_gemini_text', return_value=raw) as generate:
                self.assertEqual(ask_json(SYSTEM, DATA), EXPECTED)
                self.assertEqual(generate.call_count, 1)

    def test_extra_data_gets_one_format_repair_with_all_original_inputs(self):
        first = json.dumps({'shots': [EXPECTED['shots'][0]]}, ensure_ascii=False)
        second = json.dumps({'shots': [EXPECTED['shots'][1]]}, ensure_ascii=False)
        raw = first + '\n' + second
        data = copy.deepcopy(DATA)
        with patch('backend.app.video_agents.generate_gemini_text',
                   side_effect=[raw, json.dumps(EXPECTED, ensure_ascii=False)]) as generate:
            self.assertEqual(ask_json(SYSTEM, data), EXPECTED)
        self.assertEqual(generate.call_count, 2)
        initial, repair = [entry.kwargs for entry in generate.call_args_list]
        self.assertEqual(initial['system_prompt'], SYSTEM)
        self.assertTrue(repair['system_prompt'].startswith(SYSTEM))
        self.assertIn('只修复', repair['system_prompt'])
        self.assertIn('保持原事实、剧情、镜头 id、所有内容及先后顺序', repair['system_prompt'])
        self.assertIn('不能只挑选其中一个丢弃其他内容', repair['system_prompt'])
        payload = json.loads(repair['user_prompt'])
        self.assertEqual(payload['original_request'], DATA)
        self.assertEqual(payload['raw_output'], raw)
        self.assertIn('Extra data', payload['parse_error'])
        self.assertEqual(repair['temperature'], 0.0)
        self.assertEqual(repair['max_output_tokens'], 10000)
        self.assertEqual(repair['response_mime_type'], 'application/json')
        self.assertEqual(repair['json_root'], 'object')
        self.assertEqual(data, DATA)

    def test_redundant_closers_after_complete_container_need_no_paid_repair(self):
        expected = {'shots': [{'id': 'rain', 'ending_prompt': 'Text containing ]} and \\"quoted\\" details.'}]}
        body = json.dumps(expected)
        for raw in (body+']}', body+' \n} ] ', '```json\n'+body+']}\n```',
                    json.dumps(expected['shots'])+']}'):
            with self.subTest(raw=raw), patch('backend.app.video_agents.generate_gemini_text', return_value=raw) as generate:
                self.assertEqual(ask_json(SYSTEM, DATA), expected)
                generate.assert_called_once()

    def test_trailing_content_is_preserved_for_repair_even_after_a_closer(self):
        body = json.dumps(EXPECTED)
        for tail in ('} {"shots":[{"id":"extra"}]}', ']} explanation', '\n42', ' [', ' false'):
            raw = body+tail
            with self.subTest(tail=tail), patch('backend.app.video_agents.generate_gemini_text',
                    side_effect=[raw, body]) as generate:
                self.assertEqual(ask_json(SYSTEM, DATA), EXPECTED)
                self.assertEqual(generate.call_count, 2)
                self.assertEqual(json.loads(generate.call_args.kwargs['user_prompt'])['raw_output'], raw)

    def test_multiple_fences_are_not_silently_reduced_to_the_first_object(self):
        raw = '```json\n{"shots":[{"id":"shot1"}]}\n```\n```json\n{"shots":[{"id":"shot2"}]}\n```'
        with patch('backend.app.video_agents.generate_gemini_text',
                   side_effect=[raw, json.dumps(EXPECTED)]) as generate:
            self.assertEqual(ask_json(SYSTEM, DATA), EXPECTED)
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(json.loads(generate.call_args.kwargs['user_prompt'])['raw_output'], raw)

    def test_json_after_a_fence_is_preserved_for_repair_not_discarded(self):
        raw = '```json\n{"shots":[{"id":"shot1"}]}\n```\n{"shots":[{"id":"shot2"}]}'
        with patch('backend.app.video_agents.generate_gemini_text',
                   side_effect=[raw, json.dumps(EXPECTED)]) as generate:
            ask_json(SYSTEM, DATA)
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(json.loads(generate.call_args.kwargs['user_prompt'])['raw_output'], raw)

    def test_nested_json_syntax_error_keeps_complete_raw_wrapper_for_repair(self):
        raw = json.dumps('{"shots":[{"id":"shot1"}]} {"shots":[{"id":"shot2"}]}')
        with patch('backend.app.video_agents.generate_gemini_text',
                   side_effect=[raw, json.dumps(EXPECTED)]) as generate:
            self.assertEqual(ask_json(SYSTEM, DATA), EXPECTED)
        self.assertEqual(json.loads(generate.call_args.kwargs['user_prompt'])['raw_output'], raw)

    def test_failed_repair_raises_after_two_calls(self):
        raw = '{"shots":[]} {"more":[]}'
        with patch('backend.app.video_agents.generate_gemini_text', side_effect=[raw, raw]) as generate:
            with self.assertRaises(VideoAgentJSONError) as caught:
                ask_json(SYSTEM, DATA)
        self.assertEqual(caught.exception.stage, 'repaired_content')
        self.assertIsInstance(caught.exception.__cause__, json.JSONDecodeError)
        self.assertNotIn(raw, str(caught.exception))
        self.assertEqual(generate.call_count, 2)

    def test_invalid_original_raw_values_are_rejected_without_repair(self):
        for raw in (None, {}, [], 42, '', ' \n\t ', 'x' * 100001):
            with self.subTest(raw_type=type(raw), length=len(raw) if isinstance(raw, str) else None):
                with patch('backend.app.video_agents.generate_gemini_text', return_value=raw) as generate:
                    with self.assertRaises(ValueError):
                        ask_json(SYSTEM, DATA)
                self.assertEqual(generate.call_count, 1)

    def test_invalid_repair_raw_values_cannot_trigger_a_third_call(self):
        for repaired in (None, '', 'x' * 100001):
            with self.subTest(repaired_type=type(repaired)):
                with patch('backend.app.video_agents.generate_gemini_text', side_effect=['{"shots":', repaired]) as generate:
                    with self.assertRaises(ValueError):
                        ask_json(SYSTEM, DATA)
                self.assertEqual(generate.call_count, 2)

    def test_valid_json_with_wrong_business_shape_is_not_repaired_here(self):
        for raw in ('null', 'false', '7', '"ordinary text"', '""', '[1,2]'):
            with self.subTest(raw=raw), patch('backend.app.video_agents.generate_gemini_text', return_value=raw) as generate:
                with self.assertRaisesRegex(ValueError, '未返回 JSON 对象'):
                    ask_json(SYSTEM, DATA)
                self.assertEqual(generate.call_count, 1)
        for value in ({}, {'shots': 'wrong type'}, {'shots': [{'id': 'wrong-order'}]}, {'other_field': []}):
            with self.subTest(value=value), patch('backend.app.video_agents.generate_gemini_text', return_value=json.dumps(value)) as generate:
                self.assertEqual(ask_json(SYSTEM, DATA), value)
                self.assertEqual(generate.call_count, 1)

    def test_repair_business_shape_is_left_for_existing_validators(self):
        wrong_shape = {'shots': 'still not a list'}
        with patch('backend.app.video_agents.generate_gemini_text',
                   side_effect=['{"shots":', json.dumps(wrong_shape)]) as generate:
            self.assertEqual(ask_json(SYSTEM, DATA), wrong_shape)
        self.assertEqual(generate.call_count, 2)

    def test_provider_and_network_failures_escape_without_local_retry(self):
        failures = [GeminiError('供应商失败'), TimeoutError('网络超时')]
        for failure in failures:
            with self.subTest(failure=failure), patch('backend.app.video_agents.generate_gemini_text', side_effect=failure) as generate:
                with self.assertRaises(type(failure)) as caught:
                    ask_json(SYSTEM, DATA)
                self.assertIs(caught.exception, failure)
                self.assertEqual(generate.call_count, 1)

    def test_invalid_provider_envelope_is_identified_without_repair_or_body_exposure(self):
        for during_repair in (False, True):
            failure = json.JSONDecodeError('private response detail', 'secret-response-body', 2)
            responses = ['{"shots":', failure] if during_repair else [failure]
            with self.subTest(during_repair=during_repair), patch(
                    'backend.app.video_agents.generate_gemini_text', side_effect=responses) as generate:
                with self.assertRaises(VideoAgentJSONError) as caught:
                    ask_json(SYSTEM, DATA)
            expected = 'repair_provider_response' if during_repair else 'provider_response'
            self.assertEqual(caught.exception.stage, expected)
            self.assertIs(caught.exception.__cause__, failure)
            self.assertEqual(generate.call_count, len(responses))
            self.assertNotIn('secret-response-body', str(caught.exception))
            self.assertNotIn('private response detail', str(caught.exception))
            self.assertEqual(len(caught.exception.output_sha256), 64)

    def test_provider_failure_during_repair_escapes_without_a_third_call(self):
        failure = GeminiError('修复调用被供应商拒绝')
        with patch('backend.app.video_agents.generate_gemini_text', side_effect=['{"shots":', failure]) as generate:
            with self.assertRaises(GeminiError) as caught:
                ask_json(SYSTEM, DATA)
        self.assertIs(caught.exception, failure)
        self.assertEqual(generate.call_count, 2)


if __name__ == '__main__':
    unittest.main()
