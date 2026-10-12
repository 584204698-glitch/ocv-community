"""Local Qwen3 voice configuration and guarded, offline synthesis batches."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys

from .config import load_project_env
from .tts_gpu_guard import guarded_tts_command

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def configuration():
    load_project_env()
    root = Path(os.environ.get('QWEN3_TTS_ROOT', str(PROJECT_ROOT / 'runtime/qwen3-tts'))).absolute()
    default_python = root / ('venv/Scripts/python.exe' if sys.platform == 'win32' else 'venv/bin/python')
    python = Path(os.environ.get('QWEN3_TTS_PYTHON', str(default_python))).absolute()
    models = root / 'models'
    return {'root': root, 'python': python,
            'design': models / 'Qwen3-TTS-12Hz-1.7B-VoiceDesign',
            'clone': models / 'Qwen3-TTS-12Hz-1.7B-Base'}


def health():
    config = configuration()
    files = ('config.json', 'model.safetensors', 'speech_tokenizer/model.safetensors')
    missing = [f'{mode}/{file}' for mode in ('design', 'clone') for file in files
               if not (config[mode] / file).is_file()]
    if not config['python'].is_file():
        missing.append('python')
    if not (config['root'] / 'download-verified.json').is_file():
        missing.append('verified_model_manifest')
    return {'ready': not missing, 'missing': missing, 'engine': 'qwen3_local'}


def run_batch(path: Path, *, mode: str, texts: list[str], owner: int, task_id: str,
              description: str = '', reference: Path | None = None, reference_text: str = '',
              on_line=print):
    if not health()['ready']:
        raise RuntimeError('本地 Qwen3-TTS 模型或运行环境尚未安装完整')
    if mode not in ('design', 'clone') or type(owner) is not int or owner < 1:
        raise ValueError('Invalid voice synthesis mode or owner')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', task_id):
        raise ValueError('Invalid voice task ID')
    config = configuration()
    path.mkdir(parents=True, exist_ok=False)
    payload = {'mode': mode, 'model_dir': str(config[mode]), 'texts': texts,
               'description': description, 'seed': int.from_bytes(os.urandom(4), 'big')}
    if mode == 'clone':
        if reference is None or not reference.is_file() or not reference_text.strip():
            raise ValueError('固定声音需要已保存的参考音频及其原文')
        shutil.copyfile(reference, path / 'reference.wav')
        payload.update(reference_text=reference_text,
                       reference_sha256=hashlib.sha256((path / 'reference.wav').read_bytes()).hexdigest())
    request_file = path / 'request.json'
    request_file.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    worker = Path(__file__).with_name('qwen3_runner.py')
    command = [str(config['python']), '-I', '-B', str(worker), '--request', str(request_file)]
    command, guarded = guarded_tts_command(command, owner=owner, task_id=task_id)
    env = {**os.environ, 'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1', 'PYTHONUNBUFFERED': '1',
           'TOKENIZERS_PARALLELISM': 'false'}
    if guarded:
        # Shared cancellation supervision is already covered by the IndexTTS contract tests.
        # Both the module1 CLI and the voice-design supervisor call this on their main thread.
        from module1_agent_director import _run_guarded_and_stream
        code = _run_guarded_and_stream(command, cwd=path, env=env, on_line=on_line)
    else:
        import subprocess
        with subprocess.Popen(command, cwd=path, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace') as process:
            for line in process.stdout:
                on_line(line.rstrip())
            code = process.wait()
    if code:
        raise RuntimeError('Qwen3-TTS 未完成生成；请检查计算队列和本次日志，不会自动重复提交')
    report = json.loads((path / 'result.json').read_text(encoding='utf-8'))
    if len(report['outputs']) != len(texts):
        raise RuntimeError('Qwen3-TTS 返回的配音分段数量不完整')
    for index, item in enumerate(report['outputs'], 1):
        expected = f'chunk-{index:04d}.wav'
        if item['file'] != expected or hashlib.sha256((path / expected).read_bytes()).hexdigest() != item['sha256']:
            raise RuntimeError('Qwen3-TTS 音频校验失败')
    return report


def resolve_saved_voice(voice_id: str, user_id: int):
    # Only server-owned saved voices from the requesting account may enter a job.
    from .voice_design import folder, read
    if not isinstance(voice_id, str) or not re.fullmatch(r'qwen3:[a-f0-9]{32}', voice_id):
        raise ValueError('请先设计并采用一个本地 Qwen 音色')
    path = folder(user_id, voice_id.split(':', 1)[1])
    record = read(path)
    if record.get('status') != 'saved':
        raise ValueError('请先试听并采用这个音色')
    reference = path / 'design/chunk-0001.wav'
    if not reference.is_file() or hashlib.sha256(reference.read_bytes()).hexdigest() != record['audio']['sha256']:
        raise ValueError('已保存音色的参考音频已变化或缺失')
    return reference, record['reference_text']
