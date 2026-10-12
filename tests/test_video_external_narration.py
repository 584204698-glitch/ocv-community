import copy
import hashlib
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from backend.app import video_external_narration as narration
from backend.app.h3_dialogue import DialoguePlan,build_prompt
from tests.test_h3_dialogue import data


def plan():
    value=data();value['duration']=9
    identity='pc_'+'d'*32
    value['speakers'].append(dict(character_id=identity,name='旁白',kind='narrator',on_screen=False,
        voice_id='qwen3:'+'3'*32))
    value['turns']=[dict(character_id=identity,text='旁白开场。',start=0,end=2),
        {**value['turns'][0],'start':2.3,'end':4.5},
        dict(character_id=identity,text='旁白结束。',start=5,end=8)]
    spans=[(0,2),(2,2.3),(2.3,4.5),(4.5,5),(5,8),(8,9)]
    value['performance_beats']=[dict(start=a,end=b,action='The woman rests her hand on the table.',action_zh='女子将手放在桌上。') for a,b in spans]
    return DialoguePlan.model_validate(value).model_dump()


class ExternalNarrationTests(unittest.TestCase):
    def test_tts_runner_receives_resolved_reference_file_not_asset_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); folder=root/'narration'; reference=root/'adopted voice.wav'
            output=folder/'segments/segment_0001.wav'
            def run(command, **kwargs):
                self.assertEqual(command[command.index('--tts-voice-path')+1],str(reference))
                self.assertNotIn('--tts-voice-id',command)
                self.assertNotIn('qwen3:asset-id',command)
                self.assertEqual(command[command.index('--tts-engine')+1],'indextts25')
                output.parent.mkdir(parents=True)
                with wave.open(str(output),'wb') as wav:
                    wav.setparams((1,2,24000,0,'NONE','not compressed'))
                    wav.writeframes(b'\0\0'*24000)
                return type('Result',(),{'returncode':0})()
            with patch.object(narration.subprocess,'run',side_effect=run):
                result=narration.synthesize(1,root,'每次入睡。',
                    dict(path=str(reference),voice_id='qwen3:asset-id'),folder)
            self.assertEqual(result,output)
            self.assertEqual((folder/'text.txt').read_text(encoding='utf8'),'每次入睡。')

    def test_measured_duration_moves_later_actions_without_changing_words(self):
        original=plan();before=copy.deepcopy(original)
        render,clips=narration.retime(original,{0:dict(duration=2.85),2:dict(duration=2.5)})
        self.assertEqual(render['duration'],10)
        self.assertEqual(render['turns'][0]['start'],3.3)
        self.assertEqual(render['turns'][0]['end'],5.5)
        self.assertEqual(clips[1]['start'],6)
        self.assertEqual(render['performance_beats'][0]['end'],3)
        self.assertEqual(render['performance_beats'][-1]['end'],10)
        self.assertTrue(all(s['kind']!='narrator' for s in render['speakers']))
        prompt=build_prompt(DialoguePlan.model_validate(render),picture_count=3)
        self.assertNotIn('旁白开场',prompt);self.assertNotIn('旁白结束',prompt)
        self.assertEqual(original,before)

    def test_overlap_and_overflow_refuse_mixing_not_crop_or_mute(self):
        with self.assertRaisesRegex(ValueError,'超过'):
            narration.retime(plan(),{0:dict(duration=14),2:dict(duration=3)})
        with self.assertRaisesRegex(ValueError,'侵入'):
            narration.check_windows([dict(start=0,end=3)],[dict(start=2.9,end=5)],9)
        narration.check_windows([dict(start=0,end=3)],[dict(start=3.2,end=5)],9)
        with self.assertRaisesRegex(ValueError,'超出'):
            narration.check_windows([dict(start=8,end=10)],[],9)

    def test_cache_scope_and_corruption_without_hidden_inference(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);value=plan();record=dict(id='a'*32,creation_parameters=dict(narration_route='external_tts'))
            voices=[dict(character_id=value['speakers'][-1]['character_id'],voice_id='qwen3:'+'3'*32,sha256='a'*64)]
            def synth(owner,path,text,voice,folder):
                folder.mkdir(parents=True,exist_ok=True);file=folder/'voice.wav'
                with wave.open(str(file),'wb') as w:
                    w.setparams((1,2,24000,0,'NONE','not compressed'));w.writeframes(b'\0\0'*24000)
                return file
            with patch.object(narration,'synthesize',side_effect=synth) as call:
                self.assertIsNone(narration.prepare(1,root,record,value,voices))
                call.assert_not_called()
                result=narration.prepare(1,root,record,value,voices,allow_synthesis=True)
                self.assertEqual(call.call_count,2)
                self.assertEqual(narration.prepare(1,root,record,value,voices),result)
                self.assertEqual(call.call_count,2)
                self.assertIsNone(narration.prepare(2,root,record,value,voices))
                voices[0]['sha256']='b'*64
                self.assertIsNone(narration.prepare(1,root,record,value,voices))
                voices[0]['sha256']='a'*64
                narration.asset(root,result['clips'][0]['file']).write_bytes(b'broken')
                with self.assertRaisesRegex(ValueError,'缓存已变化'):
                    narration.prepare(1,root,record,value,voices)

    def test_missing_narrator_reference_is_actionable(self):
        with self.assertRaisesRegex(ValueError,'固定音色'):
            narration.prepare(1,Path('.'),dict(creation_parameters=dict(narration_route='external_tts')),plan(),[])

    def test_mix_retains_original_track_and_uses_sample_delays(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);file=root/'voice.wav';file.write_bytes(b'audio')
            clip=dict(file='voice.wav',sha256=hashlib.sha256(b'audio').hexdigest(),start=1.25)
            inputs,graph=narration.mix_arguments(root,'original.mp4',[clip],.65)
            self.assertEqual(inputs[:2],['-i','original.mp4'])
            self.assertIn('[original][n1]amix',graph)
            self.assertIn('adelay=60000S',graph)
            self.assertNotIn('volume=0,',graph)


