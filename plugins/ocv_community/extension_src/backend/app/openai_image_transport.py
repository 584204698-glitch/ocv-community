"""Single-dispatch Images-compatible transport with a durable local receipt.

The multipart fields and sizes are a compatibility contract derived from the
working relay, not a claim that every OpenAI-compatible provider supports them.
Reference identity/ownership is resolved by callers; this adapter preserves the
exact ordered bytes and never substitutes text-only generation after a failure.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Callable
from urllib.parse import urlsplit
import uuid
import warnings

from PIL import Image
import requests


MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_PIXELS = 32 * 1024 * 1024
MAX_SIDE = 8192
MAX_LEDGER_BYTES = 64 * 1024
# Reference-image edits may spend several minutes rendering before headers or
# the next body chunk arrive. This is an idle-read limit, not a retry policy;
# an expired request still keeps its unknown receipt and is never re-POSTed.
IMAGE_READ_TIMEOUT_SECONDS = 600
SIZES = {
    '16:9': (1536, 864), '9:16': (864, 1536), '1:1': (1024, 1024),
    '3:2': (1536, 1024), '2:3': (1024, 1536), '2:1': (1536, 768),
    '3:4': (1152, 1536), '21:9': (1536, 656),
}
FORMATS = {'PNG': ('image/png', '.png'), 'JPEG': ('image/jpeg', '.jpg'),
           'WEBP': ('image/webp', '.webp')}
REJECTED_HTTP = {400, 401, 403, 429}
REQUEST_ID_PATTERN = re.compile(
    r'(?:req_[A-Za-z0-9]{8,80}|[A-Fa-f0-9]{32,64}|'
    r'[A-Fa-f0-9]{8}-(?:[A-Fa-f0-9]{4}-){3}[A-Fa-f0-9]{12})'
)


class OpenAIImageError(RuntimeError):
    """Deliberately outside the async provider's automatic retry hierarchy."""

    def __init__(self, code, *, actual_size=None, target_ratio=None, candidate_saved=False):
        # Only program-owned codes may reach the UI/receipt. Never carry a
        # provider body, URL or exception message through this error type.
        self.code = code if isinstance(code, str) and re.fullmatch(r'OPENAI_IMAGE_[A-Z0-9_]+', code) else 'OPENAI_IMAGE_FAILURE_NO_RETRY'
        short = self.code.removeprefix('OPENAI_IMAGE_')
        if short in {'RESPONSE_ASPECT_MISMATCH_NO_CROP', 'PREVIOUS_RESPONSE_REJECTED_NO_RETRY',
                     'RESPONSE_CANDIDATE_SAVE_FAILED_NO_RETRY'}:
            measured = ''
            if (isinstance(actual_size, (tuple, list)) and len(actual_size) == 2
                    and all(type(value) is int and 0 < value <= MAX_SIDE for value in actual_size)
                    and isinstance(target_ratio, str) and target_ratio in SIZES):
                measured = f'实际图片为 {actual_size[0]}×{actual_size[1]}，目标比例为 {target_ratio}。'
            candidate = ('已隔离保留候选图片，请核查后决定是否采用。' if candidate_saved is True else
                         '候选图片未能完整保存，请先核对生成记录和服务端结果。')
            message = f'接口已返回有效图片，但未通过画幅验收，未采用为正式输出。{measured}{candidate}系统不会自动重试或再次提交同一输出。'
        elif short == 'REFERENCES_DISABLED':
            message = '所选图像配置未启用参考图，本次尚未提交；请启用参考图能力或更换配置。'
        elif short == 'REFERENCE_IDS_NOT_RESOLVED':
            message = '参考图尚未解析完成，本次尚未提交；请重新选择并保存参考图。'
        elif short == 'RESOLUTION_UNSUPPORTED_ONLY_1K':
            message = '此兼容接口目前仅支持 1K 请求，本次尚未提交；请改选 1K。'
        elif short == 'RATIO_UNSUPPORTED':
            message = '尚未选择有效画幅比例，本次尚未提交；请重新选择比例。'
        elif re.fullmatch(r'HTTP_(400|401|403|429)_REJECTED_NO_RETRY', short):
            message = '接口明确拒绝本次请求；请检查配置、模型权限或限流情况后再手动重试。'
        elif short == 'OUTPUT_BUSY_OR_INTERRUPTED_NO_RETRY':
            message = '这张图片正在处理或上次处理被中断；请先核对任务状态，系统不会重复提交。'
        elif short == 'PREVIOUS_OUTCOME_UNKNOWN_NO_RETRY':
            message = '上次提交结果尚未确认；请先核对服务端记录和扣费，系统不会再次提交同一输出。'
        elif short.startswith(('REQUEST_CHANGED_', 'EXISTING_OUTPUT_', 'SAVED_OUTPUT_')):
            message = '已有图片或生成记录与本次请求不一致；请先核对记录，重绘时使用新版本，原文件不会被覆盖。'
        elif short == 'OUTPUT_APPEARED_DURING_REQUEST_NO_OVERWRITE':
            message = '生成期间发现目标位置已有图片，已保留原文件；请先核对本次服务端结果，系统不会自动重试。'
        elif short.startswith('LEDGER_'):
            message = '生成记录无法安全读取或保存，已停止操作；请先核对已有图片和接口记录，系统不会自动重试。'
        elif short.startswith(('REFERENCE_', 'PROMPT_', 'CONFIGURATION_', 'PROTOCOL_',
                               'BASE_URL_', 'ENDPOINT_', 'REQUEST_INVALID', 'OUTPUT_MUST_')):
            message = '设置、参考图或输出位置无效，本次尚未提交；请检查配置及参考文件后再操作。'
        elif short.startswith(('RESPONSE_', 'JPEG_')):
            message = '接口返回的图片或数据不符合要求；请先核对服务端结果，系统不会自动重试或覆盖旧图。'
        else:
            message = '本次生成结果尚未确认；请先核对服务端记录和扣费，系统不会自动重试。'
        super().__init__(f'{message}（{self.code}）')


