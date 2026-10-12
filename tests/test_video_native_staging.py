import copy
import unittest
from unittest.mock import Mock, patch

from backend.app import video_native_staging as staging, video_prompt_refresh as refresh
from backend.app.video_prompt_inputs import content_fingerprint
from backend.app.h3_dialogue import DialoguePlan, build_prompt
from tests import test_video_native_story as fixtures
from backend.app import video_studio as studio


class NativeStagingTest(unittest.TestCase):
    def test_external_audio_route_reaches_local_planner_without_native_voice_instructions(self):
        before=copy.deepcopy(self.shot['native_dialogue'])
        ask=Mock(return_value=self.result)
        result,_=staging.refresh({'video_direction':{'dialogue_route':'external_tts'}},'水彩',
            self.shot,self.f.refs,basis='action',action='保持既有动作。',image_prompt='原图',ask=ask)
        instruction=ask.call_args.args[0]
        self.assertNotIn('H3 直接生成这些话',instruction)
        self.assertIn('H3 接收对白音轨作为口型约束',instruction)
        self.assertIn('旁白独立后期合成',instruction)
        self.assertEqual(result['native_dialogue']['turns'],before['turns'])
        self.assertIn('旁白不传入H3',result['video_prompt'])

    def test_native_audio_route_keeps_original_generation_contract(self):
        ask=Mock(return_value=self.result)
        staging.refresh({'video_direction':{'dialogue_route':'h3_native'}},'水彩',self.shot,
            self.f.refs,basis='action',action='保持既有动作。',image_prompt='原图',ask=ask)
        self.assertIn('H3 直接生成这些话',ask.call_args.args[0])

    def setUp(self):
        self.f=fixtures.NativeDirectorTests();self.f.setUp()
        _,shots=__import__('backend.app.video_native_story',fromlist=['compile_storyboard']).compile_storyboard(
            self.f.raw,self.f.record,self.f.cast,self.f.refs)
        self.shot=shots[0]
        self.result=dict(scene='The rabbit takes two steps from the door toward the bear and stops beside the umbrella.',
            soundscape='Footsteps and quiet rain outside.',positions=[dict(character_id=i,position=p) for i,p in
            zip(self.f.ids[:2],['left of the door','beside the umbrella'])],action='小兔从门边走到小熊身旁。',
            opening='小兔站在门左侧，小熊站在伞边。',image_prompt='水彩绘本，小兔站门左侧，小熊在伞边，单一室内空间。')

    def test_native_refresh_uses_audio_aware_path_and_preserves_every_frozen_word(self):
        before=copy.deepcopy(self.shot['native_dialogue'])
        with patch('backend.app.video_agents.ask_json',return_value=self.result), \
             patch.object(refresh,'revise_motion',side_effect=AssertionError('Narration motion called')), \
             patch.object(refresh,'write_video_prompts',side_effect=AssertionError('Narration finalizer called')):
            result,_=refresh.refresh({'project_cast':self.f.cast},'水彩',self.shot,self.f.refs,
                basis='action',action='小兔从门边走到小熊身旁。',image_prompt=self.shot['image_prompt'])
        self.assertEqual(result['native_dialogue']['turns'],before['turns'])
        self.assertEqual(result['native_dialogue']['duration'],before['duration'])
        self.assertEqual([s['character_id'] for s in result['native_dialogue']['speakers']],
                         [s['character_id'] for s in before['speakers']])
        plan=copy.deepcopy(result['native_dialogue'])
        for index,speaker in enumerate(plan['speakers']):speaker['voice_id']='qwen3:'+str(index+1)*32
        actual=build_prompt(DialoguePlan.model_validate(plan),picture_count=3)
        self.assertIn(self.result['scene'],actual)
        self.assertIn('一起走吧',actual)
        self.assertNotIn('视频只用无声',actual)

    def test_changed_role_in_translation_cannot_change_binding(self):
        bad=copy.deepcopy(self.result);bad['positions'][0]['character_id']='pc_'+'f'*32
        with self.assertRaisesRegex(ValueError,'角色名单'):
            staging.refresh({},'水彩',self.shot,self.f.refs,basis='action',action='走近',image_prompt='门前',ask=lambda *_:bad)

    def test_design_refresh_replaces_stale_opening_without_changing_frozen_dialogue(self):
        self.shot['visual_description']='旧全景：整座小屋及人物全身。'
        for index, speaker in enumerate(self.shot['native_dialogue']['speakers']):
            speaker['voice_id']='qwen3:'+str(index+1)*32
        before=copy.deepcopy(self.shot)
        result,_=staging.refresh({},'水彩',self.shot,self.f.refs,basis='action',
            action=self.result['action'],image_prompt=self.shot['image_prompt'],ask=Mock(return_value=self.result))
        self.assertEqual(result['visual_description'],result['motion_plan']['reference_visual'])
        self.assertNotEqual(result['visual_description'],before['visual_description'])
        self.assertEqual(result['native_dialogue']['turns'],before['native_dialogue']['turns'])
        for old,new in zip(before['native_dialogue']['speakers'],result['native_dialogue']['speakers']):
            for key in old.keys()-{'position'}:self.assertEqual(old[key],new[key])
        self.assertEqual(self.shot,before)
        observed,_=staging.refresh({},'水彩',self.shot,self.f.refs,basis='image',
            action='',image_prompt=self.shot['image_prompt'],ask=Mock(return_value=self.result))
        self.assertEqual(observed['visual_description'],before['visual_description'])

    def test_refresh_preserves_later_entry_and_only_opening_participants_for_core(self):
        self.shot['native_dialogue']['speakers'][1]['presence']='later'
        before=copy.deepcopy(self.shot['native_dialogue'])
        result,_=staging.refresh({},'水彩',self.shot,self.f.refs,basis='video_inputs',
            action='',image_prompt='小兔独自站门边。',ask=Mock(return_value=self.result))
        self.assertEqual(result['native_dialogue']['speakers'][1]['presence'],'later')
        self.assertEqual(result['native_dialogue']['turns'],before['turns'])
        self.assertEqual(result['motion_plan']['reference_participants'],['小兔'])
        self.assertEqual(result['motion_plan']['participants'],['小兔','小熊'])


    def test_actual_image_analysis_used_only_when_requested_and_cached(self):
        ask=Mock(return_value=self.result)
        with patch.object(refresh,'analyze_image',side_effect=AssertionError('No extra vision call')):
            staging.refresh({},'水彩',self.shot,self.f.refs,basis='image',action='走近',image_prompt='旧设计',
                force_vision=True,image_analysis={'description':'实际图中小兔在左'},ask=ask)
        self.assertEqual(ask.call_args.args[1]['core_basis'],{'kind':'image_analysis','description':'实际图中小兔在左'})

    def test_fixed_scene_design_reaches_local_revision_without_becoming_new_asset(self):
        fixed={'id':'scene_original','image_prompt':'门牌上逐字写“松果书屋”。'}
        self.shot['approved_scene_design']=copy.deepcopy(fixed)
        ask=Mock(return_value=self.result)
        staging.refresh({},'水彩',self.shot,self.f.refs,basis='action',
            action='只收紧人物取景。',image_prompt='旧开场',ask=ask)
        self.assertEqual(ask.call_args.args[1]['approved_scene_design'],fixed)
        self.assertEqual(self.shot['approved_scene_design'],fixed)

    def test_chinese_scene_sign_stays_in_image_design_with_english_h3_scene(self):
        answer=copy.deepcopy(self.result)
        answer['scene']='A medium view beside the doorway; keep the existing lettering on its sign unchanged.'
        answer['opening']='门边双人中景，门牌逐字保留“松果书屋”。'
        answer['image_prompt']='水彩绘本，门边双人中景，门牌逐字保留“松果书屋”。'
        before=copy.deepcopy(self.shot['native_dialogue']['turns'])
        result,_=staging.refresh({},'水彩',self.shot,self.f.refs,basis='action',
            action='保持动作，只收紧构图。',image_prompt='门边',ask=Mock(return_value=answer))
        self.assertIn('松果书屋',result['visual_description'])
        self.assertIn('松果书屋',result['image_prompt'])
        self.assertNotIn('松果书屋',result['native_dialogue']['scene'])
        self.assertEqual(result['native_dialogue']['turns'],before)

    def test_words_do_not_invalidate_visual_binding_but_staging_does(self):
        before=content_fingerprint(self.shot)
        self.shot['native_dialogue']['turns'][0]['text']='等等我。'
        self.assertEqual(content_fingerprint(self.shot),before)
        self.shot['native_dialogue']['scene']=self.result['scene']
        self.assertNotEqual(content_fingerprint(self.shot),before)
        self.shot['native_staging_stale']=True
        with self.assertRaisesRegex(ValueError,'动作已修改'):staging.assert_staging_current(self.shot)


