import copy
import hashlib
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from backend.app import video_external_dialogue as exact
from backend.app.comfyui_bridge import _patch_dialogue_audio_guide, _patch_opening_guide
from backend.app.h3_dialogue import DialoguePlan, build_prompt
from tests.test_video_external_narration import plan
from tests.test_h3_opening_guide import OpeningGuideTests


def audio(folder,seconds):
    folder.mkdir(parents=True,exist_ok=True);file=folder/'voice.wav'
    with wave.open(str(file),'wb') as wav:
        wav.setparams((1,2,24000,0,'NONE','not compressed'));wav.writeframes(b'\0\0'*round(seconds*24000))
    return file


class ExternalDialogueTests(unittest.TestCase):
    def test_narration_time_wrappers_preserve_actions_and_quoted_or_unmatched_prose(self):
        cases=[
            ('As the narrator speaks, The woman turns, then lowers her gaze.',
             'The woman turns, then lowers her gaze.'),
            ('During the narration, The woman nods.', 'The woman nods.'),
            ('While the narrator speaks, The woman waves.', 'The woman waves.'),
            ('When the narration ends, The woman rests her hand.', 'The woman rests her hand.'),
            ('The woman keeps watching as the narration ends.', 'The woman keeps watching.'),
            ('旁白讲述时，女子转头，然后低头。', '女子转头，然后低头。'),
            ('旁白结束时，女子仍看着对方。', '女子仍看着对方。'),
            ('在旁白讲述期间，女子拿起杯子。', '女子拿起杯子。'),
            ('随着旁白响起，女子点头。', '女子点头。'),
        ]
        for action,expected in cases:
            with self.subTest(action=action):
                self.assertEqual(exact._without_narration_cue(action),expected)
        for action in ('The narrator closes the book.',
                       'The woman says, "During the narration, turn left."',
                       'The woman quotes "I wait as the narration ends."',
                       '女子说：“旁白讲述时，向左转。”',
                       'As the narrator speaks to the woman, she turns.',
                       'During the narration rehearsal, the woman waves.'):
            with self.subTest(action=action):
                self.assertEqual(exact._without_narration_cue(action),action)

    def test_narrator_only_projection_changes_no_times_actions_or_full_audio_words(self):
        value=plan();narrator=value['speakers'][-1]['character_id']
        for turn in value['turns']:turn['character_id']=narrator
        for beat in value['performance_beats']:
            beat['action']='During the narration, The woman raises the cup and takes a sip.'
            beat['action_zh']='旁白讲述时，女子端起杯子喝一口。'
        before=copy.deepcopy(value)
        durations={i:dict(duration=t['end']-t['start']-.15) for i,t in enumerate(value['turns'])}
        visual,clips=exact.retime(value,durations)
        self.assertEqual(value,before)
        self.assertEqual(visual['turns'],[])
        self.assertEqual([(b['start'],b['end']) for b in visual['performance_beats']],
                         [(b['start'],b['end']) for b in value['performance_beats']])
        self.assertEqual([c['text'] for c in clips],[t['text'] for t in value['turns']])
        self.assertTrue(all(c['kind']=='narrator' for c in clips))
        for beat in visual['performance_beats']:
            self.assertEqual(beat['action'],'Without speaking: The woman raises the cup and takes a sip.')
            self.assertEqual(beat['action_zh'],'不说台词：女子端起杯子喝一口。')
        prompt=build_prompt(DialoguePlan.model_validate(visual),picture_count=3,dialogue_audio_guide=True)
        self.assertNotIn('During the narration',prompt)
        self.assertNotIn('<d>',prompt)
        self.assertIn('takes a sip',prompt)
        self.assertEqual(prompt.count('keeps its mouth naturally closed'),2)
        self.assertEqual(prompt.count('Mouth movement occurs only when required by an explicitly planned nonverbal action.'),2)

    def test_mixed_projection_preserves_character_speech_and_only_marks_silent_beats(self):
        value=plan();before=copy.deepcopy(value)
        visual,clips=exact.retime(value,{i:dict(duration=1.5) for i in range(3)})
        for beat in visual['performance_beats']:
            overlaps=any(max(beat['start'],t['start'])<min(beat['end'],t['end']) for t in visual['turns'])
            self.assertEqual(beat['action'].startswith('Without speaking: '),not overlaps)
            self.assertEqual(beat['action_zh'].startswith('不说台词：'),not overlaps)
        self.assertEqual(visual['turns'][0]['text'],value['turns'][1]['text'])
        self.assertEqual([c['text'] for c in clips],[t['text'] for t in value['turns']])
        self.assertEqual(value,before)
        again=copy.deepcopy(visual);exact._visual_beats(again)
        self.assertEqual(again,visual)
        prompt=build_prompt(DialoguePlan.model_validate(visual),picture_count=3,dialogue_audio_guide=True)
        self.assertIn('articulates the supplied recording',prompt)
        self.assertIn('All mouths rest after their recording ends and during guide silence.',prompt)
        self.assertNotIn('旁白开场',prompt)

    def test_qwen_synthesis_uses_main_thread_supervisor_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)/'clip'
            def run(command,**kwargs):
                self.assertIn('backend.app.qwen_dialogue_synthesis',command)
                self.assertEqual(command[command.index('--voice-id')+1],'qwen3:'+'a'*32)
                output=Path(command[command.index('--output')+1])
                file=audio(output,1);file.rename(output/'chunk-0001.wav')
                return type('Result',(),{'returncode':0})()
            with patch.object(exact,'qwen_reference'),patch.object(exact.subprocess,'run',side_effect=run):
                result=exact.synthesize_qwen(1,'真的吗？',dict(voice_id='qwen3:'+'a'*32,sha256='b'*64),folder)
            self.assertEqual(exact.wav_seconds(result),1)
            self.assertEqual((folder/'text.txt').read_text(encoding='utf8'),'真的吗？')

    def test_qwen_uses_adopted_reference_and_ignores_index_emotion(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp);value=plan()
            record={'id':'a'*32,'creation_parameters':{'dialogue_route':'external_tts','dialogue_tts_engine':'qwen3'}}
            voices=[{'character_id':s['character_id'],'voice_id':s['voice_id'],'sha256':'b'*64} for s in value['speakers']]
            def track(root,folder,clips,duration,**kwargs):
                file=audio(folder,duration)
                return dict(file=file.relative_to(root).as_posix(),sha256=hashlib.sha256(file.read_bytes()).hexdigest(),duration=duration)
            with patch.object(exact,'qwen_reference',return_value=(path/'ref.wav','reference words')),patch.object(exact,'synthesize_qwen',side_effect=lambda owner,text,voice,folder:audio(folder,1.5)) as qwen,patch.object(exact,'synthesize') as index,patch.object(exact,'make_track',side_effect=track):
                self.assertIsNone(exact.prepare(1,path,record,value,voices))
                qwen.assert_not_called()
                first=exact.prepare(1,path,record,value,voices,allow_synthesis=True)
                self.assertEqual(qwen.call_count,3);index.assert_not_called()
                self.assertEqual(first['clips'][0]['engine'],'qwen3')
                value['turns'][1].update(emotion='happy',emotion_strength=.8)
                self.assertEqual(exact.prepare(1,path,record,value,voices,allow_synthesis=True)['clips'],first['clips'])
                self.assertEqual(qwen.call_count,3)
                record['creation_parameters']['dialogue_tts_engine']='indextts25'
                self.assertIsNone(exact.prepare(1,path,record,value,voices))
                index.assert_not_called()

    def test_qwen_rejects_changed_reference_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            reference=audio(Path(tmp),1)
            with patch('backend.app.qwen3_local.resolve_saved_voice',return_value=(reference,'原文')):
                with self.assertRaisesRegex(ValueError,'发生变化'):
                    exact.qwen_reference(1,dict(voice_id='qwen3:'+'a'*32,sha256='0'*64))

    def test_measured_slots_shrink_without_deleting_words_or_actions(self):
        value=plan();before=copy.deepcopy(value)
        visual,clips=exact.retime(value,{i:{'duration':1.5} for i in range(3)})
        self.assertLess(visual['duration'],value['duration'])
        self.assertEqual([c['text'] for c in clips],[t['text'] for t in value['turns']])
        self.assertEqual([c['character_id'] for c in clips],[t['character_id'] for t in value['turns']])
        self.assertTrue(all(s['kind']=='character' for s in visual['speakers']))
        self.assertTrue(all(t['character_id']!=value['speakers'][-1]['character_id'] for t in visual['turns']))
        self.assertTrue(all(b['action'].removeprefix('Without speaking: ') in [x['action'] for x in value['performance_beats']] or 'physical end-state' in b['action'] for b in visual['performance_beats']))
        self.assertEqual(visual['turns'][0]['end'],next(c['end'] for c in clips if c['kind']=='character'))
        self.assertEqual(value,before)
        prompt=build_prompt(DialoguePlan.model_validate(visual),picture_count=3,dialogue_audio_guide=True)
        self.assertNotIn('<Audio ',prompt)
        self.assertNotIn('旁白开场',prompt)
        self.assertIn('timed audio guide',prompt)
        self.assertIn('not a voice-cloning sample',prompt)
        self.assertIn('articulates the supplied recording',prompt)
        self.assertIn('speaking mouth at rest',prompt)

    def test_prepare_is_project_scoped_reuses_audio_and_excludes_narration_from_guide(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp);value=plan();record={'id':'a'*32,'creation_parameters':{'dialogue_route':'external_tts'}}
            voices=[{'character_id':s['character_id'],'voice_id':s['voice_id'],'sha256':'b'*64,'path':'adopted.wav'} for s in value['speakers']]
            def synth(owner,root,text,voice,folder,**kwargs):return audio(folder,1.5)
            def track(root,folder,clips,duration,**kwargs):
                self.assertTrue(all(c['kind']=='character' for c in clips))
                file=audio(folder,duration)
                return {'file':file.relative_to(root).as_posix(),'sha256':hashlib.sha256(file.read_bytes()).hexdigest(),'duration':duration}
            with patch.object(exact,'synthesize',side_effect=synth) as synth_call,patch.object(exact,'make_track',side_effect=track):
                self.assertIsNone(exact.prepare(1,path,record,value,voices))
                synth_call.assert_not_called()
                first=exact.prepare(1,path,record,value,voices,allow_synthesis=True)
                self.assertEqual(synth_call.call_count,3)
                self.assertEqual(exact.prepare(1,path,record,value,voices),first)
                self.assertEqual(synth_call.call_count,3)
                value['performance_beats'][0]['action']='During the narration, The woman lowers her gaze.'
                changed=exact.prepare(1,path,record,value,voices,allow_synthesis=True)
                self.assertEqual(changed['clips'],first['clips'])
                self.assertNotEqual(changed['key'],first['key'])
                self.assertEqual(synth_call.call_count,3)
                value['turns'][1].update(emotion='happy')
                second=exact.prepare(1,path,record,value,voices,allow_synthesis=True)
                self.assertEqual(synth_call.call_count,4)
                self.assertEqual(first['clips'][0]['key'],second['clips'][0]['key'])
                self.assertNotEqual(first['clips'][1]['key'],second['clips'][1]['key'])
                self.assertIsNone(exact.prepare(2,path,record,value,voices))
                exact.asset(path,second['clips'][0]['file']).write_bytes(b'corrupt')
                with self.assertRaisesRegex(ValueError,'缓存已变化'):
                    exact.prepare(1,path,record,value,voices)

    def test_missing_adopted_voice_and_duration_overflow_are_actionable(self):
        record={'id':'a'*32,'creation_parameters':{'dialogue_route':'external_tts'}}
        with self.assertRaisesRegex(ValueError,'每位发言者'):
            exact.prepare(1,Path('.'),record,plan(),[])
        with self.assertRaisesRegex(ValueError,'未截断'):
            exact.retime(plan(),{i:{'duration':8} for i in range(3)})

    def test_audio_guide_chains_after_image_guide_preserving_model_latent_and_refs(self):
        graph,mappings=OpeningGuideTests().graph()
        graph['10']['inputs']['audio_vae']=['audio_vae',0]
        _patch_opening_guide(graph,mappings,enabled=True)
        old_inputs=copy.deepcopy(graph['10']['inputs'])
        _patch_dialogue_audio_guide(graph,mappings,'timed.wav')
        guide=graph['ocv_dialogue_audio_guide']['inputs']
        self.assertEqual(guide['positive'],['ocv_core_opening_guide',0])
        self.assertEqual(guide['audio_vae'],['audio_vae',0])
        self.assertEqual(guide['frame_idx'],0)
        self.assertEqual(graph['10']['inputs'],old_inputs)
        self.assertEqual(graph['sampler']['inputs']['latent_image'],['10',1])
        self.assertEqual(graph['guider']['inputs']['conditioning'],['ocv_dialogue_audio_guide',0])
        with self.assertRaisesRegex(ValueError,'重复'):_patch_dialogue_audio_guide(graph,mappings,'duplicate.wav')


if __name__=='__main__':unittest.main()