class NarrationRouteTests(unittest.TestCase):
    def setUp(self):
        from tests.test_video_inputs import VideoInputTest
        from backend.app import video_studio as studio
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        VideoInputTest.setUp(self)
        self.studio=studio
        self.record['status']='video_review'
        self.record['creation_parameters']['speech_mode']='mixed'
        self.record['shots'][0]['native_dialogue']=plan()
        studio.save(self.path,self.record)
        app=FastAPI();app.include_router(studio.router);self.client=TestClient(app)

    def change(self,revision=1):
        return self.client.post(self.base+'/narration-route',json=dict(revision=revision,route='external_tts'))

    def test_qwen_engine_switch_preserves_plan_assets_and_never_synthesizes(self):
        rows=[dict(character_id=s['character_id'],voice_id=s['voice_id'],status='current',kind=s['kind']) for s in plan()['speakers']]
        route=self.base+'/dialogue-route'
        with patch('backend.app.video_dialogue.voice_view',return_value={'characters':rows}), \
             patch('backend.app.qwen3_local.health',return_value={'ready':True}), \
             patch('backend.app.qwen3_local.resolve_saved_voice',return_value=(self.path/'voice.wav','原文')) as reference, \
             patch('backend.app.qwen3_local.run_batch',side_effect=AssertionError('no inference')):
            response=self.client.post(route,json=dict(revision=1,route='external_tts',tts_engine='qwen3'))
        self.assertEqual(response.status_code,200,response.text)
        after=response.json()
        self.assertEqual(after['creation_parameters']['dialogue_tts_engine'],'qwen3')
        self.assertEqual(after['shots'][0]['native_dialogue'],self.record['shots'][0]['native_dialogue'])
        self.assertEqual(after['shots'][0]['image'],self.record['shots'][0]['image'])
        self.assertEqual(after['shots'][1],self.record['shots'][1])
        self.assertTrue(after['shots'][0]['dialogue_stale'])
        self.assertEqual(reference.call_count,len({t['character_id'] for t in plan()['turns']}))
        self.assertEqual(self.client.post(route,json=dict(revision=after['revision'],route='external_tts',tts_engine='qwen3')).json(),after)

    def test_switch_preserves_assets_and_only_invalidates_narrated_shots(self):
        with patch('backend.app.video_dialogue.voice_view',return_value={
                'characters':[dict(kind='narrator',status='current')]}), \
                patch.object(narration,'synthesize',side_effect=AssertionError('no inference on setting change')):
            response=self.change()
        self.assertEqual(response.status_code,200,response.text)
        record=response.json()
        self.assertTrue(record['shots'][0]['dialogue_stale'])
        self.assertEqual(record['shots'][1],self.record['shots'][1])
        self.assertEqual(record['shots'][0]['image'],self.record['shots'][0]['image'])
        self.assertEqual(record['shots'][0]['native_dialogue'],self.record['shots'][0]['native_dialogue'])
        self.assertEqual(self.change().status_code,409)

    def test_missing_voice_other_owner_and_running_project_cannot_switch(self):
        with patch('backend.app.video_dialogue.voice_view',return_value={
                'characters':[dict(kind='narrator',status='unbound')]}):
            self.assertEqual(self.change().status_code,409)
        with patch.object(self.studio,'require_user',return_value={'id':2}):
            self.assertEqual(self.change().status_code,404)
        self.record['status']='video_generating';self.studio.save(self.path,self.record)
        self.assertEqual(self.change().status_code,409)

    def test_dialogue_route_requires_actual_fixed_voices_and_never_runs_inference(self):
        rows=[dict(character_id=s['character_id'],status='current',kind=s['kind']) for s in plan()['speakers']]
        route=self.base+'/dialogue-route'
        with patch('backend.app.video_dialogue.voice_view',return_value={'characters':rows}), \
             patch('backend.app.video_external_dialogue.synthesize',side_effect=AssertionError('no model call')):
            response=self.client.post(route,json=dict(revision=1,route='external_tts'))
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['creation_parameters']['dialogue_route'],'external_tts')
        self.assertEqual(response.json()['shots'][0]['image'],self.record['shots'][0]['image'])
        self.assertEqual(response.json()['shots'][1],self.record['shots'][1])
        self.assertEqual(self.client.post(route,json=dict(revision=2,route='external_tts')).json(),response.json())
        self.studio.save(self.path,self.record)
        next(row for row in rows if row['character_id']==plan()['turns'][1]['character_id'])['status']='generated'
        with patch('backend.app.video_dialogue.voice_view',return_value={'characters':rows}):
            rejected=self.client.post(route,json=dict(revision=1,route='external_tts'))
        self.assertEqual(rejected.status_code,409)
        self.assertIn('固定音色',rejected.json()['detail'])


