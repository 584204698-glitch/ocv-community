import copy
import hashlib
import math
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from backend.app.video_manual_dub import basis, place, effective_clips
from backend.app import video_generation as generation, video_external_dialogue as exact


class ManualDubTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name); self.source = self.path / 'raw.mp4'
        self.source.write_bytes(b'video')
        self.clips = [dict(duration=1.2, start=1.3, end=2.5, kind='character', text='小林！', character_id='speaker_a'),
                      dict(duration=1.92, start=2.95, end=4.87, kind='character', text='阿竹，你来了。', character_id='molan'),
                      dict(duration=4.96, start=5.72, end=10.68, kind='narrator', text='她叫阿竹。', character_id='narrator')]
        self.data = dict(key='frozen', clips=self.clips, plan=dict(duration=14, speakers=[]))
        self.shot = dict(id='shot_005', video='raw.mp4', video_status='completed',
                         video_request=dict(external_dialogue=self.data), narration_preview='old.mp4')
        self.record = dict(id='project', revision=3, status='video_review', logs=[], shots=[self.shot])

    def test_individual_moves_preserve_duration_identity_and_frozen_request(self):
        original = copy.deepcopy(self.data)
        moved = place(self.clips, [1.5, 3.25, 6.3], 14.375)
        for clip, length in zip(moved,[1.2,1.92,4.96]):
            self.assertAlmostEqual(clip['end']-clip['start'],length)
        self.assertEqual([c['character_id'] for c in moved], ['speaker_a','molan','narrator'])
        self.assertEqual(self.data, original)

    def test_bounds_and_nonfinite_do_not_cut_or_speed_audio(self):
        for starts in ([0, 1], [-.1, 3, 6], [1, 3, 12], [math.nan,3,6], [True,3,6]):
            with self.subTest(starts=starts), self.assertRaises(ValueError):
                place(self.clips, starts, 14.375)

    def test_timing_does_not_leak_to_replaced_video_or_voice(self):
        self.shot['manual_dub_timing'] = dict(basis=basis(self.source,self.data), starts=[1.5,3.25,6.3])
        self.assertEqual(effective_clips(self.shot,self.source,self.data,14.375)[0]['start'],1.5)
        self.source.write_bytes(b'new-video')
        with self.assertRaises(ValueError):effective_clips(self.shot,self.source,self.data,14.375)
        self.source.write_bytes(b'video'); self.data['clips'][0]['text']='new-line'
        with self.assertRaises(ValueError):effective_clips(self.shot,self.source,self.data,14.375)

    def test_automatic_positions_feed_timeline_and_manual_override_is_scoped(self):
        self.shot['dialogue_alignment']=dict(status='audio_placed',audio_starts=[1.5,3.25,6.3],
                                             audio_basis=basis(self.source,self.data))
        self.assertEqual(effective_clips(self.shot,self.source,self.data,14.375)[1]['start'],3.25)
        self.shot['manual_dub_timing']=dict(basis=basis(self.source,self.data),starts=[1.4,3.2,6])
        self.assertEqual(effective_clips(self.shot,self.source,self.data,14.375)[1]['start'],3.2)
        self.shot.pop('manual_dub_timing');self.source.write_bytes(b'replaced')
        with self.assertRaises(ValueError):effective_clips(self.shot,self.source,self.data,14.375)
        self.source.write_bytes(b'video'); self.data['clips'][0]['text']='new-line'
        with self.assertRaises(ValueError):effective_clips(self.shot,self.source,self.data,14.375)

    def contexts(self):
        from contextlib import ExitStack
        stack = ExitStack()
        for target, value in [('require_user',{'id':1}),('directory',self.path),('read',self.record)]:
            stack.enter_context(patch.object(generation.studio,target,return_value=value))
        stack.enter_context(patch.object(generation.studio,'editable'))
        stack.enter_context(patch('backend.app.video_export._source',return_value=self.source))
        stack.enter_context(patch('backend.app.video_export.probe_media_duration',return_value=14.375))
        return stack

    def test_save_only_composes_frozen_media_and_export_is_invalidated(self):
        frozen=copy.deepcopy(self.shot['video_request']); self.record['export']={'old':'file'}
        req=generation.ManualDubTiming(revision=3,basis=basis(self.source,self.data),starts=[1.5,3.25,6.3])
        def compose(path,shot):
            self.assertEqual(shot['manual_dub_timing']['starts'],req.starts)
            shot['dialogue_alignment']={'status':'manual','lip_sync_verified':False}
            return 'manual.mp4'
        with self.contexts(),patch.object(exact,'preview',side_effect=compose),patch.object(generation.studio,'save'):
            result=generation.save_dub_timeline('project','shot_005',req,None)
        self.assertEqual(result['shots'][0]['video_request'],frozen)
        self.assertEqual(self.shot['video'],'raw.mp4');self.assertEqual(self.shot['narration_preview'],'manual.mp4')
        self.assertNotIn('export',result);self.assertEqual(self.shot['dialogue_alignment']['status'],'manual')
        self.assertFalse(generation.NARRATION_PREVIEWS)

    def test_failed_composition_keeps_previous_preview_and_positions(self):
        before=copy.deepcopy(self.record)
        req=generation.ManualDubTiming(revision=3,basis=basis(self.source,self.data),starts=[1.5,3.25,6.3])
        with self.contexts(),patch.object(exact,'preview',side_effect=RuntimeError('failed')),patch.object(generation.studio,'save') as saved:
            with self.assertRaises(HTTPException):generation.save_dub_timeline('project','shot_005',req,None)
            saved.assert_not_called()
        self.assertEqual(self.record,before);self.assertFalse(generation.NARRATION_PREVIEWS)

    def test_manual_preview_bypasses_automatic_recognition_and_reuses_raw_video(self):
        self.shot['manual_dub_timing']=dict(basis=basis(self.source,self.data),starts=[1.5,3.25,6.3])
        track=self.path/'track.wav'
        with wave.open(str(track),'wb') as wav:
            wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(16000)
            wav.writeframes(b'\x00\x00'*230000)
        metadata=dict(file='track.wav',sha256=hashlib.sha256(track.read_bytes()).hexdigest(),duration=14.375)
        def run(args,label):Path(args[-1]).write_bytes(b'dubbed')
        with patch('backend.app.video_export._source',return_value=self.source), \
                patch('backend.app.video_export.probe_media_duration',return_value=14.375), \
                patch('backend.app.video_external_alignment.calibrate') as calibrate, \
                patch.object(exact,'make_track',return_value=metadata) as make, \
                patch('backend.app.video_export.ffmpeg_binary',return_value='ffmpeg'), \
                patch('backend.app.video_export._run',side_effect=run):
            exact.preview(self.path,self.shot)
        calibrate.assert_not_called()
        self.assertEqual([c['start'] for c in make.call_args.args[2]],[1.5,3.25,6.3])
        self.assertEqual(self.shot['dialogue_alignment']['status'],'manual')

    def test_restore_auto_does_not_regenerate_voices_or_change_request(self):
        self.shot['manual_dub_timing']=dict(basis=basis(self.source,self.data),starts=[1.5,3.25,6.3])
        req=generation.ManualDubTiming(revision=3,basis=basis(self.source,self.data),reset=True)
        def compose(path,shot):
            self.assertNotIn('manual_dub_timing',shot)
            return 'auto.mp4'
        with self.contexts(),patch.object(exact,'preview',side_effect=compose),patch.object(generation.studio,'save'):
            generation.save_dub_timeline('project','shot_005',req,None)
        self.assertNotIn('manual_dub_timing',self.shot)


if __name__=='__main__':unittest.main()
