import copy
import hashlib
import json
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from backend.app import video_dialogue_quality as quality, video_external_dialogue as exact
from backend.app import video_export as export
from backend.app.video_external_narration import digest
from backend.app.video_manual_dub import basis as timing_basis
from tests.test_video_external_narration import plan


class ExternalDialogueQualityTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name).resolve()
        self.full = plan()
        self.source = self.path / 'original.mp4'
        self.final = self.path / 'dubbed.mp4'
        self.source.write_bytes(b'original H3 speech, deliberately not recognized')
        self.final.write_bytes(b'final TTS and narration; recognition is mocked')
        speakers = {s['character_id']: s for s in self.full['speakers']}
        raw_clips = {}
        for i, turn in enumerate(self.full['turns']):
            file = self.path / f'{i}.wav'
            with wave.open(str(file), 'wb') as wav:
                wav.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
                wav.writeframes(b'\0\0' * 36000)
            raw_clips[i] = dict(file=file.name, sha256=hashlib.sha256(file.read_bytes()).hexdigest(),
                duration=1.5, voice_id=speakers[turn['character_id']]['voice_id'], voice_sha256='b'*64)
        visual, clips = exact.retime(self.full, raw_clips)
        self.data = dict(version=exact.REVISION, plan=visual, clips=clips, guide=raw_clips[0])
        self.data['key'] = digest(dict(version=exact.REVISION, plan=visual, clips=clips))
        basis = dict(plan=self.full, dialogue_route='external_tts', external_dialogue=self.data,
            voices=[dict(character_id=s['character_id'], voice_id=s['voice_id'], sha256='b'*64)
                    for s in self.full['speakers']])
        self.shot = dict(id='shot_001', kind='video', video_status='completed', video=self.source.name,
            video_request=dict(dialogue=dict(plan=visual, basis=basis, fingerprint=digest(basis)),
                               external_dialogue=self.data))
        self.record = dict(id='a'*32, revision=1, logs=[], status='video_review',
            creation_parameters=dict(dialogue_route='external_tts', speech_mode='mixed', quality_review_policy='media'),
            export_settings=dict(narration_volume=.7), shots=[self.shot])
        self.transcript = {'segments': [dict(text=c['text'], words=[
            dict(text=c['text'], start=c['start']+.01, end=c['end']-.01)]) for c in clips]}
        for target in ('backend.app.pipeline.probe_media_duration', 'backend.app.video_export.probe_media_duration'):
            patcher = patch(target, return_value=9.)
            patcher.start(); self.addCleanup(patcher.stop)
        def preview(path, shot, volume):
            shot['dialogue_alignment'] = dict(status='unverified', warning='Original H3 words differ')
            return self.final.name
        patcher = patch.object(exact, 'preview', side_effect=preview)
        self.preview = patcher.start(); self.addCleanup(patcher.stop)

    def screen(self, **kwargs):
        return quality.screen_external_dialogue(self.path, self.shot, volume=.7, **kwargs)

    def refreeze(self):
        basis = self.shot['video_request']['dialogue']['basis']
        self.shot['video_request']['dialogue']['fingerprint'] = digest(basis)

    def test_final_file_and_full_narration_are_checked_without_mutating_shot(self):
        before = copy.deepcopy(self.shot)
        with patch.object(quality, 'transcribe', return_value=self.transcript) as recognize:
            report = self.screen()
        self.assertEqual(recognize.call_args.args[0], self.final)
        self.assertEqual(report['review']['expected'], [t['text'] for t in self.full['turns']])
        self.assertTrue(report['review']['text_matches'])
        self.assertFalse(report['review']['issues'])
        for key in ('speaker_verified', 'timbre_verified', 'quality_accepted'):
            self.assertFalse(report['review'][key])
        self.assertEqual(self.shot, before)

    def test_native_checker_still_reads_original_and_never_previews(self):
        with patch.object(quality, 'transcribe', return_value={'segments': []}) as recognize:
            report = quality.screen_dialogue(self.path, self.shot)
        self.assertEqual(recognize.call_args.args[0], self.source)
        self.assertTrue(report['review']['issues'])
        self.preview.assert_not_called()

    def test_missing_or_extra_final_narration_fails(self):
        for text in ('旁白开。', '旁白开场。再说一句。'):
            with self.subTest(text=text):
                changed = copy.deepcopy(self.transcript)
                changed['segments'][0]['text'] = text
                self.final.write_bytes(text.encode())
                report = self.screen(recognize=lambda *_: changed)
                self.assertEqual(report['review']['issues'][0]['code'], 'speech_mismatch')

    def test_cache_binds_final_bytes_volume_and_effective_positions_and_rechecks_verdict(self):
        with patch.object(quality, 'transcribe', return_value=self.transcript) as recognize:
            first = self.screen()
            cache = self.path / 'assets/dialogue_reviews' / (first['fingerprint']+'.json')
            saved = json.loads(cache.read_text(encoding='utf8'))
            saved['review']['quality_accepted'] = True
            cache.write_text(json.dumps(saved), encoding='utf8')
            self.assertFalse(self.screen()['review']['quality_accepted'])
            self.assertEqual(recognize.call_count, 1)
            self.final.write_bytes(b'new final mux')
            second = self.screen()
            third = quality.screen_external_dialogue(self.path, self.shot, volume=.4)
            self.shot['manual_dub_timing'] = dict(basis=timing_basis(self.source, self.data),
                starts=[c['start']+.1 for c in self.data['clips']])
            fourth = self.screen()
        self.assertEqual(len({r['fingerprint'] for r in (first, second, third, fourth)}), 4)
        self.assertEqual(recognize.call_count, 4)

    def test_native_cache_is_never_used_for_final_audio(self):
        with patch.object(quality, 'transcribe', return_value=self.transcript) as recognize:
            native = quality.screen_dialogue(self.path, self.shot)
            final = self.screen()
        self.assertEqual(recognize.call_count, 2)
        self.assertNotEqual(native['fingerprint'], final['fingerprint'])

    def test_missing_frozen_basis_changed_words_voice_or_audio_fail_before_preview(self):
        original = copy.deepcopy(self.shot)
        for defect in ('basis', 'text', 'voice', 'audio'):
            with self.subTest(defect=defect):
                self.shot = copy.deepcopy(original)
                frozen = self.shot['video_request']
                if defect == 'basis':
                    frozen['dialogue']['basis'].pop('plan')
                elif defect == 'text':
                    frozen['external_dialogue']['clips'][0]['text'] = '偷偷改写旁白。'
                elif defect == 'voice':
                    frozen['external_dialogue']['clips'][0]['voice_id'] = 'qwen3:'+'f'*32
                else:
                    (self.path / '0.wav').write_bytes(b'changed audio')
                self.refreeze()
                with self.assertRaises(ValueError): self.screen()
        self.preview.assert_not_called()

    def test_manual_overlap_overflow_and_stale_timing_fail(self):
        for starts, basis in [([0, 0, 4], timing_basis(self.source, self.data)),
                              ([0, 2, 8.8], timing_basis(self.source, self.data)),
                              ([0, 2, 4], 'stale')]:
            with self.subTest(starts=starts, basis=basis):
                self.shot['manual_dub_timing'] = dict(basis=basis, starts=starts)
                with self.assertRaises(ValueError): self.screen()
        self.preview.assert_not_called()

    def test_dispatch_preserves_original_on_final_mismatch_and_uses_export_volume(self):
        before = copy.deepcopy(self.record)
        bad = dict(review=dict(issues=[dict(message='旁白漏字')], recognized='旁白开'))
        with patch.object(quality, 'screen_external_dialogue', return_value=bad) as check, \
             patch.object(quality, 'screen_dialogue', side_effect=AssertionError('not final audio')):
            with self.assertRaises(quality.NativeSpeechReviewRequired):
                quality.check_native_speech(self.path, self.record)
        self.assertEqual(check.call_args.kwargs['volume'], .7)
        self.assertEqual(self.record, before)

    def test_route_mismatch_or_missing_tts_never_falls_back_to_native(self):
        with patch.object(quality, 'screen_dialogue', side_effect=AssertionError('wrong route')):
            self.record['creation_parameters']['dialogue_route'] = 'h3_native'
            with self.assertRaises(ValueError): quality.check_native_speech(self.path, self.record)
            self.record['creation_parameters']['dialogue_route'] = 'external_tts'
            self.shot['video_request'].pop('external_dialogue')
            with self.assertRaises(ValueError): quality.check_native_speech(self.path, self.record)

    def test_one_click_final_failure_stops_next_gpu_request_and_export(self):
        from backend.app import one_click, video_studio, video_generation, video_quality
        from backend.app import video_native_image_quality
        self.record['native_story'] = {'text': '采用稿'}
        pending = dict(id='shot_002', kind='video', video_status='pending')
        self.record['shots'].append(pending)
        before = copy.deepcopy(self.record)
        bad = dict(review=dict(issues=[dict(message='最终旁白漏字')], recognized='旁白开'))
        for action in ('videos', 'export'):
            with self.subTest(action=action), \
                 patch.object(video_studio, 'storyboard_review_error', return_value=''), \
                 patch.object(video_native_image_quality, 'check_native_core_images', return_value=[]), \
                 patch.object(video_quality, 'check_automatic_export', return_value=[]), \
                 patch.object(quality, 'screen_external_dialogue', return_value=bad), \
                 patch.object(quality, 'screen_dialogue', side_effect=AssertionError('not final audio')), \
                 patch.object(video_generation, 'generate_for_user') as generate, \
                 patch.object(export, 'start_export_for_user') as start_export, \
                 patch('backend.app.video_native_speech_repair.retake') as retake:
                with self.assertRaises(quality.NativeSpeechReviewRequired):
                    one_click.dispatch(action, {'id': 1}, self.record)
                generate.assert_not_called(); start_export.assert_not_called(); retake.assert_not_called()
                self.assertEqual(self.record, before)

    def test_automatic_export_checks_tts_and_records_speech_count(self):
        saves = []
        report = dict(review=dict(issues=[], text_matches=True))
        with patch.object(export.studio, 'read', side_effect=lambda *_: copy.deepcopy(self.record)), \
             patch.object(export.studio, 'save', side_effect=lambda path, record: saves.append(copy.deepcopy(record))), \
             patch('backend.app.video_quality.check_automatic_export', return_value=[{}]), \
             patch.object(quality, 'screen_external_dialogue', return_value=report) as check, \
             patch.object(export, '_render', return_value=(self.path, self.final, self.final)) as render, \
             patch.object(export, 'reconcile_source_job'):
            export._worker(self.path, 1, review_generated=True)
        check.assert_called_once()
        render.assert_called_once()
        self.assertEqual(saves[-1]['status'], 'completed')
        self.assertEqual(saves[-1]['export']['review']['speech_shots_checked'], 1)

    def test_automatic_export_final_failure_never_renders(self):
        saves = []
        with patch.object(export.studio, 'read', side_effect=lambda *_: copy.deepcopy(self.record)), \
             patch.object(export.studio, 'save', side_effect=lambda path, record: saves.append(copy.deepcopy(record))), \
             patch('backend.app.video_quality.check_automatic_export', return_value=[]), \
             patch.object(quality, 'screen_external_dialogue', side_effect=ValueError('final TTS mismatch')), \
             patch.object(export, '_render') as render:
            export._worker(self.path, 1, review_generated=True)
        render.assert_not_called()
        self.assertEqual(saves[-1]['status'], 'export_failed')
        self.assertIn('final TTS mismatch', saves[-1]['error'])
