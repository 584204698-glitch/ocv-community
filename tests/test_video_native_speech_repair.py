import copy
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from backend.app import video_studio as studio, one_click, video_generation
from backend.app.video_dialogue_quality import NativeSpeechReviewRequired
from backend.app.video_native_speech_repair import retake


class SpeechRetakeTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        root=Path(temporary.name)
        for target,name,value in [(studio,'ROOT',root/'projects'),(one_click,'ROOT',root/'runs'),
                                  (studio,'ACTIVE',set()),(studio,'IMAGE_EDITS',set())]:
            p=patch.object(target,name,value);p.start();self.addCleanup(p.stop)
        self.identity='a'*32;self.path=studio.directory(1,self.identity);self.path.mkdir(parents=True)
        self.content=b'original completed native media'
        (self.path/'clip.mp4').write_bytes(self.content)
        self.plan={'turns':[{'text':'小熊，快吹呀！','character_id':'pc_'+'b'*32}]}
        self.record=dict(id=self.identity,revision=1,status='video_review',logs=[],native_story={'text':'story'},
            native_owner_id=1,creation_parameters={'video_generation_backend':'comfyui'},shots=[
                dict(id='shot_001',kind='video',video='clip.mp4',video_status='completed',video_task_id='known-task',
                     video_request={'dialogue':{'plan':self.plan}}),
                dict(id='shot_002',kind='video',video_status='pending')])
        studio.save(self.path,self.record)
        self.report=dict(fingerprint='review-id',evidence={'plan':self.plan,'clip_sha256':hashlib.sha256(self.content).hexdigest()},
            review=dict(text_matches=False,recognized='熊，快吹呀！',expected=['小熊，快吹呀！'],issues=[{'code':'speech_mismatch'}]))

    def failure(self,report=None):
        return NativeSpeechReviewRequired('speech needs review','shot_001',report or self.report)

    def test_one_completed_local_retake_uses_normal_generator_and_keeps_other_shots(self):
        with patch.object(video_generation,'generate_for_user',return_value={'status':'video_generating'}) as generate:
            result=retake(self.identity,1,{'id':1},self.failure())
        args=generate.call_args.args
        self.assertEqual(result['status'],'video_generating')
        self.assertEqual(args[1].shot_ids,['shot_001']);self.assertTrue(args[1].regenerate_completed)
        current=studio.read(self.path)
        self.assertEqual(args[1].revision,current['revision'])
        self.assertEqual(current['shots'][1],self.record['shots'][1])
        self.assertEqual(current['shots'][0]['video_request'],self.record['shots'][0]['video_request'])
        self.assertEqual((self.path/'clip.mp4').read_bytes(),self.content)
        self.assertFalse(current['shots'][0]['native_speech_repairs'][0]['quality_accepted'])
        with patch.object(video_generation,'generate_for_user') as again,self.assertRaises(NativeSpeechReviewRequired):
            retake(self.identity,current['revision'],{'id':1},self.failure())
        again.assert_not_called()

    def test_normal_invalidation_preserves_retry_marker_and_archives_original_request(self):
        with patch.object(video_generation,'generate_for_user'):
            retake(self.identity,1,{'id':1},self.failure())
        record=studio.read(self.path);shot=record['shots'][0]
        marker=copy.deepcopy(shot['native_speech_repairs'])
        studio._invalidate_shot_video(record,shot,'normal regeneration')
        self.assertEqual(shot['native_speech_repairs'],marker)
        self.assertEqual(shot['video_history'][0]['video_request'],self.record['shots'][0]['video_request'])
        self.assertEqual(shot['video_history'][0]['video_task_id'],'known-task')
        self.assertEqual((self.path/'clip.mp4').read_bytes(),self.content)
        self.assertNotIn('video_request',shot)
        self.assertEqual(record['shots'][1],self.record['shots'][1])

    def test_empty_alignment_or_stale_evidence_cannot_trigger_retake(self):
        for mode in ('empty','alignment','changed_plan','changed_clip'):
            report=copy.deepcopy(self.report)
            if mode=='empty':report['review']['recognized']=''
            elif mode=='alignment':report['review']['issues']=[{'code':'alignment_missing'}]
            elif mode=='changed_plan':report['evidence']['plan']={'turns':[]}
            else:report['evidence']['clip_sha256']='0'*64
            before=(self.path/'record.json').read_bytes()
            with self.subTest(mode=mode),patch.object(video_generation,'generate_for_user') as generate,self.assertRaises(ValueError):
                retake(self.identity,1,{'id':1},self.failure(report))
            generate.assert_not_called();self.assertEqual(before,(self.path/'record.json').read_bytes())

    def test_uncertain_clip_and_nonlocal_backend_submit_nothing(self):
        for mode in ('unknown','cloud','owner'):
            value=copy.deepcopy(self.record)
            if mode=='unknown':value['shots'][0]['video_status']='unknown'
            elif mode=='cloud':value['creation_parameters']['video_generation_backend']='runninghub'
            else:value['native_owner_id']=2
            studio.save(self.path,value)
            with self.subTest(mode=mode),patch.object(video_generation,'generate_for_user') as generate,self.assertRaises(ValueError):
                retake(self.identity,1,{'id':1},self.failure())
            generate.assert_not_called();self.assertEqual(studio.read(self.path),value)

    def test_pause_wins_and_failed_dispatch_does_not_create_an_automatic_loop(self):
        token=one_click._executing.set(True)
        try:
            with patch.object(one_click,'load',return_value={'state':'paused'}),patch.object(video_generation,'generate_for_user') as generate:
                retake(self.identity,1,{'id':1},self.failure())
            generate.assert_not_called();self.assertEqual(studio.read(self.path),self.record)
        finally:one_click._executing.reset(token)
        with patch.object(video_generation,'generate_for_user',side_effect=ValueError('configuration unavailable')),self.assertRaisesRegex(ValueError,'configuration'):
            retake(self.identity,1,{'id':1},self.failure())
        current=studio.read(self.path)
        self.assertEqual(current['shots'][0]['video_task_id'],'known-task')
        with patch.object(video_generation,'generate_for_user') as generate,self.assertRaises(NativeSpeechReviewRequired):
            retake(self.identity,current['revision'],{'id':1},self.failure())
        generate.assert_not_called()
