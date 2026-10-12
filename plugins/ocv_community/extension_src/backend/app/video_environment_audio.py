"""Opt-in CPU stem separation and TTS mixing, never automatic audio approval.

No model downloads or framework installation occur in this worker. A caller must
provide a separately acquired official checkpoint and its expected SHA256.

Enable with OCV_ENVIRONMENT_AUDIO=hdemucs_cpu and absolute existing paths in
OCV_ENVIRONMENT_AUDIO_PYTHON / OCV_ENVIRONMENT_AUDIO_CHECKPOINT. The optional
OCV_ENVIRONMENT_AUDIO_SHA256 defaults to the separately verified official file.
Missing settings default to off and preserve the legacy TTS-only route.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
from pathlib import Path
import subprocess
import threading
import time

REVISION = 'environment-stem-hdemucs-cpu-v1'
MODEL_URL = 'https://download.pytorch.org/torchaudio/models/hdemucs_high_trained.pt'
SOURCES = ['drums', 'bass', 'other', 'vocals']
OFFICIAL_SHA256 = 'a004b2790d73ffeaa535db458a1a79b539dfdbafbccc31f275d07e632ebd7816'
MIX_REVISION = 'environment-tts-mix-v1'
_WORKER_LOCK = threading.Lock()
_MIX_LOCK = threading.Lock()


def settings():
    """Explicit opt-in; never infer private runtime or checkpoint locations."""
    mode = os.environ.get('OCV_ENVIRONMENT_AUDIO', 'off').strip()
    if mode == 'off':
        return None
    if mode != 'hdemucs_cpu':
        raise ValueError('环境声模式须为 off 或 hdemucs_cpu')
    result = {}
    for key, variable in [('python', 'OCV_ENVIRONMENT_AUDIO_PYTHON'),
                          ('checkpoint', 'OCV_ENVIRONMENT_AUDIO_CHECKPOINT')]:
        value = os.environ.get(variable, '')
        file = Path(value)
        if not value or not file.is_absolute() or not file.is_file():
            raise ValueError(f'已启用环境声分离，请配置有效绝对文件路径 {variable}')
        # Resolving a venv Python symlink loses its environment on worker launch.
        result[key] = file if key == 'python' else file.resolve()
    result['sha256'] = os.environ.get('OCV_ENVIRONMENT_AUDIO_SHA256', OFFICIAL_SHA256)
    if not re.fullmatch('[a-f0-9]{64}', result['sha256']):
        raise ValueError('环境声分离权重 SHA256 无效')
    return result


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _basis(source_sha, model_sha):
    return dict(revision=REVISION, source_sha256=source_sha, model_sha256=model_sha,
                sample_rate=44100, device='cpu', threads=4, sources=SOURCES)


def _artifact(folder, row):
    file = (folder/row['file']).resolve()
    if not file.is_relative_to(folder.resolve()) or not file.is_file() or sha256(file) != row['sha256']:
        raise ValueError('环境声音频缓存文件缺失、越界或哈希已变化')
    return file


def _write_json(file, value):
    temporary = file.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf8')
    temporary.replace(file)


def _separated(path, original, config, source_sha, ffmpeg):
    """Reuse frozen stems before starting an isolated CPU-only worker."""
    basis = _basis(source_sha, config['sha256'])
    root = path/'assets/environment_audio/stems'
    folder = root/_digest(basis)
    receipt = folder/'receipt.json'
    with _WORKER_LOCK:
        if sha256(config['checkpoint']) != config['sha256']:
            raise ValueError('环境声分离权重 SHA256 与配置不一致')
        if not receipt.is_file():
            folder.mkdir(parents=True, exist_ok=True)
            command = [str(config['python']), '-B', str(Path(__file__).resolve()),
                '--source', str(original), '--checkpoint', str(config['checkpoint']),
                '--sha256', config['sha256'], '--output-root', str(root), '--ffmpeg', ffmpeg]
            env = {**os.environ, 'CUDA_VISIBLE_DEVICES': '', 'OMP_NUM_THREADS': '4',
                   'MKL_NUM_THREADS': '4', 'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1'}
            with (folder/'worker.log').open('ab') as log:
                result = subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT,
                    timeout=600, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            if result.returncode or not receipt.is_file():
                raise ValueError('环境声 CPU 分离失败，已保留本镜日志；未混入原H3人声')
        saved = json.loads(receipt.read_text(encoding='utf8'))
        if saved.get('basis') != basis:
            raise ValueError('环境声分离缓存与本镜原片或权重不一致')
        if set(saved.get('artifacts', {})) != {'non_vocal_candidate', 'vocals'}:
            raise ValueError('环境声分离缓存缺少明确的模型输出轨')
        for row in saved['artifacts'].values():
            _artifact(folder, row)
        candidate = _artifact(folder, saved['artifacts']['non_vocal_candidate'])
    if sha256(original) != source_sha:
        raise ValueError('分离期间原片已变化，未混合环境声')
    return candidate, saved, receipt


def mix_environment(path, original, rendered, track, duration, alignment):
    """Mix only estimated non-vocal audio into the unchanged timed TTS track.

    No record mutation, synthesis or downloads. All diagnostics stay alongside
    this original video attempt; failures never fall back to mixing raw speech.
    """
    config = settings()
    if config is None:
        return track, None
    # Screening and export may request the same cached preview concurrently.
    # Serialize only the enabled local CPU path and its atomic cache writes.
    with _MIX_LOCK:
        return _mix_environment(path, original, rendered, track, duration, alignment, config)


def _mix_environment(path, original, rendered, track, duration, alignment, config):
    from .video_export import _run as run, ffmpeg_binary, probe_media_duration
    from .video_external_dialogue import verified
    path, original, rendered = Path(path).resolve(), Path(original).resolve(), Path(rendered).resolve()
    if any(not file.is_relative_to(path) or not file.is_file() for file in (original, rendered)):
        raise ValueError('环境声输入必须是本项目原片与对应画面')
    if not math.isfinite(duration) or not 0 < duration < 17:
        raise ValueError('环境声混合时长无效')
    source_sha, rendered_sha = sha256(original), sha256(rendered)
    status = alignment.get('status')
    pieces = alignment.get('intervals') if status == 'calibrated' else None
    if status == 'calibrated':
        graph = timing_filter(pieces, duration)
    else:
        if rendered_sha != source_sha:
            raise ValueError('画面已变化但缺少已校准时间映射，不能猜测环境声时序')
        graph = None
    tts = verified(path, track)
    ffmpeg = ffmpeg_binary()
    ffmpeg = shutil.which(ffmpeg) or ffmpeg
    diagnostic_root = original.parent/'environment_audio'
    diagnostic_root.mkdir(parents=True, exist_ok=True)
    try:
        candidate, separated, separation_receipt = _separated(path, original, config, source_sha, ffmpeg)
        if abs(probe_media_duration(candidate)-duration) > .05:
            raise ValueError('分离环境声与原片时长不一致')
        basis = dict(revision=MIX_REVISION, source_sha256=source_sha, rendered_sha256=rendered_sha,
            separation_basis=separated['basis'], stem_sha256=sha256(candidate),
            tts_track=track, duration=duration, intervals=pieces, environment_gain=1.0,
            raw_audio_mixed=False)
        folder = diagnostic_root/_digest(basis)
        receipt = folder/'mix.json'
        if receipt.is_file():
            saved = json.loads(receipt.read_text(encoding='utf8'))
            if saved.get('basis') != basis:
                raise ValueError('环境声混合缓存依据不一致')
            mixed = _artifact(folder, saved['artifact'])
            if saved.get('mapped_artifact'):
                _artifact(folder, saved['mapped_artifact'])
            if abs(probe_media_duration(mixed)-duration) > .002:
                raise ValueError('环境声混合缓存时长不一致')
        else:
            folder.mkdir(parents=True, exist_ok=True)
            if graph:
                mapped = folder/'mapped-environment.wav'
                run([ffmpeg, '-y', '-i', str(candidate), '-filter_complex_threads', '1',
                    '-filter_complex', graph, '-map', '[environment]', '-threads', '2',
                    '-c:a', 'pcm_f32le', str(mapped)], '环境声采用与校准画面相同的分段时序')
                if abs(probe_media_duration(mapped)-duration) > .002:
                    raise ValueError('校准环境声时长不一致')
                environment = mapped
            else:
                environment = candidate
            mixed = folder/'mixed.wav'
            temporary = folder/'mixed.tmp.wav'
            mix = ('[0:a]aresample=48000,asetpts=PTS-STARTPTS[tts];'
                '[1:a]aresample=48000,asetpts=PTS-STARTPTS,apad,'
                f'atrim=duration={duration:.9f}[environment];'
                '[tts][environment]amix=inputs=2:duration=first:normalize=0:dropout_transition=0,'
                'alimiter=limit=0.95:level=0:latency=1[mixed]')
            run([ffmpeg, '-y', '-i', str(tts), '-i', str(environment), '-filter_complex_threads', '1',
                '-filter_complex', mix, '-map', '[mixed]', '-threads', '2', '-c:a', 'pcm_s16le',
                str(temporary)], '混合已分离的非人声候选与原TTS配音')
            if abs(probe_media_duration(temporary)-duration) > .002:
                raise ValueError('环境声与TTS混合时长不一致')
            if sha256(original) != source_sha or sha256(rendered) != rendered_sha:
                raise ValueError('混合期间原片或画面已变化，未采用混合结果')
            verified(path, track)
            temporary.replace(mixed)
            saved = dict(basis=basis, separation_receipt=separation_receipt.relative_to(path).as_posix(),
                artifact=dict(file=mixed.name, sha256=sha256(mixed)),
                source_file=original.relative_to(path).as_posix(),
                status='mixed_estimated_non_vocal', raw_audio_mixed=False,
                quality_accepted=False, speaker_verified=False, lip_sync_verified=False,
                environment_preservation_verified=False,
                limitations=separated.get('limitations', []))
            if graph:
                saved['mapped_artifact']=dict(file=environment.name,sha256=sha256(environment))
            _write_json(receipt, saved)
        if (sha256(original)!=source_sha or sha256(rendered)!=rendered_sha
                or sha256(candidate)!=basis['stem_sha256']):
            raise ValueError('采用环境声前原片、画面或分离轨已变化')
        verified(path,track)
        result = dict(file=mixed.relative_to(path).as_posix(), sha256=sha256(mixed), duration=duration)
        diagnostic = dict(status='mixed_estimated_non_vocal',
            receipt=receipt.relative_to(path).as_posix(), source_sha256=source_sha,
            raw_audio_mixed=False, quality_accepted=False, speaker_verified=False,
            lip_sync_verified=False, environment_preservation_verified=False)
        return result, diagnostic
    except Exception as exc:
        _write_json(diagnostic_root/'last_failure.json', dict(source_sha256=source_sha,
            status='failed', error=str(exc)[:2000], raw_audio_mixed=False, quality_accepted=False))
        raise


def sha256(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def nonvocal_indices(sources):
    if list(sources) != SOURCES:
        raise ValueError('Unexpected separation sources; do not guess a residual stem')
    return [0, 1, 2]


def timing_filter(pieces, duration, input_label='0:a'):
    """Apply the same piecewise mapping as calibrated video, without raw audio."""
    if not math.isfinite(duration) or duration <= 0 or not pieces:
        raise ValueError('Missing positive duration or calibration pieces')
    filters, labels = [], []
    previous_source = previous_target = 0.0
    for index, piece in enumerate(pieces):
        values = [piece.get(key) for key in ('start', 'end', 'target_start', 'target_end', 'scale')]
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
            raise ValueError('Invalid calibration piece')
        start, end, target_start, target_end, scale = values
        if (abs(start-previous_source) > 1e-6 or abs(target_start-previous_target) > 1e-6
                or end <= start or target_end <= target_start or not .5 <= scale <= 2
                or abs((target_end-target_start)/(end-start)-scale) > 1e-6):
            raise ValueError('Calibration pieces must preserve continuous ordered coverage')
        label = f'env{index}'
        filters.append(f'[{input_label}]atrim=start={start:.9f}:end={end:.9f},'
            f'asetpts=PTS-STARTPTS,atempo={1/scale:.9f},'
            f'apad,atrim=duration={target_end-target_start:.9f},asetpts=PTS-STARTPTS[{label}]')
        labels.append(f'[{label}]')
        previous_source, previous_target = end, target_end
    if abs(previous_source-duration) > 1e-6 or abs(previous_target-duration) > 1e-6:
        raise ValueError('Calibration must cover the entire original and final duration')
    filters.append(''.join(labels)+f'concat=n={len(labels)}:v=0:a=1[environment]')
    return ';'.join(filters)


def _run(command, *, data=None):
    result = subprocess.run(command, input=data, capture_output=True, check=False)
    if result.returncode:
        raise ValueError(result.stderr.decode('utf8', errors='replace')[-1800:])
    return result.stdout


def separate(source, checkpoint, expected_sha256, output_root, *, ffmpeg='ffmpeg'):
    # Must run in its own process: do not change a web worker's torch/GPU state.
    source, checkpoint = Path(source).resolve(), Path(checkpoint).resolve()
    if not source.is_file() or not checkpoint.is_file():
        raise ValueError('Existing original media and local official checkpoint are required')
    actual_sha = sha256(checkpoint)
    if actual_sha != expected_sha256:
        raise ValueError('Separation checkpoint hash mismatch')
    basis = _basis(sha256(source), actual_sha)
    key = _digest(basis)
    folder = Path(output_root).resolve()/key
    receipt = folder/'receipt.json'
    if receipt.is_file():
        saved = json.loads(receipt.read_text(encoding='utf8'))
        if saved.get('basis') != basis:
            raise ValueError('Separation cache basis mismatch')
        for row in saved['artifacts'].values():
            try:
                _artifact(folder,row)
            except ValueError as exc:
                raise ValueError('Separation cache artifact changed') from exc
        # A cached experiment is never an audio approval, even if edited later.
        for field in ('quality_accepted', 'speech_absence_verified', 'environment_preservation_verified'):
            saved[field] = False
        return saved
    os.environ['CUDA_VISIBLE_DEVICES'] = ''
    os.environ['OMP_NUM_THREADS'] = '4'
    os.environ['MKL_NUM_THREADS'] = '4'
    import numpy as np
    import torch
    import torchaudio
    torch.set_num_threads(4)
    torch.set_num_interop_threads(1)
    started = time.monotonic()
    raw = _run([ffmpeg, '-v', 'error', '-threads', '4', '-i', str(source), '-map', '0:a:0', '-vn',
                '-ar', '44100', '-ac', '2', '-f', 'f32le', 'pipe:1'])
    samples = np.frombuffer(raw, dtype='<f4').reshape(-1, 2).copy()
    if not 0 < len(samples) <= 17*44100 or not np.isfinite(samples).all():
        raise ValueError('Expected one finite H3 clip of at most 17 seconds')
    waveform = torch.from_numpy(samples.T.copy())
    model = torchaudio.models.hdemucs_high(sources=SOURCES).cpu().eval()
    model.load_state_dict(torch.load(checkpoint, map_location='cpu', weights_only=True))
    reference = waveform.mean(dim=0)
    mean, std = reference.mean(), reference.std()
    if float(std) <= 1e-8:
        raise ValueError('Original audio is effectively silent; no environment claim is possible')
    with torch.inference_mode():
        estimates = model(((waveform-mean)/std).unsqueeze(0))[0]*std
    # Sum model-estimated non-vocal sources only. Never mix the original audio
    # back in or use unreviewed raw audio to fill speech windows.
    candidate = estimates[nonvocal_indices(SOURCES)].sum(dim=0)
    vocals = estimates[3]
    if not torch.isfinite(estimates).all():
        raise ValueError('Separation returned non-finite samples')
    folder.mkdir(parents=True, exist_ok=True)
    artifacts = {}
    for name, audio in [('non_vocal_candidate', candidate), ('vocals', vocals)]:
        file = folder/(name+'.wav')
        payload = audio.T.contiguous().numpy().astype('<f4').tobytes()
        _run([ffmpeg, '-v', 'error', '-y', '-threads', '4', '-f', 'f32le', '-ar', '44100', '-ac', '2',
              '-i', 'pipe:0', '-c:a', 'pcm_f32le', str(file)], data=payload)
        artifacts[name] = dict(file=file.name, sha256=sha256(file),
            rms=float(audio.square().mean().sqrt()), peak=float(audio.abs().max()))
    result = dict(basis=basis, model_source=MODEL_URL, source_file=str(source),
        output_directory=str(folder), artifacts=artifacts, duration=len(samples)/44100,
        elapsed_seconds=time.monotonic()-started, torch_version=torch.__version__,
        torchaudio_version=torchaudio.__version__, input_rms=float(waveform.square().mean().sqrt()),
        dc_removed=float(mean), residual_method='sum estimated drums+bass+other; original excluded',
        quality_accepted=False, speech_absence_verified=False, environment_preservation_verified=False,
        limitations=['Music-trained model; speech leakage and missing action sounds remain possible.',
                     'Non-vocal output may contain unwanted music.',
                     'Calibrated video requires the same piecewise time mapping for this stem.'])
    temporary = receipt.with_suffix('.tmp')
    temporary.write_text(json.dumps(result, indent=2), encoding='utf8')
    temporary.replace(receipt)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--checkpoint', required=True)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--output-root', required=True)
    parser.add_argument('--ffmpeg', default='ffmpeg')
    args = parser.parse_args()
    print(json.dumps(separate(args.source, args.checkpoint, args.sha256, args.output_root,
                             ffmpeg=args.ffmpeg), ensure_ascii=False))
