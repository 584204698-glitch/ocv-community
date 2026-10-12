"""Account-scoped local voice design. Saved references are immutable identities."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .auth import require_user
from . import qwen3_local
from .voice_design_agent import ProposalRequest

router = APIRouter(prefix='/api/voice-design')
ROOT = Path(__file__).resolve().parents[2] / 'workspace/voice_design'
LOCK = threading.RLock()


class DesignRequest(BaseModel):
    request_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    name: str = Field(min_length=1, max_length=60)
    description: str = Field(min_length=5, max_length=1200)
    reference_text: str = Field(min_length=10, max_length=300)
    project_id: str | None = Field(default=None, pattern=r'^[a-f0-9]{32}$')
    character_id: str | None = Field(default=None, pattern=r'^pc_[a-f0-9]{32}$')


class LibraryUpdate(BaseModel):
    favorite: bool | None = None
    archived: bool | None = None


def folder(user: int, identity: str) -> Path:
    if type(user) is not int or user < 1 or not re.fullmatch('[a-f0-9]{32}', identity):
        raise HTTPException(404, '音色不存在')
    return ROOT / str(user) / identity


def read(path: Path):
    if not (path / 'record.json').is_file():
        raise HTTPException(404, '音色不存在')
    return json.loads((path / 'record.json').read_text(encoding='utf-8'))


def save(path: Path, record: dict):
    temporary = path / (uuid.uuid4().hex + '.tmp')
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.chmod(0o600)
    temporary.replace(path / 'record.json')


def process_identity(pid: int):
    """Linux process identity prevents a reused PID from keeping a dead task active."""
    try:
        fields = (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()
        return fields[19] if fields[0] not in ('Z', 'X') else None
    except FileNotFoundError:
        return None


def reconciled(path: Path):
    with LOCK:
        record = read(path)
        if record['status'] != 'generating' or not Path('/proc').is_dir():
            return record
        supervisor = record.get('_supervisor')
        lost = (process_identity(supervisor['pid']) != supervisor['start_ticks']) if supervisor else (
            time.time() - record['created_at'] > 120)
        if lost:
            latest = read(path)
            if latest != record:
                return latest
            record.update(status='failed', error='声音生成进程已中断，原记录已保留。请新建音色后重试。',
                          updated_at=time.time())
            save(path, record)
        return record


def public(record):
    result = {key: value for key, value in record.items() if not key.startswith('_')}
    if result.get('audio'):
        result['audio_url'] = '/api/voice-design/' + result['id'] + '/audio'
    result['voice_id'] = 'qwen3:' + result['id']
    return result


def launch(owner, identity):
    path = folder(owner, identity)
    try:
        with (path / 'supervisor.log').open('xb') as log:
            process = subprocess.Popen([sys.executable, '-B', '-m', 'backend.app.voice_design_supervisor',
                                        '--owner', str(owner), '--identity', identity],
                                       cwd=qwen3_local.PROJECT_ROOT, stdout=log, stderr=subprocess.STDOUT)
            code = process.wait()
        with LOCK:
            record = read(path)
            if record['status'] == 'generating':
                record.update(status='failed', error='本次音色生成未完成，请检查模型服务和共享计算队列。',
                              _supervisor_exit=code, updated_at=time.time())
                save(path, record)
    except Exception:
        with LOCK:
            record = read(path)
            record.update(status='failed', error='音色生成服务启动失败，原记录已保留。', updated_at=time.time())
            save(path, record)


@router.get('')
def list_voices(request: Request):
    owner = int(require_user(request)['id'])
    rows = []
    for file in (ROOT / str(owner)).glob('*/record.json'):
        rows.append(public(reconciled(file.parent)))
    return {'health': qwen3_local.health(), 'voices': sorted(rows, key=lambda r: r['created_at'], reverse=True)}


@router.post('', status_code=202)
def design(data: DesignRequest, request: Request):
    owner = int(require_user(request)['id'])
    content = {key: value.strip() for key, value in data.model_dump(exclude_none=True).items()}
    if not content['name'] or len(content['description']) < 5 or len(content['reference_text']) < 10:
        raise HTTPException(400, '请填写音色名称、声音描述和试听文本')
    path = folder(owner, data.request_id)
    source = None
    if bool(data.project_id) != bool(data.character_id):
        raise HTTPException(400, '项目和角色须同时提供')
    if data.project_id:
        from .voice_design_agent import project_context
        try:
            source = project_context(owner, data.project_id, data.character_id)[0]['source']
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    with LOCK:
        if path.exists():
            record = reconciled(path)
            if record['_request'] != content:
                raise HTTPException(409, '本次请求已存在且内容不同，请刷新后创建新的音色')
            return public(record)
        if not qwen3_local.health()['ready']:
            raise HTTPException(503, '本地 Qwen3-TTS 尚未安装完整')
        for old in (ROOT / str(owner)).glob('*/record.json'):
            if reconciled(old.parent)['status'] == 'generating':
                raise HTTPException(409, '已有音色正在生成，请等它完成')
        path.mkdir(parents=True, exist_ok=False)
        record = {'id': data.request_id, 'name': content['name'], 'description': content['description'],
                  'reference_text': content['reference_text'], 'status': 'generating',
                  'created_at': time.time(), 'updated_at': time.time(), 'error': '', '_request': content,
                  'engine': 'Qwen3-TTS-12Hz-1.7B-VoiceDesign'}
        if source:
            record['source'] = source
        save(path, record)
        threading.Thread(target=launch, args=(owner, data.request_id), daemon=True).start()
        return public(record)


@router.post('/suggest')
def suggest(data: ProposalRequest, request: Request):
    from .voice_design_agent import propose
    owner = int(require_user(request)['id'])
    try:
        return propose(owner, data)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, '音色设计师暂时无法完成方案，请检查语言接口后重试；现有声音未改变。') from exc


@router.patch('/{identity}/library')
def update_library(identity: str, data: LibraryUpdate, request: Request):
    path = folder(int(require_user(request)['id']), identity)
    with LOCK:
        record = read(path)
        if record['status'] == 'generating':
            raise HTTPException(409, '声音正在生成，完成后可收藏或归档')
        record.update(data.model_dump(exclude_none=True))
        save(path, record)
    return public(record)


@router.get('/{identity}')
def get_voice(identity: str, request: Request):
    return public(reconciled(folder(int(require_user(request)['id']), identity)))


@router.post('/{identity}/adopt')
def adopt(identity: str, request: Request):
    path = folder(int(require_user(request)['id']), identity)
    with LOCK:
        record = read(path)
        if record['status'] not in ('ready', 'saved'):
            raise HTTPException(409, '音色尚未生成完成')
        record.update(status='saved', updated_at=time.time())
        save(path, record)
    return public(record)


@router.get('/{identity}/audio')
def audio(identity: str, request: Request):
    path = folder(int(require_user(request)['id']), identity)
    record = read(path)
    file = path / 'design/chunk-0001.wav'
    if record['status'] not in ('ready', 'saved') or not file.is_file():
        raise HTTPException(404, '试听音频尚未生成')
    return FileResponse(file, media_type='audio/wav')
