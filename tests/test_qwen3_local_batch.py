import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import module1_agent_director as module1
from backend.app import qwen3_local
from backend.app.tts_editor import _build_regeneration_plan


class QwenBatchContractTest(unittest.TestCase):
    def test_linux_postprocessing_uses_system_ffmpeg(self):
        import shutil, wave, math, struct
        executable = shutil.which('ffmpeg')
        if not executable:
            self.skipTest('System ffmpeg not installed')
        with TemporaryDirectory() as temporary, patch.object(module1, 'FFMPEG', Path(temporary) / 'missing.exe'):
            root = Path(temporary)
            source, target = root / 'source.wav', root / 'normalized.wav'
            with wave.open(str(source), 'wb') as wav:
                wav.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
                wav.writeframes(b''.join(struct.pack('<h', int(3000 * math.sin(2 * math.pi * 220 * i / 24000))) for i in range(24000)))
            module1._normalize_qwen_audio(source, target)
            module1._normalize_qwen_loudness(target)
            with wave.open(str(target), 'rb') as wav:
                self.assertEqual((wav.getnchannels(), wav.getframerate()), (1, 24000))
                self.assertGreater(wav.getnframes(), 20000)

    def test_missing_ffmpeg_rejects_before_any_model_work(self):
        with patch.object(module1, 'FFMPEG', Path('/missing/ffmpeg.exe')), \
             patch.object(module1.shutil, 'which', return_value=None), \
             patch.object(qwen3_local, 'run_batch') as run:
            with self.assertRaises(FileNotFoundError):
                module1.step2_qwen3_local_synthesize(['test'], None)
            run.assert_not_called()

    def test_fixed_reference_is_copied_once_and_all_sentences_share_one_guard(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            ref = root / 'saved.wav'
            ref.write_bytes(b'original speaker reference')
            output = root / 'batch'
            calls = []

            def run(command, *, cwd, env, on_line):
                calls.append(command)
                payload = json.loads((cwd / 'request.json').read_text(encoding='utf-8'))
                self.assertEqual(payload['texts'], ['First sentence.', 'Another sentence.'])
                self.assertEqual(payload['reference_sha256'], hashlib.sha256(ref.read_bytes()).hexdigest())
                self.assertEqual((cwd / 'reference.wav').read_bytes(), ref.read_bytes())
                self.assertEqual(env['HF_HUB_OFFLINE'], '1')
                outputs = []
                for i in range(1, 3):
                    name = f'chunk-{i:04d}.wav'
                    data = f'sample{i}'.encode()
                    (cwd / name).write_bytes(data)
                    outputs.append({'file': name, 'sha256': hashlib.sha256(data).hexdigest()})
                (cwd / 'result.json').write_text(json.dumps({'outputs': outputs}))
                return 0

            with patch.object(qwen3_local, 'health', return_value={'ready': True}), \
                 patch.object(qwen3_local, 'configuration', return_value={'python': root / 'python', 'clone': root / 'Base'}), \
                 patch.object(qwen3_local, 'guarded_tts_command', side_effect=lambda cmd, **kw: (['GUARD', *cmd], True)) as guard, \
                 patch.object(module1, '_run_guarded_and_stream', side_effect=run):
                result = qwen3_local.run_batch(output, mode='clone', texts=['First sentence.', 'Another sentence.'],
                    owner=3, task_id='fixture-qwen3', reference=ref, reference_text='Exact reference transcript.')
            self.assertEqual(len(result['outputs']), 2)
            self.assertEqual(len(calls), 1)
            self.assertEqual(guard.call_args.kwargs, {'owner': 3, 'task_id': 'fixture-qwen3'})

    def test_rejected_guard_never_falls_back_to_unprotected_execution(self):
        with TemporaryDirectory() as temporary, \
             patch.object(qwen3_local, 'health', return_value={'ready': True}), \
             patch.object(qwen3_local, 'configuration', return_value={'python': Path('/env/python'), 'design': Path('/model')}), \
             patch.object(qwen3_local, 'guarded_tts_command', return_value=(['guard'], True)), \
             patch.object(module1, '_run_guarded_and_stream', return_value=3) as run:
            with self.assertRaisesRegex(RuntimeError, '不会自动重复提交'):
                qwen3_local.run_batch(Path(temporary) / 'design', mode='design', texts=['Example.'],
                                      description='Adult natural voice.', owner=1, task_id='example')
            self.assertEqual(run.call_count, 1)

    def test_local_engine_is_valid_cli_choice_and_long_edits_keep_original_mapping(self):
        from backend.app.main import GenerateRequest
        self.assertEqual(GenerateRequest(project_name='Voice', script='测试新音色。', tts_engine='qwen3_local').tts_engine, 'qwen3_local')
        text = '这是一段需要持续复用同一个声音的长台词。' * 30
        chunks, positions, tokens = _build_regeneration_plan({7: text}, 'qwen3_local')
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 180 for chunk in chunks))
        self.assertEqual(''.join(chunks), text)
        self.assertEqual(positions[7], list(range(len(chunks))))


if __name__ == '__main__':
    unittest.main()
