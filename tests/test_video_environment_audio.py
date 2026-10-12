import copy
import hashlib
import json
import tempfile
import unittest
import shutil
import subprocess
import wave
from pathlib import Path

from backend.app.video_environment_audio import REVISION, SOURCES, sha256, nonvocal_indices, timing_filter, separate
from backend.app.pipeline import ffmpeg_binary


class EnvironmentAudioTests(unittest.TestCase):
    def pieces(self):
        return [dict(start=0, end=2, target_start=0, target_end=1, scale=.5),
                dict(start=2, end=4, target_start=1, target_end=4, scale=1.5)]

    def test_nonvocal_candidate_uses_model_outputs_only(self):
        self.assertEqual(nonvocal_indices(SOURCES), [0, 1, 2])
        with self.assertRaises(ValueError):nonvocal_indices(['vocals', 'other'])

    def test_calibration_audio_uses_inverse_video_speed_and_full_duration(self):
        graph = timing_filter(self.pieces(), 4)
        self.assertIn('atempo=2.000000000', graph)
        self.assertIn('atempo=0.666666667', graph)
        self.assertIn('concat=n=2:v=0:a=1[environment]', graph)
        self.assertNotIn('amix', graph)

    def test_missing_overlapping_truncated_or_invalid_mapping_rejected(self):
        for key, value in [('start', 1), ('end', 2), ('scale', 2.5),
                           ('target_start', .5), ('target_end', float('nan'))]:
            rows=copy.deepcopy(self.pieces());rows[1][key]=value
            with self.subTest(key=key), self.assertRaises(ValueError):timing_filter(rows, 4)
        with self.assertRaises(ValueError):timing_filter(self.pieces()[:1], 4)
        with self.assertRaises(ValueError):timing_filter([], 4)

    def test_bad_checkpoint_fails_before_loading_torch_or_generating(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); source=root/'original.mp4'; checkpoint=root/'model.pt'
            source.write_bytes(b'original');checkpoint.write_bytes(b'not official')
            with self.assertRaisesRegex(ValueError, 'hash mismatch'):
                separate(source, checkpoint, '0'*64, root/'outputs')
            self.assertFalse((root/'outputs').exists())

    def test_cache_checks_bytes_and_never_reuses_an_approval_flag(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); source=root/'original.mp4'; checkpoint=root/'model.pt'
            source.write_bytes(b'original');checkpoint.write_bytes(b'local checkpoint')
            basis=dict(revision=REVISION,source_sha256=sha256(source),model_sha256=sha256(checkpoint),
                sample_rate=44100,device='cpu',threads=4,sources=SOURCES)
            key=hashlib.sha256(json.dumps(basis,sort_keys=True).encode()).hexdigest()
            cached=root/'outputs'/key;cached.mkdir(parents=True)
            stem=cached/'candidate.wav';stem.write_bytes(b'saved candidate')
            (cached/'receipt.json').write_text(json.dumps(dict(basis=basis,quality_accepted=True,
                artifacts=dict(non_vocal_candidate=dict(file=stem.name,sha256=sha256(stem))))))
            result=separate(source,checkpoint,sha256(checkpoint),root/'outputs')
            self.assertFalse(result['quality_accepted'])
            self.assertFalse(result['speech_absence_verified'])
            stem.write_bytes(b'changed candidate')
            with self.assertRaisesRegex(ValueError,'artifact changed'):
                separate(source,checkpoint,sha256(checkpoint),root/'outputs')

    @unittest.skipUnless(shutil.which(ffmpeg_binary()), 'FFmpeg required')
    def test_actual_ffmpeg_mapping_retains_duration_and_decodable_audio(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); source=root/'stem.wav'; target=root/'mapped.wav'
            subprocess.run([ffmpeg_binary(), '-v', 'error', '-f', 'lavfi', '-i',
                'sine=frequency=440:sample_rate=44100:duration=4', str(source)],
                check=True,capture_output=True)
            subprocess.run([ffmpeg_binary(), '-v', 'error', '-i', str(source),
                '-filter_complex_threads', '1', '-filter_complex', timing_filter(self.pieces(),4),
                '-map', '[environment]', '-c:a', 'pcm_s16le', str(target)],
                check=True,capture_output=True)
            with wave.open(str(target), 'rb') as wav:
                self.assertAlmostEqual(wav.getnframes()/wav.getframerate(),4,places=3)
                self.assertTrue(any(wav.readframes(wav.getnframes())))
