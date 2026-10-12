import hashlib
import io
from pathlib import Path
import tempfile
import unittest
import wave
from unittest.mock import patch

from backend.app.h3_voice_reference import freeze_voice_reference, inspect_voice_reference, validate_voice_references


def pcm(seconds,channels=1):
    buffer=io.BytesIO()
    with wave.open(buffer,'wb') as w:
        w.setparams((channels,2,24000,0,'NONE','not compressed'))
        w.writeframes(b'\x01\x02' * (int(seconds*24000)*channels))
    return buffer.getvalue()


class ReferenceLimitsTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)

    def source(self,seconds,name='source.wav',channels=1):
        p=self.root/name;p.write_bytes(pcm(seconds,channels));return p

    def freeze(self,source,name='out.wav'):
        target=self.root/name
        return target,freeze_voice_reference(source,target,hashlib.sha256(source.read_bytes()).hexdigest())

    def test_three_long_qwen_samples_become_stable_five_second_references(self):
        outputs=[]
        for i,duration in enumerate([15.44,16.32,17.36]):
            source=self.source(duration,f'voice-{i}.wav');original=source.read_bytes()
            target,metadata=self.freeze(source,f'out-{i}.wav');outputs.append(target)
            self.assertEqual(metadata['duration'],5);self.assertAlmostEqual(metadata['source_duration'],duration)
            self.assertNotEqual(metadata['sha256'],metadata['source_sha256'])
            self.assertEqual(source.read_bytes(),original)
            another,again=self.freeze(source,f'another-{i}.wav')
            self.assertEqual(another.read_bytes(),target.read_bytes());self.assertEqual(again,metadata)
            with wave.open(str(target)) as w:
                self.assertEqual(w.getnframes(),120000)
                self.assertEqual(w.readframes(120000),b'\x01\x02'*120000)
        self.assertEqual(validate_voice_references(outputs),[5,5,5])

    def test_short_legal_pcm_is_preserved_byte_for_byte_including_stereo(self):
        source=self.source(3.5,channels=2);target,metadata=self.freeze(source)
        self.assertEqual(target.read_bytes(),source.read_bytes())
        self.assertEqual(metadata['sha256'],metadata['source_sha256'])
        self.assertEqual(validate_voice_references([target]),[3.5])

    def test_long_reference_ends_at_last_clear_pause_without_changing_pcm_clock(self):
        for rate, width, channels in [(16000, 1, 1), (24000, 2, 2), (32000, 3, 1), (48000, 4, 2)]:
            with self.subTest(rate=rate, width=width, channels=channels):
                frames = bytearray()
                for index in range(rate * 7):
                    # Two pauses; the last one is 3.8--4.1 seconds, followed by speech.
                    quiet = 2.2 <= index / rate < 2.4 or 3.8 <= index / rate < 4.1
                    value = 0 if quiet else (1 if index % 2 else -1) * (1 << (width * 8 - 4))
                    encoded = (value + 128).to_bytes(1, 'little') if width == 1 else value.to_bytes(width, 'little', signed=True)
                    frames.extend(encoded * channels)
                source = self.root / f'{rate}.wav'
                with wave.open(str(source), 'wb') as writer:
                    writer.setparams((channels, width, rate, 0, 'NONE', 'not compressed'))
                    writer.writeframes(frames)
                original = source.read_bytes()
                target, metadata = self.freeze(source, f'{rate}-out.wav')
                self.assertEqual(metadata['boundary'], 'quiet_boundary')
                self.assertAlmostEqual(metadata['duration'], 3.94, places=2)
                inspected = inspect_voice_reference(source, hashlib.sha256(original).hexdigest())
                self.assertEqual(inspected['duration'], metadata['duration'])
                with wave.open(str(target)) as reader:
                    self.assertEqual(reader.getframerate(), rate)
                    self.assertEqual(reader.getsampwidth(), width)
                    self.assertEqual(reader.getnchannels(), channels)
                    self.assertEqual(reader.readframes(reader.getnframes()), frames[:reader.getnframes() * width * channels])
                self.assertEqual(source.read_bytes(), original)

    def test_trailing_pause_or_no_clear_pause_does_not_change_five_second_budget(self):
        source = self.source(7)
        _, metadata = self.freeze(source)
        self.assertEqual(metadata['duration'], 5)
        self.assertEqual(metadata['boundary'], 'bounded_no_clear_pause')

    def test_short_corrupt_or_changed_voice_cannot_create_reference(self):
        source=self.source(1.9)
        with self.assertRaisesRegex(ValueError,'至少需要 2 秒'):self.freeze(source)
        source.write_bytes(b'not wav')
        with self.assertRaisesRegex(ValueError,'PCM WAV'):self.freeze(source)
        source=self.source(4)
        with self.assertRaisesRegex(ValueError,'发生变化'):
            freeze_voice_reference(source,self.root/'out.wav','0'*64)
        self.assertFalse((self.root/'out.wav').exists())

    def test_submit_guard_enforces_each_and_combined_duration(self):
        with self.assertRaisesRegex(ValueError,'2–15 秒'):
            validate_voice_references([self.source(15.44)])
        with self.assertRaisesRegex(ValueError,'合计'):
            validate_voice_references([self.source(8,'a.wav'),self.source(8,'b.wav')])
        valid=self.source(2)
        with self.assertRaisesRegex(ValueError,'最多'):
            validate_voice_references([valid]*4)
        self.assertEqual(validate_voice_references([valid]),[2])

    def test_actual_bridge_rejects_overlong_reference_before_any_media_upload(self):
        from backend.app import comfyui_bridge as bridge
        from tests.test_comfyui_video_inputs import profile
        source=self.source(16)
        with patch.object(bridge,'_connection',return_value=bridge.DEFAULT_CONNECTION), \
             patch.object(bridge,'recover_guard_submission',return_value=None), \
             patch.object(bridge,'video_profile',return_value=profile('composition_reference')), \
             patch.object(bridge,'_upload_reference') as upload, \
             patch.object(bridge,'submit_prompt') as submit:
            with self.assertRaisesRegex(ValueError,'2–15 秒'):
                bridge.run_video_profile(1,'preset',prompt='test',image_path=self.root/'absent.png',duration=6,
                    ratio='16:9',output_path=self.root/'clip.mp4',input_mode='composition_reference',voice_reference_paths=[source])
            upload.assert_not_called();submit.assert_not_called()
