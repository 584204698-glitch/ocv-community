"""Exercise one-dispatch transport and its receipts without a paid provider."""
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from PIL import Image
import requests

from backend.app import openai_image_transport as transport


def picture(color='red', *, size=(32, 18), format='PNG'):
    output = io.BytesIO()
    Image.new('RGB', size, color).save(output, format=format)
    return output.getvalue()


def payload(content=None):
    return {'data': [{'b64_json': base64.b64encode(content if content is not None else picture()).decode()}]}


class Response:
    def __init__(self, data=None, *, status=200, content=None, headers=None, chunks=None):
        self.status_code = status
        self.body = content if content is not None else json.dumps(data if data is not None else payload()).encode()
        self.headers = headers if headers is not None else {'Content-Length': str(len(self.body))}
        self.chunks = chunks
        self.closed = False

    def iter_content(self, chunk_size):
        if self.chunks is not None:
            yield from self.chunks
        else:
            for index in range(0, len(self.body), chunk_size):
                yield self.body[index:index + chunk_size]

    def close(self):
        self.closed = True


class Session:
    def __init__(self, result=None):
        self.result = result if result is not None else Response()
        self.calls = []
        self.closed = False
        self.before_post = None

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.before_post:
            self.before_post()
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def close(self):
        self.closed = True


