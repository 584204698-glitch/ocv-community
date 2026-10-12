import array
import copy
import json
import math
import shutil
import subprocess
import unittest
from unittest.mock import patch

from backend.app import video_export as export, video_dialogue as dialogue, video_native_export as native
from tests import test_video_export as fixtures
from tests.test_h3_dialogue import data


@unittest.skipUnless(shutil.which(export.ffmpeg_binary()) and shutil.which(export.ffprobe_binary()), 'FFmpeg required')
class NativeExportTests(unittest.TestCase):
    def setUp(self):
        fixtures.ExportFixture.setUp(self)
        self.record['creation_parameters']={'speech_mode':'dialogue'}
        self.record['shots']=[self.record['shots'][0],self.record['shots'][2]]
        self.record.pop('audio')
        (self.path/'assets/audio.wav').unlink();(self.path/'assets/subtitles.srt').unlink()
        for index,shot in enumerate(self.record['shots']):
            duration=6.4+index*.1
            subprocess.run([export.ffmpeg_binary(),'-v','error','-y','-f','lavfi','-i',
                f'color=c={shot["id"]}:s=160x90:r=30:d={duration}', '-f','lavfi','-i',
                f'sine=frequency={440+index*440}:sample_rate=48000:duration={duration}',
                '-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac','-shortest',str(self.path/shot['video'])],check=True,capture_output=True)
            shot['video_request']={'dialogue':{'plan':data()},'audio_policy':{'mode':'native_dialogue'}}

    def test_real_render_preserves_source_tail_without_narration_and_offsets_subtitles(self):
        with patch.object(dialogue,'assert_native_export_current',return_value=[]):
            folder,raw,subtitled=export._render(self.path,self.record)
        expected=sum(math.ceil(export.probe_media_duration(self.path/s['video'])*30-1e-6)/30 for s in self.record['shots'])
        for file in (raw,subtitled):
            self.assertAlmostEqual(export.probe_media_duration(file),expected,delta=.08)
            subprocess.run([export.ffmpeg_binary(),'-v','error','-i',str(file),'-f','null','-'],check=True,capture_output=True)
        # End of the first 6.4s clip must retain audio, despite a 6s request.
        content=subprocess.check_output([export.ffmpeg_binary(),'-v','error','-ss','6.2','-i',str(raw),'-t','0.08',
            '-vn','-ac','1','-ar','48000','-f','f32le','pipe:1'])
        samples=array.array('f',content)
        self.assertGreater(math.sqrt(sum(x*x for x in samples)/len(samples)),.02)
        metadata=json.loads((folder/'声音与字幕说明.json').read_text(encoding='utf8'))
        self.assertEqual(metadata['subtitle_timing'],'planned_observation_only')
        self.assertAlmostEqual(metadata['captions'][2]['start'],6.6,delta=.04)
        self.assertFalse((folder/'配音.wav').exists())

    def test_no_muting_and_no_silent_replacement_for_missing_original_audio(self):
        with patch.object(dialogue,'assert_native_export_current',return_value=[]):
            self.record['export_settings']={'use_video_audio':False}
            with self.assertRaisesRegex(ValueError,'保留 H3 音轨'):native.render_native(self.path,self.record)
            self.record['export_settings']['use_video_audio']=True
            with patch.object(export,'_has_audio_stream',return_value=False),self.assertRaisesRegex(ValueError,'缺少 H3 原生音轨'):
                native.render_native(self.path,self.record)
        self.assertFalse((self.root/'output').exists())

    def test_external_narration_real_mix_keeps_video_duration_and_adds_caption(self):
        import hashlib
        voice=self.path/'narration.wav'
        subprocess.run([export.ffmpeg_binary(),'-y','-f','lavfi','-i',
            'sine=frequency=1500:sample_rate=48000:duration=0.3',str(voice)],check=True,capture_output=True)
        narrator=dict(file='narration.wav',sha256=hashlib.sha256(voice.read_bytes()).hexdigest(),
            start=5.7,end=6,duration=.3,text='独立旁白。',character_id='pc_'+'d'*32)
        self.record['shots'][0]['video_request']['external_narration']=dict(clips=[narrator])
        reports=[dict(review=dict(issues=[],text_matches=True,turns=copy.deepcopy(data()['turns']))) for _ in self.record['shots']]
        with patch.object(dialogue,'assert_native_export_current',return_value=[]), \
             patch('backend.app.video_dialogue_quality.check_native_speech',return_value=reports) as check:
            folder,raw,_=native.render_native(self.path,self.record)
        check.assert_called_once()
        metadata=json.loads((folder/'声音与字幕说明.json').read_text(encoding='utf8'))
        self.assertIn('IndexTTS2.5',metadata['audio_source'])
        self.assertTrue(any(r['text']=='独立旁白。' and r['start']==5.7 for r in metadata['captions']))
        self.assertTrue(raw.is_file())

    def test_failed_speech_review_cannot_be_rendered_as_automatic_pass(self):
        self.record['_native_speech_reviews']=[{'review':{'issues':[{}],'text_matches':False}}]*2
        with patch.object(dialogue,'assert_native_export_current'),self.assertRaisesRegex(ValueError,'筛查未通过'):
            native.render_native(self.path,self.record)
        self.assertFalse((self.root/'output').exists())

    def test_exact_dialogue_export_uses_measured_tts_track_and_omits_h3_speech(self):
        import hashlib
        from backend.app import video_external_dialogue as exact
        voice=self.path/'dialogue.wav'
        subprocess.run([export.ffmpeg_binary(),'-v','error','-y','-f','lavfi','-i',
            'sine=frequency=1500:sample_rate=48000:duration=0.5',str(voice)],check=True,capture_output=True)
        clip=dict(file='dialogue.wav',sha256=hashlib.sha256(voice.read_bytes()).hexdigest(),duration=.5,kind='character')
        self.record['creation_parameters']['dialogue_route']='external_tts'
        self.record['export_settings']={'use_video_audio':False}
        for shot in self.record['shots']:
            turns=[{**clip,'text':t['text'],'character_id':t['character_id'],'start':t['start'],'end':t['start']+.5} for t in data()['turns']]
            guide=exact.make_track(self.path,self.path/'guide',turns,6)
            shot['video_request']['external_dialogue']=dict(plan=data(),clips=turns,guide=guide)
            shot['video_request']['audio_policy']['mode']='external_dialogue'
        with patch.object(dialogue,'assert_native_export_current',return_value=[]), \
             patch('backend.app.video_dialogue_quality.check_native_speech',side_effect=AssertionError('Unused H3 speech must not block TTS export')):
            folder,raw,_=native.render_native(self.path,self.record)
        metadata=json.loads((folder/'声音与字幕说明.json').read_text(encoding='utf8'))
        self.assertEqual(metadata['subtitle_timing'],'measured_tts_clips')
        self.assertIn('original H3 audio excluded',metadata['audio_source'])
        self.assertAlmostEqual(metadata['captions'][0]['end'],.7)
        # The original 440Hz voice continues here; dubbed output must be quiet.
        content=subprocess.check_output([export.ffmpeg_binary(),'-v','error','-ss','1.2','-i',str(raw),'-t','0.08',
            '-vn','-ac','1','-ar','48000','-f','f32le','pipe:1'])
        samples=array.array('f',content)
        self.assertLess(math.sqrt(sum(x*x for x in samples)/len(samples)),.001)

