import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from PIL import Image

from backend.app.video_native_grounding import ground_performance, SYSTEM
from backend.app.h3_dialogue import validate_dialogue_draft
from tests.test_h3_dialogue import data


class NativeGroundingTests(unittest.TestCase):
    def test_unchanged_chinese_action_reuses_approved_english_without_another_model_call(self):
        self.response['performance_beats'][0]['action']='The approved action continues with the title “片名”.'
        before=copy.deepcopy(self.response)
        ask=Mock(return_value=self.response)
        plan,receipt=self.call(ask=ask)
        self.assertEqual(plan['performance_beats'][0],self.plan['performance_beats'][0])
        self.assertEqual(self.response,before)
        self.assertEqual(ask.call_count,1)
        self.assertEqual(receipt['format_repairs'][0]['beat_index'],0)
        saved=json.loads(next((self.path/'assets/native_grounding').glob('*.json')).read_text(encoding='utf8'))
        self.assertEqual(saved['response'],before)
        self.assertEqual(self.call({**self.shot,'native_dialogue':plan,'native_visual_grounding':receipt},
                                  Mock(side_effect=AssertionError('no new model call'))),(plan,receipt))

    def test_changed_chinese_action_or_tag_injection_is_not_repaired(self):
        self.response['performance_beats'][0]['action']='The action changes to “开灯”.'
        self.response['performance_beats'][0]['action_zh']='角色提前开灯。'
        with self.assertRaisesRegex(ValueError,'用英文'):
            self.call()
        fresh={**self.shot,'id':'tag_injection'}
        self.response['performance_beats'][0]=copy.deepcopy(self.plan['performance_beats'][0])
        self.response['performance_beats'][0]['action']='The action adds <片名>.'
        with self.assertRaisesRegex(ValueError,'不能注入'):
            self.call(fresh)

    def test_pixel_layout_scene_can_align_without_changing_dialogue_or_role_bindings(self):
        before=copy.deepcopy(self.plan)
        self.response['scene']='A fixed medium view of the same table; the two people are side by side.'
        plan,receipt=self.call()
        self.assertEqual(plan['scene'],self.response['scene'])
        for key in ('duration','turns'):
            if key=='turns':
                self.assertEqual([(t['character_id'],t['text'],t['start'],t['end']) for t in plan[key]],
                                 [(t['character_id'],t['text'],t['start'],t['end']) for t in before[key]])
            else:self.assertEqual(plan[key],before[key])
        self.assertEqual([(s['character_id'],s['picture'],s.get('voice_id')) for s in plan['speakers']],
                         [(s['character_id'],s['picture'],s.get('voice_id')) for s in before['speakers']])
        self.assertEqual(self.plan,before)
        self.assertEqual(self.call({**self.shot,'native_dialogue':plan,'native_visual_grounding':receipt},
                                  Mock(side_effect=AssertionError('reuse checked layout'))),(plan,receipt))
        self.response['conflicts']=['required hidden face is exposed']
        with self.assertRaisesRegex(ValueError,'开场图与已批准动作不一致'):
            self.call({**self.shot,'id':'conflicting_layout'},Mock(return_value=self.response))

    def test_format_order_normalizes_without_changing_speech_or_timeline(self):
        self.response['performance_beats'].reverse()
        self.response['mouth_visibility'].reverse()
        plan,_=self.call()
        self.assertEqual(plan['performance_beats'],self.plan['performance_beats'])
        self.assertEqual([t['text'] for t in plan['turns']],[t['text'] for t in self.plan['turns']])

    def test_explicit_retry_escapes_failed_cache_once_without_replacing_old_evidence(self):
        bad=copy.deepcopy(self.response);bad['positions'].pop()
        with self.assertRaises(ValueError):self.call(ask=Mock(return_value=bad))
        cached=list((self.path/'assets/native_grounding').glob('*.json'))[0]
        old=cached.read_bytes()
        with self.assertRaises(ValueError):self.call(ask=Mock(side_effect=AssertionError('no silent retry')))
        retry={**self.shot,'native_grounding_retry':1,'native_grounding_force_recheck':True}
        from backend.app.video_native_grounding import reuse_asset_only_grounding
        self.assertIsNone(reuse_asset_only_grounding(self.path,retry,self.image))
        ask=Mock(return_value=self.response)
        plan,receipt=self.call(retry,ask)
        self.assertEqual(ask.call_count,1)
        self.assertEqual(cached.read_bytes(),old)
        self.assertEqual(self.call(retry,Mock(side_effect=AssertionError('reuse same retry'))),(plan,receipt))
        self.assertEqual(len(list((self.path/'assets/native_grounding').glob('*.json'))),2)

    def test_reordered_positions_bind_by_id_without_reordering_cast(self):
        self.response['positions'].reverse()
        plan,_=self.call()
        self.assertEqual([s['character_id'] for s in plan['speakers']],
                         [s['character_id'] for s in self.plan['speakers']])
        self.assertIn('behind the curtain',plan['speakers'][0]['position'])
        self.assertEqual(plan['speakers'][1]['position'],self.plan['speakers'][1]['position'])

    def test_redundant_known_narrator_position_reuses_cached_response_without_call(self):
        narrator=dict(character_id='pc_'+'e'*32,name='Narrator',kind='narrator',
                      on_screen=False,picture=None,position='',appearance='',
                      voice_description='Gentle voice',voice_mode='generated')
        self.shot['native_dialogue']['speakers'].append(narrator)
        self.response['positions'].append(dict(character_id=narrator['character_id'],
                    position='Offscreen narrator; no body or face is visible.'))
        self.response['positions'].reverse()
        plan,receipt=self.call()
        self.assertEqual(plan['speakers'][-1]['position'],'')
        self.assertFalse(plan['speakers'][-1]['on_screen'])
        self.assertEqual(plan['turns'][0]['text'],self.plan['turns'][0]['text'])
        self.assertEqual(self.call(ask=Mock(side_effect=AssertionError('must use retained response'))),(plan,receipt))
        self.assertEqual(self.ask.call_count,1)

    def test_duplicate_and_missing_positions_remain_blocked(self):
        duplicate=copy.deepcopy(self.response)
        duplicate['positions'].append(copy.deepcopy(duplicate['positions'][0]))
        missing=copy.deepcopy(self.response);missing['positions'].pop()
        for index,response in enumerate((duplicate,missing)):
            with self.subTest(index=index),self.assertRaises(ValueError):
                self.call({**self.shot,'id':f'bad_{index}'},Mock(return_value=response))

    def test_voice_only_retry_reuses_checked_motion_without_model(self):
        from backend.app.video_native_grounding import reuse_asset_only_grounding
        plan,receipt=self.call()
        changed=copy.deepcopy(plan)
        changed['speakers'][0]['voice_id']='qwen3:'+'9'*32
        result=reuse_asset_only_grounding(self.path,{**self.shot,'native_dialogue':changed,
                    'native_visual_grounding':receipt},self.image)
        self.assertEqual(result[0],changed)
        self.assertEqual(result[1]['status'],'reused')
        self.assertEqual(self.ask.call_count,1)
        changed['turns'][0]['text']='这是一句新台词。'
        self.assertIsNone(reuse_asset_only_grounding(self.path,{**self.shot,'native_dialogue':changed,
                    'native_visual_grounding':receipt},self.image))

    def test_emotion_only_retry_reuses_motion_without_model(self):
        from backend.app.video_native_grounding import reuse_asset_only_grounding
        plan,receipt=self.call()
        changed=copy.deepcopy(plan)
        changed['turns'][0].update(emotion='surprised',emotion_strength=.4)
        result=reuse_asset_only_grounding(self.path,{**self.shot,'native_dialogue':changed,
                    'native_visual_grounding':receipt},self.image)
        self.assertEqual(result[0],changed)
        self.assertEqual(result[1]['status'],'reused')
        self.assertEqual(self.ask.call_count,1)
        changed['turns'][0]['end']-=.2
        changed.pop('performance_beats')
        self.assertIsNone(reuse_asset_only_grounding(self.path,{**self.shot,'native_dialogue':changed,
                    'native_visual_grounding':receipt},self.image))

    def test_replaced_core_keeps_motion_but_never_claims_old_visual_observation(self):
        from backend.app.video_native_grounding import reuse_asset_only_grounding
        plan,receipt=self.call()
        Image.new('RGB',(32,32),'red').save(self.image)
        result=reuse_asset_only_grounding(self.path,{**self.shot,'native_dialogue':plan,
                    'native_visual_grounding':receipt},self.image)
        self.assertEqual(result[0],plan)
        self.assertEqual(result[1]['status'],'not_rechecked')
        self.assertNotIn('observations',result[1])
        self.assertEqual(self.ask.call_count,1)

    def test_explicit_keep_prompt_asset_adoption_does_not_need_old_grounding(self):
        from backend.app.video_native_grounding import reuse_asset_only_grounding
        shot={**self.shot,'video_prompt_input_basis':{'source':'asset_replacement'}}
        result=reuse_asset_only_grounding(self.path,shot,self.image)
        self.assertEqual(result[0],self.plan)
        self.assertEqual(result[1]['status'],'not_rechecked')
        self.ask.assert_not_called()

    def test_seated_rabbit_and_fox_can_turn_from_each_other_toward_open_door(self):
        # Frozen shot_001 staging from the retained birthday-story record.
        original=copy.deepcopy(self.plan)
        for speaker,name in zip(original['speakers'],('Rabbit','Fox')):
            speaker['name']=name
        original['turns']=[]
        original['duration']=5
        original['scene']='Rabbit and Fox sit beside the birthday table; an open doorway is on the right.'
        original['performance_beats']=[dict(start=0,end=5,
            action='Rabbit glances toward the open doorway; Fox notices the empty third chair. Neither changes places.',
            action_zh='兔子望向敞开的门，狐狸看了看空着的第三把椅子；两人都没有换位。')]
        response=dict(positions=[dict(character_id=s['character_id'],
            position='Seated beside the table, initially facing the other character.') for s in original['speakers']],
            performance_beats=[dict(start=0,end=5,
                action='Initially facing Fox, Rabbit naturally turns its head toward the open doorway. Fox glances from Rabbit to the empty third chair. Both remain seated.',
                action_zh='开场兔子面向狐狸，随后自然转头望向敞开的门；狐狸从兔子转而看向空着的第三把椅子。两人保持坐姿。')],
            mouth_visibility=[],observations='兔子和狐狸坐在桌旁互看，右侧门已打开，第三把椅子空着。',conflicts=[])
        ask=Mock(return_value=response)
        shot={**self.shot,'native_dialogue':original,'action':'两人望向敞开的门。'}
        plan,receipt=self.call(shot,ask)
        self.assertEqual(plan['speakers'][0]['character_id'],original['speakers'][0]['character_id'])
        self.assertEqual(plan['turns'],[])
        self.assertEqual(plan['performance_beats'][0]['end'],5)
        self.assertIn('naturally turns its head',plan['performance_beats'][0]['action'])
        self.assertIn('静态核心图中已经达到动作目标',ask.call_args.args[0])
        self.assertIn('不报conflicts',SYSTEM)
        self.assertEqual(ask.call_count,1)
        self.assertEqual(self.call(shot,Mock(side_effect=AssertionError('no second audit'))),(plan,receipt))

    def test_missing_or_replaced_role_and_missing_required_prop_remain_hard_conflicts(self):
        for index,conflict in enumerate(('开场应在场的狐狸缺失。','狐狸被另一只兔子替换。','剧情必需的第三把椅子缺失。')):
            response=copy.deepcopy(self.response);response['conflicts']=[conflict]
            ask=Mock(return_value=response)
            with self.subTest(conflict=conflict),self.assertRaisesRegex(ValueError,'尚未提交H3'):
                self.call({**self.shot,'id':f'hard_{index}'},ask)
            self.assertEqual(ask.call_count,1)

    def test_resting_hand_can_advance_to_approved_gesture_without_changing_speech(self):
        original=copy.deepcopy(self.plan)
        original['performance_beats'][1].update(action='The left woman raises her hand to point toward the door.',
                                                action_zh='左侧女子抬手指向门口。')
        response=copy.deepcopy(self.response)
        response['performance_beats']=copy.deepcopy(original['performance_beats'])
        response['performance_beats'][1].update(
            action='Starting with her hand resting on the table, the left woman raises it and points toward the door.',
            action_zh='左侧女子从手放在桌上的姿态抬手，指向门口。')
        response['observations']='两名女子在场，左侧女子手放在桌上，门口可见。'
        ask=Mock(return_value=response)
        plan,_=self.call({**self.shot,'native_dialogue':original},ask)
        self.assertEqual([(t['character_id'],t['text'],t['start'],t['end']) for t in plan['turns']],
                         [(t['character_id'],t['text'],t['start'],t['end']) for t in original['turns']])
        self.assertEqual([s['voice_id'] for s in plan['speakers']],[s['voice_id'] for s in original['speakers']])
        self.assertIn('既定抬手/指向/示意手势',ask.call_args.args[0])
        self.assertEqual(ask.call_count,1)

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name);self.image=self.path/'core.png'
        Image.new('RGB',(32,32),'green').save(self.image)
        value=data()
        value['performance_beats']=[dict(start=a,end=b,action='The women keep their planned positions.',
            action_zh='两名女子保持既定位置。') for a,b in ((0,.2),(.2,2.7),(2.7,3),(3,5.5),(5.5,6))]
        self.plan=validate_dialogue_draft(value)
        self.shot=dict(id='shot_001',action='女子在遮挡后回答。',image_prompt='只见一只手。',
                       native_dialogue=self.plan)
        self.response=dict(positions=[dict(character_id=s['character_id'],position=s['position'])
            for s in self.plan['speakers']],performance_beats=copy.deepcopy(self.plan['performance_beats']),
            observations='图中一名女子被帘子挡住，只见一只手。',conflicts=[],
            mouth_visibility=[dict(turn_index=i,character_id=t['character_id'],mouth_visible=True,
                basis='The speaker is visible in the approved composition.') for i,t in enumerate(self.plan['turns'])])
        self.response['positions'][0]['position']='behind the curtain, only one hand visible; face concealed'
        self.ask=Mock(return_value=self.response)

    def call(self,shot=None,ask=None):
        return ground_performance(self.path,shot or self.shot,self.image,ask=ask or self.ask)

    def test_image_grounding_preserves_words_voices_presence_and_exact_timing(self):
        original=copy.deepcopy(self.shot)
        plan,receipt=self.call()
        self.assertEqual([{k:v for k,v in t.items() if k!='mouth_visible'} for t in plan['turns']],self.plan['turns'])
        self.assertEqual(plan['duration'],self.plan['duration'])
        self.assertEqual(plan['scene'],self.plan['scene'])
        for actual,old in zip(plan['speakers'],self.plan['speakers']):
            for key in set(old)-{'position'}:self.assertEqual(actual[key],old[key])
        self.assertIn('face concealed',plan['speakers'][0]['position'])
        self.assertEqual(self.shot,original)
        self.assertEqual(self.ask.call_args.args[2]['mime_type'],'image/jpeg')
        self.assertTrue(receipt['observations'])

    def test_repeat_and_restart_reuse_evidence_without_another_language_call(self):
        plan,receipt=self.call()
        no=Mock(side_effect=AssertionError('must reuse'))
        self.assertEqual(self.call(ask=no),(plan,receipt))
        saved={**self.shot,'native_dialogue':plan,'native_visual_grounding':receipt}
        self.assertEqual(self.call(saved,no),(plan,receipt))
        self.assertFalse(no.called)

    def test_surface_sound_alignment_keeps_dialogue_scene_and_timing(self):
        response=copy.deepcopy(self.response)
        response['soundscape']='Shoes scrape softly on stone paving.'
        plan,receipt=self.call(ask=Mock(return_value=response))
        self.assertEqual(plan['soundscape'],response['soundscape'])
        self.assertEqual(plan['scene'],self.plan['scene'])
        self.assertEqual(plan['duration'],self.plan['duration'])
        self.assertEqual([{k:v for k,v in t.items() if k!='mouth_visible'} for t in plan['turns']],self.plan['turns'])
        saved={**self.shot,'native_dialogue':plan,'native_visual_grounding':receipt}
        self.assertEqual(self.call(saved,Mock(side_effect=AssertionError('cached'))),(plan,receipt))

    def test_overlong_position_repairs_only_length_once_and_reuses_result(self):
        long=copy.deepcopy(self.response);long['positions'][0]['position']='hidden behind curtain; '*20
        ask=Mock(side_effect=[long,self.response])
        plan,receipt=self.call(ask=ask)
        self.assertEqual(ask.call_count,2)
        self.assertEqual(plan['turns'][0]['text'],self.plan['turns'][0]['text'])
        self.assertIn('output_schema',ask.call_args.args[1])
        saved=json.loads((self.path/'assets/native_grounding'/f'{receipt["fingerprint"]}.json').read_text(encoding='utf8'))
        self.assertEqual(saved['before_length_repair'],long)
        self.assertEqual(self.call(ask=Mock(side_effect=AssertionError('cached'))),(plan,receipt))

    def test_length_repair_cannot_erase_conflicts_and_failure_does_not_reissue(self):
        long=copy.deepcopy(self.response);long['positions'][0]['position']='hidden behind curtain; '*20
        long['conflicts']=['The expected face is exposed.']
        ask=Mock(side_effect=[long,self.response])
        with self.assertRaisesRegex(ValueError,'改变了其他字段'):self.call(ask=ask)
        self.assertEqual(ask.call_count,2)
        with self.assertRaises(ValueError):self.call(ask=Mock(side_effect=AssertionError('must not retry')))

    def test_changed_picture_or_instruction_invalidates_preparation(self):
        plan,receipt=self.call()
        saved={**self.shot,'native_dialogue':plan,'native_visual_grounding':receipt}
        Image.new('RGB',(32,32),'blue').save(self.image)
        self.call(saved)
        self.assertEqual(self.ask.call_count,2)
        self.call({**saved,'action':'女子从帘子后走出。'})
        self.assertEqual(self.ask.call_count,3)

    def test_foreign_roles_time_changes_and_extra_dialogue_cannot_be_applied(self):
        bads=[]
        role=copy.deepcopy(self.response);role['positions'][0]['character_id']='pc_'+'f'*32;bads.append(role)
        time=copy.deepcopy(self.response);time['performance_beats'][0]['end']=.3;bads.append(time)
        words=copy.deepcopy(self.response);words['turns']=[];bads.append(words)
        for n,response in enumerate(bads):
            with self.subTest(n=n),self.assertRaises(ValueError):
                self.call({**self.shot,'id':f'shot_{n+2:03}'},Mock(return_value=response))

    def test_visible_conflict_is_retained_and_stops_instead_of_becoming_new_story(self):
        self.response['conflicts']=['要求隐藏脸，实际图中整张脸可见。']
        with self.assertRaisesRegex(ValueError,'开场图与已批准动作不一致'):self.call()
        self.assertEqual(len(list((self.path/'assets/native_grounding').glob('*.json'))),1)
        with self.assertRaisesRegex(ValueError,'开场图与已批准动作不一致'):
            self.call(ask=Mock(side_effect=AssertionError('do not repeat failed call')))

    def test_cache_tampering_cannot_substitute_other_words(self):
        plan,receipt=self.call()
        target=self.path/'assets/native_grounding'/f'{receipt["fingerprint"]}.json'
        value=json.loads(target.read_text(encoding='utf8'))
        value['evidence']['plan']['turns'][0]['text']='改成另一句话。'
        target.write_text(json.dumps(value),encoding='utf8')
        with self.assertRaisesRegex(ValueError,'缓存与当前镜头不一致'):
            self.call({**self.shot,'native_dialogue':plan,'native_visual_grounding':receipt})

    def test_another_project_image_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as elsewhere:
            file=Path(elsewhere)/'core.png';Image.new('RGB',(32,32),'red').save(file)
            with self.assertRaisesRegex(ValueError,'不属于当前项目'):
                ground_performance(self.path,self.shot,file,ask=self.ask)

    def test_mouth_visibility_cannot_be_missing_duplicated_or_assigned_to_other_role(self):
        for index,kind in enumerate(('missing','duplicate','role')):
            value=copy.deepcopy(self.response)
            if kind=='missing':value['mouth_visibility']=[]
            elif kind=='duplicate':value['mouth_visibility'][1]=copy.deepcopy(value['mouth_visibility'][0])
            else:value['mouth_visibility'][0]['character_id']='pc_'+'f'*32
            with self.subTest(kind=kind),self.assertRaisesRegex(ValueError,'发言可见性'):
                self.call({**self.shot,'id':f'shot_bad{index}'},Mock(return_value=value))


if __name__=='__main__':unittest.main()