class NativeMotionApiTest(unittest.TestCase):
    create = fixtures.NativeStoryTests.create
    def setUp(self):
        fixtures.NativeStoryTests.setUp(self)
        self.record=self.create(one_click=False).json()
        f=fixtures.NativeDirectorTests();f.setUp()
        from backend.app.video_native_story import compile_storyboard
        self.record['scenes'],self.record['shots']=compile_storyboard(f.raw,f.record,f.cast,f.refs)
        self.record['status']='image_review'
        self.path=studio.directory(1,self.record['id']);studio.save(self.path,self.record)
        self.url='/api/video-studio/'+self.record['id']+'/shots/shot_001/motion'

    def test_motion_save_marks_real_h3_input_stale_instead_of_silent_old_action(self):
        result=self.client.post(self.url,json={'revision':1,'kind':'video','action':'小兔走到门口。','video_prompt':'图1保持画风。'})
        self.assertEqual(result.status_code,200,result.text)
        self.assertTrue(result.json()['shots'][0]['native_staging_stale'])
        self.assertEqual(result.json()['shots'][1],self.record['shots'][1])
        self.assertEqual(result.json()['shots'][0]['native_dialogue'],self.record['shots'][0]['native_dialogue'])

    def test_native_static_toggle_rejected_without_changing_record(self):
        before=(self.path/'record.json').read_bytes()
        result=self.client.post(self.url,json={'revision':1,'kind':'static','action':'','video_prompt':''})
        self.assertEqual(result.status_code,400)
        self.assertEqual((self.path/'record.json').read_bytes(),before)

    def test_local_design_refresh_persists_one_opening_and_preserves_other_shots(self):
        from PIL import Image
        f=NativeStagingTest();f.setUp()
        shot=self.record['shots'][0]
        shot.update(visual_description='旧全景，人物全身可见。',image='core.png',image_status='completed')
        shot['scene_reference_id']='fixed-room'
        self.record['scene_assets']=[{'id':'fixed-room','name':'室内',
            'image_prompt':'门牌上逐字写“松果书屋”。'}]
        Image.new('RGB',(32,18),'green').save(self.path/'core.png')
        studio.save(self.path,self.record)
        before=studio.read(self.path);pixels=(self.path/'core.png').read_bytes()
        # Asset identity validation is outside this persistence regression;
        # the real staging translator, HTTP handler, save and reload run here.
        with patch.object(studio,'verify_project_cast'),patch.object(studio,'validate_cast_shot'), \
             patch('backend.app.video_agents.ask_json',return_value=f.result) as ask:
            response=self.client.post(self.url.replace('/motion','/refresh-prompts'),json={
                'revision':before['revision'],'basis':'action','action':f.result['action'],
                'image_prompt':shot['image_prompt']})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(ask.call_args.args[1]['approved_scene_design'],self.record['scene_assets'][0])
        after=studio.read(self.path);target=after['shots'][0]
        self.assertEqual(target['visual_description'],f.result['opening'])
        self.assertEqual(target['visual_description'],target['motion_plan']['reference_visual'])
        self.assertEqual(after['shots'][1:],before['shots'][1:])
        self.assertEqual(target['native_dialogue']['turns'],before['shots'][0]['native_dialogue']['turns'])
        self.assertEqual(after['scenes'],before['scenes'])
        self.assertEqual((self.path/'core.png').read_bytes(),pixels)
        self.assertTrue(target['image_prompt_out_of_sync'])
        self.assertNotIn('approved_scene_design',target)

    def test_native_core_refresh_reads_generated_pixels_and_keeps_assets_and_timing(self):
        from PIL import Image
        import hashlib
        f=NativeStagingTest();f.setUp()
        shot=self.record['shots'][0]
        shot['manual_h3_prompt']={'prompt':'old manually adopted prompt','fingerprint':'old'}
        shot.update(image='core.png',image_status='completed',image_origin='generated')
        Image.new('RGB',(32,18),'green').save(self.path/'core.png')
        studio.save(self.path,self.record)
        before=studio.read(self.path);pixels=(self.path/'core.png').read_bytes()
        analysis={'description':'实际核心图中两位角色在桌旁。',
                  'fingerprint':hashlib.sha256(pixels).hexdigest()}
        with patch.object(studio,'verify_project_cast'),patch.object(studio,'validate_cast_shot'), \
             patch('backend.app.video_prompt_refresh.analyze_image',return_value=analysis) as vision, \
             patch('backend.app.video_agents.ask_json',return_value=f.result) as ask:
            response=self.client.post(self.url.replace('/motion','/refresh-prompts'),json={
                'revision':before['revision'],'basis':'image','action':shot['action'],
                'image_prompt':shot['image_prompt']})
        self.assertEqual(response.status_code,200,response.text)
        vision.assert_called_once()
        self.assertEqual(ask.call_args.args[1]['core_basis']['kind'],'image_analysis')
        after=studio.read(self.path)
        self.assertNotIn('manual_h3_prompt',after['shots'][0])
        self.assertEqual(after['shots'][0]['manual_h3_history'][-1]['prompt'],'old manually adopted prompt')
        self.assertEqual(after['shots'][1:],before['shots'][1:])
        self.assertEqual(after['scenes'],before['scenes'])
        self.assertEqual(after['shots'][0]['native_dialogue']['turns'],before['shots'][0]['native_dialogue']['turns'])
        self.assertEqual(after['shots'][0]['native_dialogue']['duration'],before['shots'][0]['native_dialogue']['duration'])
        self.assertEqual((self.path/'core.png').read_bytes(),pixels)
        self.assertFalse(after['shots'][0]['image_prompt_out_of_sync'])


if __name__=='__main__':unittest.main()
