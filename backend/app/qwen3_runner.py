"""Isolated official Qwen3-TTS worker. Invoked by the shared TTS supervisor."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--request', required=True)
    args = parser.parse_args()
    request_file = Path(args.request).resolve(strict=True)
    data = json.loads(request_file.read_text(encoding='utf-8'))
    root = request_file.parent
    assert data['mode'] in ('design', 'clone')
    texts = data['texts']
    assert isinstance(texts, list) and 1 <= len(texts) <= 500
    assert all(isinstance(t, str) and t.strip() and len(t) <= 2000 for t in texts)
    if data['mode'] == 'design':
        assert len(texts) == 1 and isinstance(data['description'], str) and data['description'].strip()
    else:
        reference = root / 'reference.wav'
        assert reference.is_file() and not reference.is_symlink()
        assert hashlib.sha256(reference.read_bytes()).hexdigest() == data['reference_sha256']
        assert isinstance(data['reference_text'], str) and data['reference_text'].strip()

    # No framework/model import in the web server or CPU-only health checks.
    import numpy as np
    import soundfile as sf
    import torch
    from qwen_tts import Qwen3TTSModel

    torch.manual_seed(data['seed'])
    started = time.monotonic()
    model = Qwen3TTSModel.from_pretrained(data['model_dir'], device_map='cuda:0',
                                        dtype=torch.bfloat16, attn_implementation='sdpa',
                                        local_files_only=True)
    report = {'mode': data['mode'], 'load_s': time.monotonic() - started, 'outputs': []}
    prompt = None
    if data['mode'] == 'clone':
        prompt = model.create_voice_clone_prompt(ref_audio=str(reference), ref_text=data['reference_text'])
    for index, text in enumerate(texts, 1):
        started = time.monotonic()
        if data['mode'] == 'design':
            waves, sr = model.generate_voice_design(text=text, language='Chinese',
                instruct=data['description'], max_new_tokens=2048)
        else:
            waves, sr = model.generate_voice_clone(text=text, language='Chinese',
                voice_clone_prompt=prompt, max_new_tokens=2048)
        wave = np.asarray(waves[0])
        if wave.ndim != 1 or not len(wave) or not np.isfinite(wave).all() or np.max(np.abs(wave)) < .001:
            raise ValueError('Qwen3-TTS did not return usable audio')
        name = f'chunk-{index:04d}.wav'
        target = root / name
        if target.exists():
            raise FileExistsError('Existing speech output must never be silently regenerated')
        sf.write(target, wave, sr, subtype='PCM_16')
        report['outputs'].append({'file': name, 'sample_rate': sr, 'duration': len(wave) / sr,
            'sha256': hashlib.sha256(target.read_bytes()).hexdigest(), 'synthesis_s': time.monotonic() - started})
        print(json.dumps({'event': 'qwen3_chunk', 'completed': index, 'total': len(texts)}), flush=True)
    (root / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