class NarrationPreviewRetryTests(unittest.TestCase):
    def setUp(self):
        from tests.test_video_inputs import VideoInputTest
        from backend.app import video_studio as studio
        VideoInputTest.setUp(self)
        self.studio=studio
        self.record['status']='video_review'
        self.record['shots'][0].update(video_status='completed', video='existing.mp4',
            video_request={'external_narration':{'clips':[{'file':'existing.wav'}]}},
            narration_preview_error='temporary failure')
        (self.path/'existing.mp4').write_bytes(b'existing original clip')
        studio.save(self.path,self.record)

    def submit(self):
        return self.client.post(self.base+'/videos/shot-A/narration-preview',json={'revision':1})

    def test_retry_reuses_frozen_assets_and_protects_ownership(self):
        with patch.object(narration,'preview',return_value='mixed.mp4') as call, \
                patch.object(narration,'synthesize',side_effect=AssertionError('must reuse')):
            response=self.submit()
        self.assertEqual(response.status_code,200,response.text)
        record=response.json();shot=record['shots'][0]
        self.assertEqual(shot['narration_preview'],'mixed.mp4')
        self.assertEqual(shot['narration_preview_error'],'')
        self.assertEqual(shot['video_request'],self.record['shots'][0]['video_request'])
        self.assertEqual(shot['video'],'existing.mp4')
        self.assertEqual(record['shots'][1],self.record['shots'][1])
        call.assert_called_once()
        self.assertEqual(self.submit().status_code,409)
        with patch.object(self.studio,'require_user',return_value={'id':2}):
            self.assertEqual(self.submit().status_code,404)

    def test_failed_tts_mux_can_retry_without_another_h3_or_language_request(self):
        from backend.app import video_external_dialogue as exact
        shot=self.record['shots'][0]
        shot.update(video_status='failed',video_terminal=True,video_error='mux failed')
        shot['video_request']={'external_dialogue':{'clips':[{'file':'existing.wav'}]}}
        self.studio.save(self.path,self.record)
        with patch.object(exact,'preview',return_value='dubbed.mp4') as call, \
             patch.object(exact,'synthesize',side_effect=AssertionError('must reuse audio')), \
             patch('backend.app.video_generation.run_video_profile',side_effect=AssertionError('must reuse video')):
            response=self.submit()
        self.assertEqual(response.status_code,200,response.text)
        updated=response.json()['shots'][0]
        self.assertEqual(updated['video_status'],'completed')
        self.assertEqual(updated['video_error'],'')
        self.assertEqual(updated['video'],'existing.mp4')
        self.assertEqual(updated['narration_preview'],'dubbed.mp4')
        self.assertEqual(updated['video_request'],shot['video_request'])
        call.assert_called_once()

    def test_failure_remains_visible_and_concurrent_edit_is_not_overwritten(self):
        with patch.object(narration,'preview',side_effect=ValueError('对白侵入旁白')):
            response=self.submit()
        self.assertEqual(response.status_code,200,response.text)
        self.assertIn('侵入',response.json()['shots'][0]['narration_preview_error'])
        self.studio.save(self.path,self.record)
        def edited(*args):
            changed=copy.deepcopy(self.record);changed['revision']=2
            changed['shots'][0]['video']='replacement.mp4'
            self.studio.save(self.path,changed)
            return 'mixed.mp4'
        with patch.object(narration,'preview',side_effect=edited):
            self.assertEqual(self.submit().status_code,409)
        self.assertEqual(self.studio.read(self.path)['shots'][0]['video'],'replacement.mp4')


if __name__=='__main__':unittest.main()
