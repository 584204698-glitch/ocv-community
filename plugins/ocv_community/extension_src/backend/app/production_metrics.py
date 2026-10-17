"""Private, request-local usage events. Never store prompts, keys or provider bodies."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
import json
import os
import re
import threading
import time
import uuid

_SCOPE = ContextVar('ocv_production_metrics', default=None)
_CALL = ContextVar('ocv_language_metrics_call', default=None)
_LOCK = threading.RLock()
FILE = 'diagnostics/model-usage.jsonl'


@contextmanager
def production_scope(path=None, stage=None):
    prior = _SCOPE.get()
    if path is None and prior is None:
        yield
        return
    scope = dict(prior or {})
    if path is not None and (not prior or Path(path) != prior['path']):
        scope = dict(path=Path(path), stage='other', hits=set())
    if stage:
        scope['stage'] = stage
    token = _SCOPE.set(scope)
    try:
        yield
    finally:
        _SCOPE.reset(token)


def _emit(event):
    scope = _SCOPE.get()
    if not scope:
        return
    target = scope['path'] / FILE
    try:
        with _LOCK:
            target.parent.mkdir(parents=True, exist_ok=True)
            data = (json.dumps({'at': time.time(), 'stage': scope['stage'], **event},
                               ensure_ascii=True, separators=(',', ':')) + '\n').encode()
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
    except OSError:
        # Accounting cannot invalidate a paid generation or change its result.
        pass


def cache_hit(key):
    scope = _SCOPE.get()
    if not scope:
        return
    with _LOCK:
        identity = (scope['stage'], str(key))
        if identity in scope['hits']:
            return
        scope['hits'].add(identity)
        _emit({'event': 'cache_hit'})


def phase_call(stage, function, *args, **kwargs):
    with production_scope(stage=stage):
        return function(*args, **kwargs)


def metered_post(*args, **kwargs):
    import requests
    call = _CALL.get()
    if call is not None:
        call['http_attempts'] += 1
    return requests.post(*args, **kwargs)


def provider_usage(data):
    call = _CALL.get()
    if call is None or not isinstance(data, dict):
        return
    raw = data.get('usage') or data.get('usageMetadata')
    if not isinstance(raw, dict):
        return
    names = {'input_tokens': ('prompt_tokens', 'input_tokens', 'promptTokenCount'),
             'output_tokens': ('completion_tokens', 'output_tokens', 'candidatesTokenCount'),
             'total_tokens': ('total_tokens', 'totalTokenCount')}
    usage = {}
    for key, aliases in names.items():
        for alias in aliases:
            if type(raw.get(alias)) is int and raw[alias] >= 0:
                usage[key] = raw[alias]
                break
    if usage:
        call['usage'] = usage


def observe_language_call(function):
    @wraps(function)
    def observed(*args, **kwargs):
        if not _SCOPE.get():
            return function(*args, **kwargs)
        from .gemini_client import _provider, language_model
        # Model labels only: reject URL-like/unexpected values instead of echoing configuration.
        model = str(language_model())
        model = model if len(model) <= 160 and re.fullmatch(r'[A-Za-z0-9_.:/-]+', model) and '://' not in model else 'configured'
        call = dict(request_id=uuid.uuid4().hex, http_attempts=0, usage=None)
        started = time.monotonic()
        token = _CALL.set(call)
        _emit(dict(event='request_started', request_id=call['request_id'], model=model,
                   provider=_provider(), input_characters=sum(len(str(kwargs.get(k) or ''))
                   for k in ('system_prompt', 'user_prompt'))))
        outcome = 'failed'
        output_length = 0
        try:
            result = function(*args, **kwargs)
            output_length = len(result) if isinstance(result, str) else 0
            outcome = 'completed'
            return result
        finally:
            _emit(dict(event='request_finished', **call, outcome=outcome,
                       elapsed_seconds=round(time.monotonic() - started, 3),
                       output_characters=output_length))
            _CALL.reset(token)
    return observed


def summary(path):
    target = Path(path) / FILE
    events, truncated = [], False
    try:
        with target.open('rb') as stream:
            size = stream.seek(0, os.SEEK_END)
            stream.seek(max(0, size - 4 * 1024 * 1024))
            if stream.tell():
                stream.readline()
                truncated = True
            for line in stream:
                try:
                    events.append(json.loads(line))
                except (ValueError, UnicodeError):
                    pass  # A writer can be appending its final line.
    except FileNotFoundError:
        pass
    requests, stages, hits = {}, {}, {}
    for event in events:
        stage = event.get('stage', 'other')
        if event.get('event') == 'cache_hit':
            hits[stage] = hits.get(stage, 0) + 1
        elif event.get('request_id'):
            requests.setdefault(event['request_id'], {}).update(event)
    for row in requests.values():
        stage = row['stage']
        item = stages.setdefault(stage, dict(stage=stage, requests=0, completed=0, failed=0,
            running=0, elapsed_seconds=0, http_attempts=0, input_tokens=0, output_tokens=0,
            token_reports=0, models=[]))
        item['requests'] += 1
        outcome = row.get('outcome') or 'running'
        item[outcome] += 1
        item['elapsed_seconds'] += row.get('elapsed_seconds', 0)
        item['http_attempts'] += row.get('http_attempts', 0)
        if row.get('usage'):
            item['token_reports'] += 1
            for key in ('input_tokens', 'output_tokens'):
                item[key] += row['usage'].get(key, 0)
        if row.get('model') and row['model'] not in item['models']:
            item['models'].append(row['model'])
    for stage, count in hits.items():
        stages.setdefault(stage, dict(stage=stage, requests=0, completed=0, failed=0, running=0,
            elapsed_seconds=0, http_attempts=0, input_tokens=0, output_tokens=0, token_reports=0, models=[]))
    for stage, item in stages.items():
        item['cache_hits'] = hits.get(stage, 0)
        item['elapsed_seconds'] = round(item['elapsed_seconds'], 3)
    keys = ('requests', 'completed', 'failed', 'running', 'http_attempts', 'token_reports', 'input_tokens', 'output_tokens', 'cache_hits')
    return dict(version=1, stages=list(stages.values()), **{key: sum(s[key] for s in stages.values()) for key in keys},
                elapsed_seconds=round(sum(s['elapsed_seconds'] for s in stages.values()), 3),
                cost=None, currency=None, history_truncated=truncated,
                scope='observed_language_requests',
                note='仅统计升级后已接入阶段的语言/视觉模型请求；不含图片、配音和视频费用。缺少供应商用量时不估算扣费。')
