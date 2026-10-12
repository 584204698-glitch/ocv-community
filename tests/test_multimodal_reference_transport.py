import unittest
from unittest.mock import Mock, patch

from backend.app import gemini_client as client


class ReferenceTransportTest(unittest.TestCase):
    def test_multiple_images_keep_the_same_order_in_all_three_protocols(self):
        fixtures = [
            ('gemini', {'GEMINI_API_KEY': 'test-key'}, {'choices': [{'message': {'content': 'OK'}, 'finish_reason': 'stop'}]}),
            ('gemini_official', {'GOOGLE_API_KEY': 'test-key'}, {'candidates': [{'content': {'parts': [{'text': 'OK'}]}}]}),
            ('anthropic_official', {'ANTHROPIC_API_KEY': 'test-key'}, {'content': [{'type': 'text', 'text': 'OK'}], 'stop_reason': 'end_turn'}),
        ]
        pictures = [{'mime_type': 'image/png', 'data': value} for value in ['first', 'second', 'third']]
        for provider, environment, payload in fixtures:
            with self.subTest(provider=provider):
                response = Mock(ok=True, status_code=200)
                response.json.return_value = payload
                with patch.dict('os.environ', {'LANGUAGE_PROVIDER': provider, **environment}, clear=True), patch.object(client.requests, 'post', return_value=response) as post:
                    self.assertEqual(client.generate_gemini_text(system_prompt='system', user_prompt='Picture order', image_data=pictures), 'OK')
                    body = post.call_args.kwargs['json']
                if provider == 'gemini_official':
                    sent = [item['inlineData']['data'] for item in body['contents'][0]['parts'] if 'inlineData' in item]
                elif provider == 'anthropic_official':
                    sent = [item['source']['data'] for item in body['messages'][0]['content'] if item['type'] == 'image']
                else:
                    sent = [item['image_url']['url'].split(',')[1] for item in body['messages'][1]['content'] if item['type'] == 'image_url']
                self.assertEqual(sent, ['first', 'second', 'third'])

    def test_single_image_legacy_input_and_invalid_lists(self):
        picture = {'mime_type': 'image/png', 'data': 'first'}
        self.assertEqual(client._reference_images(picture), [picture])
        for value in [[{}], [picture] * 10, ['not image data']]:
            with self.assertRaises(ValueError):
                client._reference_images(value)


if __name__ == '__main__':
    unittest.main()