def ledger_path(output: Path) -> Path:
    return output.with_name(output.name + '.openai-image.json')


def _error(code, **diagnostics):
    return OpenAIImageError('OPENAI_IMAGE_' + code, **diagnostics)


def _exception_category(exc):
    """Classify by known types, never by provider text or dynamic class names."""
    for kind, category in (
        (requests.exceptions.ConnectTimeout, 'connect_timeout'),
        (requests.exceptions.ReadTimeout, 'read_timeout'),
        (requests.exceptions.Timeout, 'timeout'),
        (requests.exceptions.SSLError, 'tls_error'),
        (requests.exceptions.ProxyError, 'proxy_error'),
        (requests.exceptions.ChunkedEncodingError, 'chunked_response_error'),
        (requests.exceptions.ContentDecodingError, 'content_decoding_error'),
        (requests.exceptions.ConnectionError, 'connection_error'),
        (requests.exceptions.RequestException, 'request_error'),
        (OSError, 'local_io_error'),
    ):
        if isinstance(exc, kind):
            return category
    if isinstance(exc, OpenAIImageError):
        return 'http_status' if exc.code.startswith('OPENAI_IMAGE_HTTP_') else 'validation_error'
    return 'unexpected_error'


def _provider_request_id(response, api_key):
    """Retain only an explicitly named, narrowly formatted correlation ID."""
    try:
        for name, value in response.headers.items():
            if (isinstance(name, str) and name.lower() in ('x-request-id', 'request-id')
                    and isinstance(value, str) and value != api_key
                    and REQUEST_ID_PATTERN.fullmatch(value)):
                return value
    except Exception:
        pass  # Optional diagnostics must not change submission/result semantics.
    return None


def _failure_details(exc, stage, request_id=None):
    details = {'failure_stage': stage, 'exception_category': _exception_category(exc)}
    if request_id is not None:
        details['provider_request_id'] = request_id
    return details


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode('utf-8')