class HTTPFixture:
    """Real requests encoding proves order in the bytes received by a server."""
    def __init__(self, status=200):
        self.calls = []
        outer = self
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                outer.calls.append({'path': self.path, 'type': self.headers.get('Content-Type'), 'body': body,
                                    'authorization': self.headers.get('Authorization')})
                content = json.dumps(payload()).encode()
                self.send_response(status)
                if status == 302:
                    self.send_header('Location', f'http://127.0.0.1:{outer.server.server_port}/unexpected')
                self.send_header('Content-Length', str(len(content)))
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(content)
            def log_message(self, *_args):
                pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}/private/v1'

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class TransportTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / 'result.jpg'
        self.config = {'protocol': 'openai_images', 'base_url': 'https://private-provider.example/private/v1',
                       'api_key': 'secret-fixture-key', 'model': 'compatible-model',
                       'endpoint': '/images/generations', 'reference_endpoint': '/images/edits',
                       'ratio': '16:9', 'resolution': '1k', 'reference_images': True}
        self.macro = {'image_prompt': 'private prompt: first character writes, second enters later.',
                      'reference_image_paths': []}

    def run_image(self, session=None, *, output=None, config=None, macro=None):
        session = session or Session()
        return transport.render_openai_image(macro or self.macro, config or self.config,
                                            output or self.output, session_factory=lambda: session)

    def receipt(self, output=None):
        return json.loads(transport.ledger_path(output or self.output).read_text(encoding='utf-8'))

    def references(self, count=2):
        paths = []
        for index in range(count):
            path = self.root / f'private-character-{index}.png'
            path.write_bytes(picture((index * 30, 20, 200)))
            paths.append(path)
        return paths

    def assert_unknown(self, session):
        with self.assertRaises(transport.OpenAIImageError):
            self.run_image(session)
        self.assertEqual(self.receipt()['status'], 'unknown')
        self.assertEqual(len(session.calls), 1)
        self.assertFalse(self.output.exists())
        second = Session()
        with self.assertRaisesRegex(transport.OpenAIImageError, 'NO_RETRY'):
            self.run_image(second)
        self.assertEqual(second.calls, [])
        self.assertFalse(self.output.with_name(self.output.name + '.openai-image.lock').exists())

    def test_generations_uses_json_and_preserves_base_path_without_private_ledger_content(self):
        session = Session()
        result = self.run_image(session)
        self.assertEqual(result, self.output)
        self.assertEqual(len(session.calls), 1)
        url, arguments = session.calls[0]
        self.assertEqual(url, self.config['base_url'] + '/images/generations')
        sent_prompt = arguments['json']['prompt']
        self.assertTrue(sent_prompt.startswith('输出画布要求：横纵比例为 16:9，最终图片尺寸为 1536x864 像素。'))
        self.assertIn('参考图片的原始长宽比不代表输出画幅', sent_prompt)
        self.assertIn('不添加边框', sent_prompt)
        self.assertTrue(sent_prompt.endswith('\n\n' + self.macro['image_prompt']))
        self.assertEqual(arguments['json'], {'model': 'compatible-model', 'prompt': sent_prompt,
                                            'size': '1536x864', 'n': 1, 'response_format': 'b64_json'})
        self.assertNotIn('files', arguments)
        self.assertNotIn('data', arguments)
        self.assertFalse(arguments['allow_redirects'])
        self.assertTrue(arguments['stream'])
        self.assertEqual(arguments['timeout'], (10, 600))
        self.assertFalse(session.trust_env)
        self.assertTrue(session.closed)
        self.assertTrue(session.result.closed)
        with Image.open(self.output) as image:
            self.assertEqual(image.format, 'JPEG')
            self.assertEqual(image.size, (32, 18))
            image.load()
        receipt = self.receipt()
        self.assertEqual(receipt['status'], 'completed')
        self.assertEqual(receipt['request']['prompt_sha256'], hashlib.sha256(sent_prompt.encode('utf-8')).hexdigest())
        self.assertEqual(receipt['request']['source_prompt_sha256'],
                         hashlib.sha256(self.macro['image_prompt'].encode('utf-8')).hexdigest())
        self.assertEqual(receipt['output_sha256'], hashlib.sha256(self.output.read_bytes()).hexdigest())
        saved = transport.ledger_path(self.output).read_text()
        for private in (self.config['api_key'], self.config['base_url'], self.macro['image_prompt'], str(self.root)):
            self.assertNotIn(private, saved)

    def test_real_http_edits_preserves_two_reference_parts_order_and_exact_bytes(self):
        server = HTTPFixture()
        self.addCleanup(server.close)
        paths = self.references()
        macro = {**self.macro, 'reference_image_paths': paths}
        config = {**self.config, 'base_url': server.base}
        transport.render_openai_image(macro, config, self.output)
        self.assertEqual(len(server.calls), 1)
        call = server.calls[0]
        self.assertEqual(call['path'], '/private/v1/images/edits')
        self.assertTrue(call['type'].startswith('multipart/form-data;'))
        self.assertEqual(call['body'].count(b'name="image[]"'), 2)
        self.assertLess(call['body'].index(paths[0].read_bytes()), call['body'].index(paths[1].read_bytes()))
        self.assertIn(b'1536x864', call['body'])
        self.assertIn(b'b64_json', call['body'])
        self.assertNotIn(paths[0].name.encode(), call['body'])
        self.assertEqual(self.receipt()['request']['reference_sha256'],
                         [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths])
        self.assertEqual([row['index'] for row in self.receipt()['references']], [1, 2])

    def test_real_http_zero_references_uses_generations_json(self):
        server = HTTPFixture()
        self.addCleanup(server.close)
        transport.render_openai_image(self.macro, {**self.config, 'base_url': server.base}, self.output)
        self.assertEqual(len(server.calls), 1)
        self.assertEqual(server.calls[0]['path'], '/private/v1/images/generations')
        self.assertEqual(server.calls[0]['type'], 'application/json')
        self.assertEqual(json.loads(server.calls[0]['body'])['n'], 1)

    def test_actual_redirect_is_not_followed_or_retried(self):
        server = HTTPFixture(status=302)
        self.addCleanup(server.close)
        config = {**self.config, 'base_url': server.base}
        for _ in range(2):
            with self.assertRaises(transport.OpenAIImageError):
                transport.render_openai_image(self.macro, config, self.output)
        self.assertEqual([call['path'] for call in server.calls], ['/private/v1/images/generations'])
        self.assertEqual(self.receipt()['status'], 'unknown')

    def test_dispatching_receipt_exists_before_post_and_reference_bytes_are_snapshotted(self):
        paths = self.references()
        originals = [path.read_bytes() for path in paths]
        session = Session()
        def before_post():
            self.assertEqual(self.receipt()['status'], 'dispatching')
            for path in paths:
                path.write_bytes(picture('yellow'))
        session.before_post = before_post
        self.run_image(session, macro={**self.macro, 'reference_image_paths': paths})
        sent = session.calls[0][1]['files']
        self.assertEqual([row[0] for row in sent], ['image[]', 'image[]'])
        self.assertEqual([row[1][1] for row in sent], originals)
        self.assertEqual(self.receipt()['request']['reference_sha256'], [hashlib.sha256(raw).hexdigest() for raw in originals])

    def test_exact_duplicate_references_are_preserved_without_silent_deduplication(self):
        path = self.references(1)[0]
        session = Session()
        self.run_image(session, macro={**self.macro, 'reference_image_paths': [path, path]})
        self.assertEqual(len(session.calls[0][1]['files']), 2)
        self.assertEqual(self.receipt()['references'][0]['sha256'], self.receipt()['references'][1]['sha256'])

    def test_reference_validation_never_silently_falls_back_to_generation(self):
        path = self.references(1)[0]
        cases = [([path] * 5, self.config), ([self.root / 'missing.png'], self.config),
                 ([''], self.config), ([path], {**self.config, 'reference_images': False}),
                 ('not-a-list', self.config)]
        for paths, config in cases:
            session = Session()
            with self.assertRaises(transport.OpenAIImageError):
                self.run_image(session, macro={**self.macro, 'reference_image_paths': paths}, config=config)
            self.assertEqual(session.calls, [])
        path.write_bytes(b'HTML is not an image')
        session = Session()
        with self.assertRaises(transport.OpenAIImageError):
            self.run_image(session, macro={**self.macro, 'reference_image_paths': [path]})
        self.assertEqual(session.calls, [])
        session = Session()
        with self.assertRaisesRegex(transport.OpenAIImageError, 'NOT_RESOLVED'):
            self.run_image(session, macro={**self.macro, 'reference_image_ids': ['identity']})
        self.assertEqual(session.calls, [])

    def test_disabled_native_references_explains_no_submission_and_creates_no_receipt(self):
        session = Session()
        with self.assertRaises(transport.OpenAIImageError) as caught:
            self.run_image(session, macro={**self.macro, 'reference_image_paths': self.references(1)},
                           config={**self.config, 'reference_images': False})
        self.assertIn('未启用参考图', str(caught.exception))
        self.assertIn('尚未提交', str(caught.exception))
        self.assertIn('OPENAI_IMAGE_REFERENCES_DISABLED', str(caught.exception))
        self.assertEqual(session.calls, [])
        self.assertFalse(transport.ledger_path(self.output).exists())

    def test_errors_have_chinese_action_and_receipts_keep_only_safe_codes(self):
        with self.assertRaises(transport.OpenAIImageError) as caught:
            self.run_image(Session(requests.Timeout('private-provider-body')))
        self.assertIn('尚未确认', str(caught.exception))
        self.assertIn('不会自动重试', str(caught.exception))
        self.assertNotIn('private-provider-body', str(caught.exception))
        self.assertEqual(self.receipt()['error_code'], 'TRANSPORT_OR_LOCAL_FAILURE')
        rejected = self.root / 'rejected-message.jpg'
        with self.assertRaises(transport.OpenAIImageError) as caught:
            self.run_image(Session(Response(status=403)), output=rejected)
        self.assertIn('明确拒绝', str(caught.exception))
        self.assertIn('OPENAI_IMAGE_HTTP_403_REJECTED_NO_RETRY', str(caught.exception))
        unsafe = transport.OpenAIImageError('https://private-provider.example secret-key')
        self.assertNotIn('private-provider', str(unsafe))

    def test_dispatch_failures_record_stable_categories_without_exception_details_or_retry(self):
        private = 'secret-fixture-key https://private-provider.example/private/v1 private-body'
        cases = [
            (requests.exceptions.ConnectTimeout, 'connect_timeout'),
            (requests.exceptions.ReadTimeout, 'read_timeout'),
            (requests.exceptions.Timeout, 'timeout'),
            (requests.exceptions.SSLError, 'tls_error'),
            (requests.exceptions.ProxyError, 'proxy_error'),
            (requests.exceptions.ConnectionError, 'connection_error'),
            (requests.exceptions.ChunkedEncodingError, 'chunked_response_error'),
            (requests.exceptions.ContentDecodingError, 'content_decoding_error'),
            (requests.exceptions.RequestException, 'request_error'),
            (OSError, 'local_io_error'),
            (type('SecretProviderException', (Exception,), {}), 'unexpected_error'),
        ]
        for index, (kind, category) in enumerate(cases):
            with self.subTest(kind=kind):
                output = self.root / f'category-{index}.jpg'
                session = Session(kind(private))
                with self.assertRaises(transport.OpenAIImageError) as caught:
                    self.run_image(session, output=output)
                receipt = self.receipt(output)
                self.assertEqual(receipt['status'], 'unknown')
                self.assertEqual(receipt['error_code'], 'TRANSPORT_OR_LOCAL_FAILURE')
                self.assertEqual(receipt['failure_stage'], 'request_dispatch')
                self.assertEqual(receipt['exception_category'], category)
                self.assertNotIn('provider_request_id', receipt)
                before = transport.ledger_path(output).read_bytes()
                for forbidden in ('secret-fixture-key', 'private-provider', 'private-body', 'SecretProviderException'):
                    self.assertNotIn(forbidden, before.decode())
                    self.assertNotIn(forbidden, str(caught.exception))
                with self.assertRaisesRegex(transport.OpenAIImageError, 'PREVIOUS_OUTCOME_UNKNOWN'):
                    self.run_image(session, output=output)
                self.assertEqual(len(session.calls), 1)
                self.assertEqual(transport.ledger_path(output).read_bytes(), before)

    def test_stream_failure_preserves_safe_request_id_and_response_body_stage(self):
        response = Response(headers={'X-Request-ID': 'req_0123456789abcdef', 'Authorization': 'private-header'})
        response.iter_content = Mock(side_effect=requests.exceptions.ReadTimeout('private streamed body'))
        self.assert_unknown(Session(response))
        receipt = self.receipt()
        self.assertEqual(receipt['failure_stage'], 'response_body')
        self.assertEqual(receipt['exception_category'], 'read_timeout')
        self.assertEqual(receipt['provider_request_id'], 'req_0123456789abcdef')
        self.assertEqual(receipt['http_status'], 200)
        saved = transport.ledger_path(self.output).read_text()
        self.assertNotIn('private-header', saved)
        self.assertNotIn('private streamed body', saved)

    def test_http_diagnostics_preserve_rejected_and_unknown_outcomes_without_reading_body(self):
        for status in (403, 429, 500, 502):
            with self.subTest(status=status):
                output = self.root / f'http-diagnostic-{status}.jpg'
                response = Response(status=status, headers={'request-id': '12345678-1234-1234-1234-123456789abc'})
                response.iter_content = Mock(side_effect=AssertionError('must not inspect rejected response body'))
                session = Session(response)
                with self.assertRaises(transport.OpenAIImageError):
                    self.run_image(session, output=output)
                receipt = self.receipt(output)
                self.assertEqual(receipt['status'], 'rejected' if status in (403, 429) else 'unknown')
                self.assertEqual(receipt['failure_stage'], 'response_headers')
                self.assertEqual(receipt['exception_category'], 'http_status')
                self.assertEqual(receipt['provider_request_id'], '12345678-1234-1234-1234-123456789abc')
                response.iter_content.assert_not_called()
                self.assertEqual(len(session.calls), 1)

    def test_decode_failure_diagnostics_do_not_store_sensitive_response(self):
        response = Response(content=b'{"error":"secret-fixture-key private response"}',
                            headers={'x-request-id': 'abcdef0123456789abcdef0123456789'})
        self.assert_unknown(Session(response))
        receipt = self.receipt()
        self.assertEqual(receipt['failure_stage'], 'response_decode')
        self.assertEqual(receipt['exception_category'], 'validation_error')
        self.assertEqual(receipt['provider_request_id'], 'abcdef0123456789abcdef0123456789')
        saved = transport.ledger_path(self.output).read_text()
        self.assertNotIn('private response', saved)
        self.assertNotIn(self.config['api_key'], saved)

    def test_request_id_allowlist_rejects_urls_tokens_text_and_unrecognized_headers(self):
        cases = [
            {'x-request-id': 'https://private-provider.example/private/v1'},
            {'x-request-id': 'secret-fixture-key'},
            {'x-request-id': 'Bearer private-token'},
            {'x-request-id': 'req_0123456789abcdef\r\nAuthorization: private-token'},
            {'x-request-id': 'req_' + 'a' * 200},
            {'x-request-id': {'nested': 'private-data'}},
            {'trace-id': 'req_0123456789abcdef'},
            {'Authorization': 'req_0123456789abcdef'},
        ]
        for index, headers in enumerate(cases):
            with self.subTest(headers=headers):
                output = self.root / f'unsafe-request-id-{index}.jpg'
                with self.assertRaises(transport.OpenAIImageError):
                    self.run_image(Session(Response(status=500, headers=headers)), output=output)
                self.assertNotIn('provider_request_id', self.receipt(output))
                saved = transport.ledger_path(output).read_text()
                for forbidden in ('private-provider', 'secret-fixture-key', 'private-token', 'private-data',
                                  'Authorization', 'req_0123456789abcdef'):
                    self.assertNotIn(forbidden, saved)

    def test_request_id_never_preserves_an_echoed_configured_api_key(self):
        key = 'req_0123456789abcdef'
        with self.assertRaises(transport.OpenAIImageError):
            self.run_image(Session(Response(status=500, headers={'x-request-id': key})),
                           config={**self.config, 'api_key': key})
        self.assertNotIn('provider_request_id', self.receipt())
        self.assertNotIn(key, transport.ledger_path(self.output).read_text())

    def test_old_unknown_receipt_without_diagnostics_is_not_rewritten_or_retried(self):
        with self.assertRaises(transport.OpenAIImageError):
            self.run_image(Session(requests.Timeout('private error')))
        old = self.receipt()
        old.pop('failure_stage')
        old.pop('exception_category')
        transport.ledger_path(self.output).write_text(json.dumps(old), encoding='utf-8')
        before = transport.ledger_path(self.output).read_bytes()
        session = Session(Response(headers={'x-request-id': 'req_0123456789abcdef'}))
        with self.assertRaisesRegex(transport.OpenAIImageError, 'PREVIOUS_OUTCOME_UNKNOWN'):
            self.run_image(session)
        self.assertEqual(session.calls, [])
        self.assertEqual(transport.ledger_path(self.output).read_bytes(), before)

    def test_supported_ratios_send_compatible_sizes_and_never_crop_returned_pixels(self):
        for index, (ratio, size) in enumerate(transport.SIZES.items()):
            with self.subTest(ratio=ratio):
                response = Response(payload(picture(size=size)))
                session = Session(response)
                output = self.root / f'ratio-{index}.jpg'
                self.run_image(session, output=output, config={**self.config, 'ratio': ratio})
                self.assertEqual(session.calls[0][1]['json']['size'], f'{size[0]}x{size[1]}')
                self.assertIn(f'横纵比例为 {ratio}，最终图片尺寸为 {size[0]}x{size[1]} 像素',
                              session.calls[0][1]['json']['prompt'])
                with Image.open(output) as image:
                    self.assertEqual(image.size, size)

    def test_edits_prefix_uses_target_ratio_and_keeps_original_prompt_exactly(self):
        path = self.references(1)[0]
        source = '  原始镜头要求\n  保持场景、动作和身份。\n'
        session = Session(Response(payload(picture(size=(18, 32)))))
        self.run_image(session, macro={**self.macro, 'image_prompt': source, 'reference_image_paths': [path]},
                       config={**self.config, 'ratio': '9:16'})
        sent = session.calls[0][1]['data']
        self.assertEqual(sent['size'], '864x1536')
        self.assertTrue(sent['prompt'].startswith('输出画布要求：横纵比例为 9:16，最终图片尺寸为 864x1536 像素。'))
        self.assertTrue(sent['prompt'].endswith('\n\n' + source))
        self.assertEqual(self.receipt()['request']['prompt_sha256'],
                         hashlib.sha256(sent['prompt'].encode('utf-8')).hexdigest())
        self.assertEqual(self.receipt()['request']['source_prompt_sha256'],
                         hashlib.sha256(source.encode('utf-8')).hexdigest())

    def test_missing_ratio_high_resolution_or_foreign_endpoint_stops_before_http(self):
        for changes in ({'ratio': None}, {'ratio': 'auto'}, {'resolution': '2k'}, {'resolution': '4k'},
                        {'endpoint': 'https://other.example/generate'}, {'endpoint': '//other.example/generate'},
                        {'endpoint': '/images/../generate'}, {'endpoint': '/images/generate?token=secret'},
                        {'base_url': 'https://name:password@provider.example'},
                        {'base_url': 'https://provider.example?token=secret'}):
            with self.subTest(changes=changes):
                session = Session()
                with self.assertRaises(transport.OpenAIImageError):
                    self.run_image(session, config={**self.config, **changes})
                self.assertEqual(session.calls, [])

    def test_network_failures_are_unknown_and_do_not_leak_exception_secrets(self):
        for index, failure in enumerate((requests.Timeout, requests.ConnectionError, RuntimeError)):
            with self.subTest(failure=failure):
                output = self.root / f'network-{index}.jpg'
                session = Session(failure('https://private.example secret-key private-prompt'))
                with self.assertRaises(transport.OpenAIImageError) as caught:
                    self.run_image(session, output=output)
                self.assertNotIn('private.example', str(caught.exception))
                self.assertNotIn('secret-key', transport.ledger_path(output).read_text())
                self.assertEqual(self.receipt(output)['status'], 'unknown')
                self.assertEqual(len(session.calls), 1)
                second = Session()
                with self.assertRaisesRegex(transport.OpenAIImageError, 'NO_RETRY'):
                    self.run_image(second, output=output)
                self.assertEqual(second.calls, [])

    def test_5xx_is_unknown_and_no_fallback_to_generations(self):
        paths = self.references()
        session = Session(Response(status=503))
        macro = {**self.macro, 'reference_image_paths': paths}
        for _ in range(2):
            with self.assertRaises(transport.OpenAIImageError):
                self.run_image(session, macro=macro)
        self.assertEqual(len(session.calls), 1)
        self.assertTrue(session.calls[0][0].endswith('/images/edits'))
        self.assertEqual(self.receipt()['status'], 'unknown')

    def test_explicit_rejected_codes_allow_a_later_manual_call_without_internal_retry(self):
        for status in (400, 401, 403, 429):
            with self.subTest(status=status):
                output = self.root / f'rejected-{status}.jpg'
                session = Session(Response(status=status, content=b'secret-provider-body'))
                with self.assertRaisesRegex(transport.OpenAIImageError, f'HTTP_{status}'):
                    self.run_image(session, output=output)
                self.assertEqual(len(session.calls), 1)
                self.assertEqual(self.receipt(output)['status'], 'rejected')
                self.assertNotIn('secret-provider-body', transport.ledger_path(output).read_text())
                next_session = Session()
                self.run_image(next_session, output=output)
                self.assertEqual(len(next_session.calls), 1)
                self.assertEqual(self.receipt(output)['status'], 'completed')

    def test_bad_json_html_urls_multiple_images_and_invalid_base64_become_unknown(self):
        cases = [Response(content=b'<html>upstream failure</html>'), Response(content=b'{bad json'),
                 Response({'data': [{'url': 'https://private.example/image.png'}]}),
                 Response({'data': []}), Response({'data': payload()['data'] * 2}),
                 Response({'data': [{'b64_json': '%%%invalid%%%'}]}),
                 Response(payload(b'<html>not pixels</html>'))]
        for index, response in enumerate(cases):
            with self.subTest(index=index):
                output = self.root / f'bad-{index}.jpg'
                session = Session(response)
                with self.assertRaises(transport.OpenAIImageError):
                    self.run_image(session, output=output)
                self.assertEqual(self.receipt(output)['status'], 'unknown')
                self.assertEqual(len(session.calls), 1)
                self.assertFalse(output.exists())

    def test_decodable_png_jpeg_and_webp_are_fully_loaded_and_saved_as_jpeg(self):
        for index, image_format in enumerate(('PNG', 'JPEG', 'WEBP')):
            with self.subTest(format=image_format):
                output = self.root / f'format-{index}.jpg'
                self.run_image(Session(Response(payload(picture(format=image_format)))), output=output)
                self.assertEqual(self.receipt(output)['response_format'], image_format)
                with Image.open(output) as image:
                    image.load()
                    self.assertEqual(image.format, 'JPEG')

    def test_gif_and_truncated_pixels_remain_unknown_without_output_or_candidate(self):
        for index, content in enumerate((picture(format='GIF'), picture()[:40])):
            output = self.root / f'invalid-image-{index}.jpg'
            with self.assertRaises(transport.OpenAIImageError):
                self.run_image(Session(Response(payload(content))), output=output)
            self.assertEqual(self.receipt(output)['status'], 'unknown')
            self.assertFalse(output.exists())
        self.assertEqual(list(self.root.glob('rejected-response-*')), [])

    def test_wrong_aspect_retains_verified_original_candidate_and_real_dimensions(self):
        for index, image_format in enumerate(('PNG', 'JPEG', 'WEBP')):
            with self.subTest(image_format=image_format):
                raw = picture(size=(32, 32), format=image_format)
                output = self.root / f'aspect-{index}.jpg'
                session = Session(Response(payload(raw)))
                with self.assertRaisesRegex(transport.OpenAIImageError, 'ASPECT_MISMATCH') as caught:
                    self.run_image(session, output=output)
                self.assertIn('32×32', str(caught.exception))
                self.assertIn('16:9', str(caught.exception))
                self.assertIn('已隔离保留候选', str(caught.exception))
                self.assertIn('不会自动重试', str(caught.exception))
                receipt = self.receipt(output)
                self.assertEqual(receipt['status'], 'response_rejected')
                self.assertEqual(receipt['failure_stage'], 'response_validation')
                self.assertEqual(receipt['exception_category'], 'validation_error')
                self.assertEqual(receipt['http_status'], 200)
                self.assertEqual((receipt['width'], receipt['height']), (32, 32))
                self.assertEqual(receipt['response_format'], image_format)
                self.assertEqual(receipt['candidate_format'], image_format)
                self.assertEqual(receipt['candidate_bytes'], len(raw))
                self.assertEqual(receipt['candidate_sha256'], hashlib.sha256(raw).hexdigest())
                self.assertEqual(receipt['response_image_sha256'], hashlib.sha256(raw).hexdigest())
                self.assertTrue(receipt['candidate_saved'])
                self.assertGreater(receipt['aspect_relative_error'], 0.01)
                candidate = self.root / receipt['candidate_file']
                self.assertEqual(candidate.name, 'rejected-response-' + receipt['attempt_id'] + transport.FORMATS[image_format][1])
                self.assertEqual(candidate.read_bytes(), raw)
                with Image.open(candidate) as image:
                    image.load()
                    self.assertEqual(image.size, (32, 32))
                    self.assertEqual(image.format, image_format)
                self.assertFalse(output.exists())
                self.assertNotIn('output_sha256', receipt)
                self.assertEqual(len(session.calls), 1)
                saved_receipt = transport.ledger_path(output).read_text()
                for private in (self.config['api_key'], self.config['base_url'], self.macro['image_prompt'],
                                str(self.root), base64.b64encode(raw).decode()):
                    self.assertNotIn(private, saved_receipt)

    def test_response_rejected_blocks_same_and_changed_request_without_modifying_candidate(self):
        with self.assertRaises(transport.OpenAIImageError):
            self.run_image(Session(Response(payload(picture(size=(32, 32))))))
        original_receipt = transport.ledger_path(self.output).read_bytes()
        candidate = self.root / self.receipt()['candidate_file']
        original_image = candidate.read_bytes()
        for config, macro in ((self.config, self.macro),
                              ({**self.config, 'ratio': '1:1'}, {**self.macro, 'image_prompt': 'changed prompt'})):
            session = Session()
            with self.assertRaisesRegex(transport.OpenAIImageError, 'PREVIOUS_RESPONSE_REJECTED') as caught:
                self.run_image(session, config=config, macro=macro)
            self.assertEqual(session.calls, [])
            self.assertIn('32×32', str(caught.exception))
            self.assertIn('16:9', str(caught.exception))
        self.assertEqual(transport.ledger_path(self.output).read_bytes(), original_receipt)
        self.assertEqual(candidate.read_bytes(), original_image)
        self.assertFalse(self.output.exists())

    def test_failed_candidate_save_keeps_received_diagnostics_and_blocks_duplicate_post(self):
        session = Session(Response(payload(picture(size=(32, 32)))))
        with patch.object(transport, '_save_candidate', side_effect=OSError('secret filesystem path')):
            with self.assertRaisesRegex(transport.OpenAIImageError, 'CANDIDATE_SAVE_FAILED') as caught:
                self.run_image(session)
        self.assertIn('32×32', str(caught.exception))
        self.assertIn('16:9', str(caught.exception))
        self.assertIn('未能完整保存', str(caught.exception))
        self.assertNotIn('secret filesystem path', str(caught.exception))
        receipt = self.receipt()
        self.assertEqual(receipt['status'], 'response_rejected')
        self.assertEqual(receipt['failure_stage'], 'candidate_write')
        self.assertEqual(receipt['exception_category'], 'local_io_error')
        self.assertFalse(receipt['candidate_saved'])
        self.assertEqual((receipt['width'], receipt['height']), (32, 32))
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob('rejected-response-*')), [])
        second = Session()
        with self.assertRaisesRegex(transport.OpenAIImageError, 'PREVIOUS_RESPONSE_REJECTED'):
            self.run_image(second, macro={**self.macro, 'image_prompt': 'changed'})
        self.assertEqual(second.calls, [])
        self.assertEqual(len(session.calls), 1)

    def test_candidate_write_is_exclusive_and_does_not_replace_an_existing_image(self):
        candidate = self.root / 'already-present.png'
        candidate.write_bytes(b'previous review evidence')
        with self.assertRaises(FileExistsError):
            transport._save_candidate(candidate, picture(size=(32, 32)))
        self.assertEqual(candidate.read_bytes(), b'previous review evidence')

    def test_failed_candidate_flush_removes_only_its_own_incomplete_file(self):
        candidate = self.root / 'new-candidate.png'
        other = self.root / 'other-evidence.png'
        other.write_bytes(b'keep existing evidence')
        with patch.object(transport.os, 'fsync', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                transport._save_candidate(candidate, picture(size=(32, 32)))
        self.assertFalse(candidate.exists())
        self.assertEqual(other.read_bytes(), b'keep existing evidence')

    def test_failed_response_rejected_receipt_write_keeps_dispatching_guard(self):
        actual = transport._write_ledger
        def fail_received(path, value):
            if value['status'] == 'response_rejected':
                raise transport.OpenAIImageError('OPENAI_IMAGE_LEDGER_WRITE_FAILED_NO_RETRY')
            return actual(path, value)
        with patch.object(transport, '_write_ledger', side_effect=fail_received):
            with self.assertRaisesRegex(transport.OpenAIImageError, 'LEDGER_WRITE_FAILED'):
                self.run_image(Session(Response(payload(picture(size=(32, 32))))))
        self.assertEqual(self.receipt()['status'], 'dispatching')
        self.assertFalse(self.output.exists())
        second = Session()
        with self.assertRaisesRegex(transport.OpenAIImageError, 'NO_RETRY'):
            self.run_image(second)
        self.assertEqual(second.calls, [])

    def test_historical_unknown_is_not_rewritten_as_a_received_response(self):
        with self.assertRaises(transport.OpenAIImageError):
            self.run_image(Session(requests.Timeout('unknown paid result')))
        before = transport.ledger_path(self.output).read_bytes()
        second = Session(Response(payload(picture(size=(32, 32)))))
        with self.assertRaisesRegex(transport.OpenAIImageError, 'PREVIOUS_OUTCOME_UNKNOWN'):
            self.run_image(second, macro={**self.macro, 'image_prompt': 'new request'})
        self.assertEqual(second.calls, [])
        self.assertEqual(transport.ledger_path(self.output).read_bytes(), before)
        self.assertNotIn('width', self.receipt())
        self.assertEqual(list(self.root.glob('rejected-response-*')), [])

    def test_verify_only_jpeg_is_rejected_when_full_pixel_load_fails(self):
        truncated = picture(format='JPEG')[:-2]
        with Image.open(io.BytesIO(truncated)) as image:
            image.verify()  # PIL's JPEG verify accepts this incomplete stream.
        session = Session(Response(payload(truncated)))
        self.assert_unknown(session)

    def test_content_length_and_actual_stream_size_are_both_limited(self):
        cases = [Response(headers={'Content-Length': '999999999'}),
                 Response(headers={}, chunks=[b'a' * 600, b'b' * 600]),
                 Response(headers={'Content-Length': 'not-a-number'})]
        for index, response in enumerate(cases):
            output = self.root / f'large-{index}.jpg'
            with patch.object(transport, 'MAX_RESPONSE_BYTES', 1000), self.assertRaises(transport.OpenAIImageError):
                self.run_image(Session(response), output=output)
            self.assertEqual(self.receipt(output)['status'], 'unknown')
            self.assertFalse(output.exists())

    def test_pixel_dimensions_are_bounded_before_load(self):
        with patch.object(transport, 'MAX_PIXELS', 100), self.assertRaisesRegex(transport.OpenAIImageError, 'DIMENSIONS'):
            self.run_image()
        self.assertEqual(self.receipt()['status'], 'unknown')

    def test_completed_request_and_bytes_are_reused_without_network(self):
        self.run_image()
        old = self.output.read_bytes()
        session = Session()
        self.assertEqual(self.run_image(session), self.output)
        self.assertEqual(session.calls, [])
        self.assertEqual(self.output.read_bytes(), old)

    def test_changed_prompt_model_url_or_reference_order_requires_new_output(self):
        paths = self.references()
        macro = {**self.macro, 'reference_image_paths': paths}
        self.run_image(macro=macro)
        original = self.output.read_bytes()
        cases = [({**macro, 'image_prompt': 'changed'}, self.config),
                 (macro, {**self.config, 'model': 'different-model'}),
                 (macro, {**self.config, 'base_url': 'https://another.example/v1'}),
                 ({**macro, 'reference_image_paths': paths[::-1]}, self.config)]
        for changed_macro, config in cases:
            session = Session()
            with self.assertRaisesRegex(transport.OpenAIImageError, 'NEW_OUTPUT_VERSION'):
                self.run_image(session, macro=changed_macro, config=config)
            self.assertEqual(session.calls, [])
            self.assertEqual(self.output.read_bytes(), original)

    def test_changed_reference_bytes_and_changed_output_are_not_reused(self):
        path = self.references(1)[0]
        macro = {**self.macro, 'reference_image_paths': [path]}
        self.run_image(macro=macro)
        path.write_bytes(picture('yellow'))
        session = Session()
        with self.assertRaisesRegex(transport.OpenAIImageError, 'NEW_OUTPUT_VERSION'):
            self.run_image(session, macro=macro)
        self.assertEqual(session.calls, [])
        output = self.root / 'tampered.jpg'
        self.run_image(output=output)
        output.write_bytes(b'externally overwritten')
        with self.assertRaisesRegex(transport.OpenAIImageError, 'SAVED_OUTPUT_CHANGED'):
            self.run_image(session, output=output)
        self.assertEqual(session.calls, [])

    def test_unknown_receipt_blocks_even_changed_input_and_preserves_old_output(self):
        session = Session(requests.Timeout('private details'))
        with self.assertRaises(transport.OpenAIImageError):
            self.run_image(session)
        self.output.write_bytes(b'previous output kept')
        second = Session()
        with self.assertRaisesRegex(transport.OpenAIImageError, 'PREVIOUS_OUTCOME_UNKNOWN'):
            self.run_image(second, macro={**self.macro, 'image_prompt': 'new prompt'})
        self.assertEqual(second.calls, [])
        self.assertEqual(self.output.read_bytes(), b'previous output kept')

    def test_orphaned_output_and_corrupt_receipt_never_trigger_paid_post(self):
        self.output.write_bytes(b'old output')
        session = Session()
        with self.assertRaisesRegex(transport.OpenAIImageError, 'WITHOUT_RECEIPT'):
            self.run_image(session)
        transport.ledger_path(self.output).write_text('{broken')
        with self.assertRaisesRegex(transport.OpenAIImageError, 'LEDGER_INVALID'):
            self.run_image(session)
        self.assertEqual(session.calls, [])
        self.assertEqual(self.output.read_bytes(), b'old output')

    def test_incomplete_rejected_receipt_is_not_permission_to_replay(self):
        transport.ledger_path(self.output).write_text(json.dumps({'version': 1, 'status': 'rejected'}))
        session = Session()
        with self.assertRaisesRegex(transport.OpenAIImageError, 'LEDGER_INVALID'):
            self.run_image(session)
        self.assertEqual(session.calls, [])

    def test_missing_completed_output_does_not_trigger_another_paid_request(self):
        self.run_image()
        self.output.unlink()
        session = Session()
        with self.assertRaisesRegex(transport.OpenAIImageError, 'SAVED_OUTPUT_UNREADABLE'):
            self.run_image(session)
        self.assertEqual(session.calls, [])

    def test_failed_completed_receipt_write_leaves_dispatching_to_block_replay(self):
        actual = transport._write_ledger
        def fail_final(path, value):
            if value['status'] == 'completed':
                raise transport.OpenAIImageError('OPENAI_IMAGE_LEDGER_WRITE_FAILED_NO_RETRY')
            return actual(path, value)
        with patch.object(transport, '_write_ledger', side_effect=fail_final):
            with self.assertRaises(transport.OpenAIImageError):
                self.run_image()
        self.assertTrue(self.output.exists())
        self.assertEqual(self.receipt()['status'], 'dispatching')
        session = Session()
        with self.assertRaisesRegex(transport.OpenAIImageError, 'NO_RETRY'):
            self.run_image(session)
        self.assertEqual(session.calls, [])

    def test_failed_output_write_preserves_unknown_and_never_marks_completed(self):
        actual = transport._atomic_bytes
        def fail_output(path, content):
            if path == self.output:
                raise OSError('private output write failure')
            return actual(path, content)
        with patch.object(transport, '_atomic_bytes', side_effect=fail_output):
            self.assert_unknown(Session())
        self.assertEqual(self.receipt()['failure_stage'], 'output_write')
        self.assertEqual(self.receipt()['exception_category'], 'local_io_error')
        self.assertNotIn('private output write failure', transport.ledger_path(self.output).read_text())

    def test_ledger_write_failure_stops_before_post(self):
        session = Session()
        with patch.object(transport, '_atomic_bytes', side_effect=OSError('secret filesystem path')):
            with self.assertRaisesRegex(transport.OpenAIImageError, 'LEDGER_WRITE_FAILED'):
                self.run_image(session)
        self.assertEqual(session.calls, [])

    def test_output_that_appears_during_request_is_preserved(self):
        session = Session()
        session.before_post = lambda: self.output.write_bytes(b'existing manual version')
        with self.assertRaisesRegex(transport.OpenAIImageError, 'NO_OVERWRITE'):
            self.run_image(session)
        self.assertEqual(self.output.read_bytes(), b'existing manual version')
        self.assertEqual(self.receipt()['status'], 'unknown')

    def test_foreign_lock_is_never_removed_and_concurrent_call_cannot_post(self):
        lock = self.output.with_name(self.output.name + '.openai-image.lock')
        lock.write_text('belongs to another worker')
        session = Session()
        with self.assertRaisesRegex(transport.OpenAIImageError, 'BUSY'):
            self.run_image(session)
        self.assertEqual(session.calls, [])
        self.assertEqual(lock.read_text(), 'belongs to another worker')
        lock.unlink()
        inner = Session()
        def reenter():
            with self.assertRaisesRegex(transport.OpenAIImageError, 'BUSY'):
                self.run_image(inner)
            self.assertTrue(lock.exists())
        session.before_post = reenter
        self.run_image(session)
        self.assertEqual(inner.calls, [])
        self.assertEqual(len(session.calls), 1)
        self.assertFalse(lock.exists())

    def test_process_interruption_leaves_dispatching_receipt_that_blocks_replay(self):
        session = Session()
        def interrupt():
            raise KeyboardInterrupt()
        session.before_post = interrupt
        with self.assertRaises(KeyboardInterrupt):
            self.run_image(session)
        self.assertEqual(self.receipt()['status'], 'dispatching')
        other = Session()
        with self.assertRaisesRegex(transport.OpenAIImageError, 'NO_RETRY'):
            self.run_image(other)
        self.assertEqual(other.calls, [])


if __name__ == '__main__':
    unittest.main()