def _atomic_bytes(path, content):
    """Same-directory replace; a failed write never truncates an old receipt."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.' + path.name + '.', suffix='.tmp',
                                         delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != 'nt':
            # Persist the rename too before a paid POST or completed receipt.
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _write_ledger(path, value):
    try:
        _atomic_bytes(path, _json_bytes(value))
    except OSError:
        raise _error('LEDGER_WRITE_FAILED_NO_RETRY') from None


def _read_bounded(path, maximum, code):
    try:
        with path.open('rb') as handle:
            content = handle.read(maximum + 1)
    except OSError:
        raise _error(code + '_UNREADABLE') from None
    if not content or len(content) > maximum:
        raise _error(code + '_SIZE_INVALID')
    return content


def _image(content, *, reference=False):
    code = 'REFERENCE' if reference else 'RESPONSE'
    if not content or len(content) > MAX_IMAGE_BYTES:
        raise _error(code + '_IMAGE_SIZE_INVALID')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as image:
                image_format = str(image.format or '').upper()
                if image_format not in FORMATS:
                    raise _error(code + '_FORMAT_UNSUPPORTED')
                width, height = image.size
                if (width < 1 or height < 1 or max(width, height) > MAX_SIDE
                        or width * height > MAX_PIXELS or getattr(image, 'n_frames', 1) != 1):
                    raise _error(code + '_DIMENSIONS_INVALID')
                image.verify()
            with Image.open(io.BytesIO(content)) as image:
                image.load()  # verify() alone does not decode compressed pixels.
                if reference:
                    converted = None
                elif image.mode in ('RGBA', 'LA') or 'transparency' in image.info:
                    rgba = image.convert('RGBA')
                    converted = Image.new('RGB', rgba.size, 'white')
                    converted.paste(rgba, mask=rgba.getchannel('A'))
                    rgba.close()
                else:
                    converted = image.convert('RGB')
        return converted, image_format, width, height
    except OpenAIImageError:
        raise
    except Exception:
        raise _error(code + '_IMAGE_INVALID') from None


def _settings(config, has_references):
    if not isinstance(config, dict) or config.get('protocol') != 'openai_images':
        raise _error('PROTOCOL_INVALID')
    model = config.get('model') or config.get('model_id')
    key = config.get('api_key')
    base = config.get('base_url')
    if (not isinstance(model, str) or not model.strip() or len(model) > 256
            or not isinstance(key, str) or not key.strip() or '\r' in key or '\n' in key
            or not isinstance(base, str) or len(base) > 2048):
        raise _error('CONFIGURATION_INVALID')
    base = base.strip().rstrip('/')
    try:
        parsed = urlsplit(base)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment or '\\' in base
                or any(ord(char) < 33 for char in base)):
            raise ValueError
        parsed.port  # Reject malformed ports locally before a possible POST.
    except ValueError:
        raise _error('BASE_URL_INVALID') from None
    endpoint = config.get('reference_endpoint' if has_references else 'endpoint')
    if endpoint is None:
        endpoint = '/images/edits' if has_references else '/images/generations'
    if not isinstance(endpoint, str) or not endpoint.startswith('/') or endpoint.startswith('//'):
        raise _error('ENDPOINT_INVALID')
    parsed_endpoint = urlsplit(endpoint)
    if (len(endpoint) > 512 or parsed_endpoint.scheme or parsed_endpoint.netloc or parsed_endpoint.query
            or parsed_endpoint.fragment or '\\' in endpoint or '%' in endpoint
            or any(part in ('.', '..') for part in endpoint.split('/'))
            or any(ord(char) < 33 for char in endpoint)):
        raise _error('ENDPOINT_INVALID')
    ratio = config.get('ratio')
    resolution = config.get('resolution')
    if not isinstance(ratio, str) or ratio not in SIZES:
        raise _error('RATIO_UNSUPPORTED')
    if resolution != '1k':
        raise _error('RESOLUTION_UNSUPPORTED_ONLY_1K')
    if has_references and config.get('reference_images') is False:
        raise _error('REFERENCES_DISABLED')
    width, height = SIZES[ratio]
    return model.strip(), key.strip(), base + endpoint, ratio, resolution, f'{width}x{height}'


def _prepare(macro, config):
    if not isinstance(macro, dict):
        raise _error('REQUEST_INVALID')
    source_prompt = macro.get('image_prompt')
    if not isinstance(source_prompt, str) or not source_prompt.strip() or len(source_prompt) > 100000:
        raise _error('PROMPT_INVALID')
    paths = macro.get('reference_image_paths', [])
    if not isinstance(paths, (list, tuple)) or len(paths) > 4:
        raise _error('REFERENCE_LIST_INVALID_MAX_4')
    if not paths and macro.get('reference_image_ids'):
        raise _error('REFERENCE_IDS_NOT_RESOLVED')
    settings = _settings(config, bool(paths))
    references = []
    for index, raw_path in enumerate(paths):
        if not isinstance(raw_path, (str, Path)) or not str(raw_path).strip():
            raise _error('REFERENCE_PATH_INVALID')
        content = _read_bounded(Path(raw_path), MAX_IMAGE_BYTES, 'REFERENCE')
        _unused, image_format, width, height = _image(content, reference=True)
        references.append({'content': content, 'format': image_format, 'width': width,
                           'height': height, 'sha256': _sha(content), 'index': index + 1})
    model, _key, url, ratio, resolution, size = settings
    prompt = (f'输出画布要求：横纵比例为 {ratio}，最终图片尺寸为 {size} 像素。'
              '参考图片的原始长宽比不代表输出画幅；请按指定画幅构图并生成完整画面，'
              '不添加边框、额外留白或黑边来凑比例。\n\n' + source_prompt)
    request = {'version': 1, 'protocol': 'openai_images', 'model': model,
               'url_sha256': _sha(url.encode('utf-8')), 'prompt_sha256': _sha(prompt.encode('utf-8')),
               'source_prompt_sha256': _sha(source_prompt.encode('utf-8')),
               'reference_sha256': [row['sha256'] for row in references],
               'ratio': ratio, 'resolution': resolution, 'size': size,
               'route': 'edits' if references else 'generations'}
    return prompt, references, settings, request, _sha(_json_bytes(request))


def _existing(output, receipt, request_sha256):
    if not receipt.exists():
        if output.exists():
            raise _error('EXISTING_OUTPUT_WITHOUT_RECEIPT_USE_NEW_VERSION')
        return None
    try:
        previous = json.loads(_read_bounded(receipt, MAX_LEDGER_BYTES, 'LEDGER'))
        if (not isinstance(previous, dict) or previous.get('version') != 1
                or previous.get('status') not in {'dispatching', 'unknown', 'rejected', 'completed', 'response_rejected'}
                or not isinstance(previous.get('request'), dict)
                or _sha(_json_bytes(previous['request'])) != previous.get('request_sha256')
                or not isinstance(previous.get('attempt_id'), str)
                or not re.fullmatch(r'[a-f0-9]{32}', previous['attempt_id'])):
            raise ValueError
        status = previous['status']
        if status in ('dispatching', 'unknown'):
            raise _error('PREVIOUS_OUTCOME_UNKNOWN_NO_RETRY')
        if status == 'response_rejected':
            raise _error('PREVIOUS_RESPONSE_REJECTED_NO_RETRY',
                         actual_size=(previous.get('width'), previous.get('height')),
                         target_ratio=previous['request'].get('ratio'),
                         candidate_saved=previous.get('candidate_saved') is True)
        if status == 'completed':
            if previous.get('request_sha256') != request_sha256:
                raise _error('REQUEST_CHANGED_USE_NEW_OUTPUT_VERSION')
            content = _read_bounded(output, MAX_IMAGE_BYTES, 'SAVED_OUTPUT')
            if _sha(content) != previous.get('output_sha256'):
                raise _error('SAVED_OUTPUT_CHANGED_USE_NEW_VERSION')
            return output
        if previous.get('http_status') not in REJECTED_HTTP:
            raise ValueError
        if output.exists():
            raise _error('EXISTING_OUTPUT_USE_NEW_VERSION')
        return None  # Explicitly rejected request permits a later manual call.
    except OpenAIImageError:
        raise
    except (ValueError, TypeError, OSError):
        raise _error('LEDGER_INVALID_NO_RETRY') from None


def _response_bytes(response):
    length = response.headers.get('Content-Length')
    if length is not None:
        try:
            if int(length) < 0 or int(length) > MAX_RESPONSE_BYTES:
                raise ValueError
        except (TypeError, ValueError):
            raise _error('RESPONSE_SIZE_INVALID') from None
    chunks, total = [], 0
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise _error('RESPONSE_SIZE_INVALID')
        chunks.append(chunk)
    if not total:
        raise _error('RESPONSE_EMPTY')
    return b''.join(chunks)


def _decode_response(content):
    try:
        payload = json.loads(content)
        if (not isinstance(payload, dict) or payload.get('error') or not isinstance(payload.get('data'), list)
                or len(payload['data']) != 1 or not isinstance(payload['data'][0], dict)):
            raise ValueError
        encoded = payload['data'][0].get('b64_json')
        if not isinstance(encoded, str) or not encoded or len(encoded) > ((MAX_IMAGE_BYTES + 2) // 3) * 4:
            raise ValueError
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise _error('RESPONSE_EXPECTED_ONE_BASE64_IMAGE') from None
    image, image_format, width, height = _image(raw)
    return image, raw, {'width': width, 'height': height, 'response_format': image_format,
                        'response_image_sha256': _sha(raw)}


def _save_candidate(path, content):
    """Keep verified source bytes separately; never replace another candidate."""
    created = False
    try:
        with path.open('xb') as handle:
            created = True
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != 'nt':
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    except Exception:
        if created:
            path.unlink(missing_ok=True)
        raise


def _reject_aspect_response(output, receipt, state, raw, metadata, ratio, deviation, request_id=None):
    candidate_name = 'rejected-response-' + state['attempt_id'] + FORMATS[metadata['response_format']][1]
    state.update(status='response_rejected', finished_unix=time.time(), **metadata,
                 error_code='OPENAI_IMAGE_RESPONSE_ASPECT_MISMATCH_NO_CROP',
                 failure_stage='response_validation', exception_category='validation_error',
                 aspect_relative_error=deviation, candidate_file=candidate_name,
                 candidate_format=metadata['response_format'], candidate_sha256=_sha(raw),
                 candidate_bytes=len(raw), candidate_saved=False)
    if request_id is not None:
        state['provider_request_id'] = request_id
    # Preserve the known response before attempting a diagnostic-only write.
    _write_ledger(receipt, state)
    try:
        _save_candidate(output.with_name(candidate_name), raw)
    except Exception as exc:
        state['error_code'] = 'OPENAI_IMAGE_RESPONSE_CANDIDATE_SAVE_FAILED_NO_RETRY'
        state.update(_failure_details(exc, 'candidate_write', request_id))
        _write_ledger(receipt, state)
        raise _error('RESPONSE_CANDIDATE_SAVE_FAILED_NO_RETRY',
                     actual_size=(metadata['width'], metadata['height']), target_ratio=ratio) from None
    state['candidate_saved'] = True
    _write_ledger(receipt, state)
    raise _error('RESPONSE_ASPECT_MISMATCH_NO_CROP', actual_size=(metadata['width'], metadata['height']),
                 target_ratio=ratio, candidate_saved=True)


def _close(resource):
    if resource is not None:
        try:
            resource.close()
        except Exception:
            pass


def render_openai_image(macro, config, output: Path, *, session_factory: Callable | None = None) -> Path:
    """Submit at most once; uncertain results block this output on later calls.

    A completed receipt can reuse only the identical request and saved bytes.
    Every redraw needs a new output version. Secrets, absolute paths, prompts,
    response bodies and provider exception strings are excluded from the receipt.
    """
    lock_fd = None
    lock = None
    session = response = None
    dispatched = False
    receipt = state = None
    stage = 'request_validation'
    provider_request_id = None
    try:
        output = Path(output)
        if output.suffix.lower() not in ('.jpg', '.jpeg'):
            raise _error('OUTPUT_MUST_BE_JPEG')
        prompt, references, settings, request, request_sha256 = _prepare(macro, config)
        model, key, url, ratio, _resolution, size = settings
        output.parent.mkdir(parents=True, exist_ok=True)
        receipt = ledger_path(output)
        lock = output.with_name(output.name + '.openai-image.lock')
        try:
            lock_fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise _error('OUTPUT_BUSY_OR_INTERRUPTED_NO_RETRY') from None
        reused = _existing(output, receipt, request_sha256)
        if reused is not None:
            return reused
        state = {'version': 1, 'status': 'dispatching', 'attempt_id': uuid.uuid4().hex,
                 'request_sha256': request_sha256, 'request': request,
                 'references': [{key: row[key] for key in ('index', 'sha256', 'format', 'width', 'height')}
                                for row in references],
                 'started_unix': time.time()}
        session = (session_factory or requests.Session)()
        session.trust_env = False
        if isinstance(session, requests.sessions.Session):
            # Never inherit an injected requests.Session's automatic POST retry.
            session.mount('http://', requests.adapters.HTTPAdapter(max_retries=0))
            session.mount('https://', requests.adapters.HTTPAdapter(max_retries=0))
        headers = {'Authorization': 'Bearer ' + key}
        arguments = {'headers': headers, 'timeout': (10, IMAGE_READ_TIMEOUT_SECONDS), 'allow_redirects': False, 'stream': True}
        fields = {'model': model, 'prompt': prompt, 'size': size, 'n': 1, 'response_format': 'b64_json'}
        if references:
            arguments['data'] = {**fields, 'n': '1'}
            # Snapshot bytes, synthetic filenames, and repeated keys preserve
            # slot order without exposing private source filenames upstream.
            arguments['files'] = [('image[]', (f'reference-{row["index"]}' + FORMATS[row['format']][1],
                                               row['content'], FORMATS[row['format']][0])) for row in references]
        else:
            arguments['json'] = fields
        _write_ledger(receipt, state)  # Must succeed before any expensive POST.
        stage = 'request_dispatch'
        dispatched = True
        response = session.post(url, **arguments)
        stage = 'response_headers'
        provider_request_id = _provider_request_id(response, key)
        status = int(response.status_code)
        state['http_status'] = status
        if status in REJECTED_HTTP:
            rejected = _error(f'HTTP_{status}_REJECTED_NO_RETRY')
            state.update(status='rejected', finished_unix=time.time(), error_code=f'HTTP_{status}',
                         **_failure_details(rejected, stage, provider_request_id))
            _write_ledger(receipt, state)
            raise rejected
        if status != 200:
            raise _error(f'HTTP_{status}_OUTCOME_UNKNOWN_NO_RETRY')
        stage = 'response_body'
        content = _response_bytes(response)
        stage = 'response_decode'
        image, raw, metadata = _decode_response(content)
        try:
            stage = 'response_validation'
            a, b = (int(part) for part in ratio.split(':'))
            deviation = abs(metadata['width'] / metadata['height'] - a / b) / (a / b)
            if deviation > 0.01:
                _reject_aspect_response(output, receipt, state, raw, metadata, ratio, deviation,
                                        provider_request_id)
            stage = 'image_encode'
            buffer = io.BytesIO()
            image.save(buffer, format='JPEG', quality=95)
            saved = buffer.getvalue()
            if len(saved) > MAX_IMAGE_BYTES:
                raise _error('JPEG_OUTPUT_TOO_LARGE')
            metadata.update(output_format='JPEG', output_sha256=_sha(saved), output_bytes=len(saved))
        finally:
            image.close()
        stage = 'output_write'
        if output.exists():
            raise _error('OUTPUT_APPEARED_DURING_REQUEST_NO_OVERWRITE')
        _atomic_bytes(output, saved)
        state.update(status='completed', finished_unix=time.time(), **metadata)
        stage = 'completed_receipt'
        _write_ledger(receipt, state)
        return output
    except Exception as exc:
        if dispatched and state is not None and state.get('status') not in ('rejected', 'completed', 'response_rejected'):
            state.update(status='unknown', finished_unix=time.time(),
                         error_code=exc.code if isinstance(exc, OpenAIImageError) else 'TRANSPORT_OR_LOCAL_FAILURE',
                         **_failure_details(exc, stage, provider_request_id))
            try:
                _write_ledger(receipt, state)
            except OpenAIImageError:
                pass  # The durable dispatching receipt still blocks another POST.
        if isinstance(exc, OpenAIImageError):
            raise
        raise _error('TRANSPORT_OR_LOCAL_FAILURE_NO_RETRY') from None
    finally:
        _close(response)
        _close(session)
        if lock_fd is not None:
            try:
                os.close(lock_fd)
            except OSError:
                pass
            finally:
                # Only the call that created this exclusive lock may remove it.
                try:
                    lock.unlink(missing_ok=True)
                except OSError:
                    pass
