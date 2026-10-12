import copy
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch
from types import SimpleNamespace
import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app import video_native_story as native, video_studio as studio, one_click as auto
from backend.app import comfyui_bridge, auth, db, pipeline
from backend.app.video_cast_planning import _digest


def with_performance(raw):
    from backend.app.h3_dialogue import DialoguePlan, performance_intervals
    raw=copy.deepcopy(raw)
    for shot in raw['shots']:
        plan=DialoguePlan.model_validate(shot['native_dialogue'],context={'unbound_draft':True})
        shot['native_dialogue']['performance_beats']=[dict(start=a,end=b,
            action='The friends keep their places beside the umbrella.',action_zh='两位朋友留在伞边。')
            for a,b in performance_intervals(plan.duration,plan.turns)]
    return raw


class NativeStoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        stack = ExitStack(); self.addCleanup(stack.close)
        stack.enter_context(patch.object(studio, 'ROOT', Path(self.tmp.name)/'projects'))
        stack.enter_context(patch.object(auto, 'ROOT', Path(self.tmp.name)/'runs'))
        stack.enter_context(patch.object(studio, 'ACTIVE', set()))
        stack.enter_context(patch.object(studio, 'require_user', return_value={'id':1}))
        stack.enter_context(patch.object(auth, 'require_user', return_value={'id':1}))
        self.profile = stack.enter_context(patch.object(comfyui_bridge, 'video_profile', return_value={
            'video_input_mode':'composition_reference', 'workflow':{
                '1':{'class_type':'MiniMaxH3ReferenceToVideo','inputs':{}}},
            'mappings':{'prompt':{'node_id':'1','input_name':'prompt'}}}))
        self.data = dict(request_id='a'*32, name='三人的故事', story='小兔拿来了雨伞，小熊想要避雨，小猫挤在门口等他们商量。',
            speech_mode='dialogue', style='水彩绘本', comfyui_profile_id='native', minimum_shots=2)
        app = FastAPI(); app.include_router(studio.router); app.include_router(auto.router)
        self.client = TestClient(app)

    def create(self, **changes):
        return self.client.post('/api/video-studio/from-story', json={**self.data, **changes})

    def test_create_and_lost_response_retry_are_idempotent_without_tts(self):
        with patch.object(pipeline.store, 'get', side_effect=AssertionError('No TTS job')):
            first = self.create(); self.assertEqual(first.status_code, 200, first.text)
            record = first.json()
            self.assertNotIn('audio', record); self.assertNotIn('source_project', record)
            self.assertEqual(record['scenes'][0]['text'], self.data['story'])
            state = auto.load(1, record['id']); self.assertEqual(state['kind'], 'native_story')
            self.assertEqual(self.create().json(), record)
            self.assertEqual(auto.load(1,record['id']),state)
            self.assertEqual(self.profile.call_count,1)
            self.assertEqual(self.create(story=self.data['story']+'后来雨停了。').status_code,409)

    def test_one_click_media_review_does_not_make_editorial_advice_a_gate(self):
        from backend.app.video_review_policy import strict_quality_review, strict_story_review
        record = self.create().json()
        self.assertTrue(strict_quality_review(record))
        self.assertFalse(strict_story_review(record))
        manual = self.create(request_id='b'*32, one_click=False).json()
        self.assertFalse(strict_quality_review(manual))
        strict = copy.deepcopy(record)
        strict['creation_parameters']['quality_review_policy'] = 'strict'
        self.assertTrue(strict_quality_review(strict))
        self.assertTrue(strict_story_review(strict))

    def test_missing_run_after_partial_create_is_recovered_but_paused_run_stays_paused(self):
        record=self.create().json()
        auto._path(1,record['id']).unlink()
        self.assertEqual(self.create().status_code,200)
        state=auto.load(1,record['id']);auto._pause(state,'manual')
        self.create();self.assertEqual(auto.load(1,record['id'])['state'],'paused')

    def test_explicit_dialogue_route_persists_and_changes_retry_identity(self):
        response=self.create(dialogue_route='external_tts')
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['creation_parameters']['dialogue_route'],'external_tts')
        self.assertEqual(self.create(dialogue_route='external_tts').json(),response.json())
        self.assertEqual(self.create(dialogue_route='h3_native').status_code,409)

    def test_native_creation_honors_scene_choice_for_both_speech_routes(self):
        from backend.app import video_scene_references
        for index, (mode, enabled) in enumerate((('dialogue', True), ('dialogue', False), ('mixed', True), ('mixed', False))):
            response=self.create(request_id=f'{index:032x}',speech_mode=mode,scene_references_enabled=enabled)
            self.assertEqual(response.status_code,200,response.text)
            record=response.json()
            self.assertEqual(video_scene_references.enabled(record),enabled)
            saved=studio.read(studio.directory(1,record['id']))
            self.assertEqual(saved['creation_parameters']['scene_references_enabled'],enabled)

    def test_automatic_planning_and_pacing_persist_without_forced_counts(self):
        response=self.create(duration_seconds=None,minimum_shots=None,visual_pacing_preset='fast')
        self.assertEqual(response.status_code,200,response.text)
        record=response.json()
        self.assertIsNone(record['native_story']['duration_seconds'])
        self.assertIsNone(record['native_story']['minimum_shots'])
        self.assertEqual(record['native_story']['visual_pacing']['target_duration'],6)
        self.assertEqual(record['creation_parameters']['visual_pacing_preset'],'fast')
        self.assertEqual(self.create(duration_seconds=None,minimum_shots=None,visual_pacing_preset='fast').json(),record)
        self.assertEqual(self.create(duration_seconds=None,minimum_shots=None,visual_pacing_preset='auto').status_code,409)

    def test_old_client_retry_preserves_existing_scene_setting_and_project(self):
        record=self.create().json()
        record['creation_parameters']['scene_references_enabled']=False
        old_basis=native.NativeStoryCreate(**self.data).model_dump()
        old_basis.pop('scene_references_enabled')
        old_basis.pop('dialogue_route')
        old_basis.pop('dialogue_tts_engine')
        # Reproduce the actual pre-render-options schema, not today's defaults.
        from backend.app.video_pacing import PACING_FIELDS
        for key in ('comfyui_steps', 'comfyui_width', 'comfyui_height', *PACING_FIELDS):
            old_basis.pop(key, None)
        record['native_creation_sha256']=native._hash(old_basis)
        path=studio.directory(1,record['id']);studio.save(path,record)
        before=(path/'record.json').read_bytes()
        response=self.create()
        self.assertEqual(response.status_code,200,response.text)
        self.assertFalse(response.json()['creation_parameters']['scene_references_enabled'])
        self.assertEqual((path/'record.json').read_bytes(),before)
        self.assertEqual(self.create(scene_references_enabled=True).status_code,409)

    def test_native_scene_asset_is_actually_forwarded_to_core_image_input(self):
        from PIL import Image
        record=self.create().json();path=studio.directory(1,record['id'])
        target=path/'assets'/'scene.jpg';target.parent.mkdir(parents=True,exist_ok=True)
        Image.new('RGB',(12,12),'white').save(target)
        record['scene_assets']=[dict(id='room',name='门前',image='assets/scene.jpg',image_status='completed')]
        shot=dict(id='shot_001',reference_ids=[],scene_reference_id='room')
        prompt,paths,asset=studio._image_inputs(path,record,shot,'本镜门打开，小兔站在门外。')
        self.assertEqual(paths,[str(target.resolve())]);self.assertEqual(asset['id'],'room')
        self.assertIn('图1仅用于保持同一空间',prompt)
        self.assertIn('不提前呈现后续结果',prompt)

    def test_approved_camera_survives_saved_suffixes_and_retries(self):
        from backend.app.video_prop_states import VIDEO_MARKER
        record=self.create().json();path=studio.directory(1,record['id'])
        shot=dict(id='shot_001',reference_ids=[],native_dialogue={
            'scene':'A fixed medium view beside the bell.' + VIDEO_MARKER + 'Ending physical state: gone'})
        original='Opening beside the bell.\n【已选项目角色】\nOld identity suffix'
        prompt,_,_=studio._image_inputs(path,record,shot,original,use_scene=False)
        self.assertIn('A fixed medium view beside the bell.',prompt)
        self.assertNotIn('Ending physical state',prompt)
        self.assertNotIn('Old identity suffix',prompt)
        again,_,_=studio._image_inputs(path,record,shot,prompt,use_scene=False)
        self.assertEqual(again,prompt)
        explicit,_,_=studio._image_inputs(path,record,shot,'Explicit repair',references=[],use_scene=False)
        self.assertEqual(explicit,'Explicit repair')

    def test_mixed_has_separate_stable_narrator_and_no_fake_visual(self):
        response=self.create(speech_mode='mixed'); self.assertEqual(response.status_code,200,response.text)
        record=response.json(); narrator=record['native_narrator']
        self.assertTrue(narrator['id'].startswith('pc_'))
        self.assertEqual(self.create(speech_mode='mixed').json()['native_narrator'],narrator)
        self.assertEqual(record['references'],[])

    def test_wrong_profile_and_extra_secret_fields_cannot_create_project(self):
        self.assertEqual(self.create(api_key='not-a-real-key').status_code,422)
        self.profile.return_value['video_input_mode']='first_last'
        self.assertEqual(self.create().status_code,400)
        self.assertFalse((studio.directory(1,'a'*32)/'record.json').exists())

    def test_native_run_uses_project_and_never_imports_tts(self):
        record=self.create().json()
        def accepted(action,user,project):
            project['status']='planning'
            path=studio.directory(1,record['id']);studio.ACTIVE.add(str(path));studio.save(path,project)
        with patch.object(db,'get_user_by_id',return_value={'id':1}), \
             patch.object(pipeline.store,'get',side_effect=AssertionError('No TTS job')), \
             patch.object(studio,'import_project_for_user') as importer, \
             patch.object(auto,'dispatch',side_effect=accepted) as dispatch:
            auto.tick(1,record['id']);auto.tick(1,record['id'])
        importer.assert_not_called(); self.assertEqual(dispatch.call_count,1)
        self.assertEqual(dispatch.call_args.args[0],'plan')

    def test_normal_planning_worker_creates_cast_and_native_shots_without_narration_director(self):
        from backend.app import video_auto_cast
        from PIL import Image
        record=self.create(one_click=False).json()
        original=video_auto_cast.ensure_auto_cast
        names=['小兔','小熊','小猫']
        designs={'characters':[dict(name=name,basis=name,appearance='简单固定外貌',image_prompt='一名角色的全身图') for name in names]}
        def renderer(macro,configs):
            Image.new('RGB',(12,12),(200,170,100)).save(macro['_output_path'])
            return macro['_output_path']
        def auto_cast(*args,**kwargs):
            kwargs.update(render=renderer,image_configs=lambda:[],ask=lambda *_:designs)
            return original(*args,**kwargs)
        calls=[]
        def director(system,basis,*,stream=False,max_output_tokens=10000):
            self.assertTrue(stream)
            self.assertEqual(max_output_tokens,
                10000 if 'performed_shots' in basis or 'shots' in basis else 24000)
            calls.append(system)
            if 'performed_shots' in basis:
                return good_story_review(basis['performed_shots'])
            if 'shots' in basis:
                return {'reviews':[dict(id=s['id'],problems=[]) for s in basis['shots']]}
            self.assertEqual(basis['dialogue_route'], 'h3_native')
            self.assertIn('source_speech_budget', basis)
            fixture=NativeDirectorTests();fixture.setUp()
            result=copy.deepcopy(fixture.raw)
            actors=basis['cast']['characters']
            for i,shot in enumerate(result['shots']):
                shot['source_text']=record['native_story']['text'][:8] if i==0 else record['native_story']['text'][8:]
                for j,speaker in enumerate(shot['native_dialogue']['speakers']):
                    speaker['character_id']=actors[j]['project_character_id'];speaker['name']=actors[j]['name']
                shot['native_dialogue']['turns'][0]['character_id']=actors[i]['project_character_id']
            return with_performance(result)
        class InlineThread:
            def __init__(self,*,target,daemon):self.target=target
            def start(self):self.target()
        with patch.object(video_auto_cast,'ensure_auto_cast',side_effect=auto_cast), \
             patch('backend.app.video_agents.ask_json',side_effect=director), \
             patch.object(studio,'threading',SimpleNamespace(Thread=InlineThread,Event=threading.Event)), \
             patch.object(studio,'plan_storyboard',side_effect=AssertionError('Narration director called')):
            studio.plan_for_user(record['id'],{'id':1})
        saved=studio.read(studio.directory(1,record['id']))
        self.assertEqual(saved['status'],'storyboard_review',saved.get('error'))
        self.assertEqual(len(saved['project_cast']['characters']),3)
        self.assertEqual(len(saved['shots']),2)
        self.assertNotIn('audio',saved)
        self.assertEqual(''.join(s['text'] for s in saved['scenes']),record['native_story']['text'])
        self.assertTrue(all(s.get('native_dialogue') for s in saved['shots']))
        self.assertEqual(len(calls),3)
        self.assertTrue(studio.production_prompt_context(saved).get('native_speech_mode'))

    def test_native_controls_auth_pause_and_edit_lock(self):
        record=self.create().json(); endpoint='/api/one-click/'+record['id']
        with self.assertRaises(Exception): auto.assert_manual_edit(record)
        response=self.client.post(endpoint,json={'action':'pause'})
        self.assertEqual(response.status_code,200,response.text)
        auto.assert_manual_edit(record)
        self.assertEqual(self.client.get(endpoint).json()['state'],'paused')
        with patch.object(auth,'require_user',return_value={'id':2}):
            self.assertEqual(self.client.get(endpoint).status_code,404)
            self.assertEqual(self.client.post(endpoint,json={'action':'resume'}).status_code,404)
        self.assertEqual(self.client.post(endpoint,json={'action':'resume'}).status_code,200)

    def test_voice_pause_does_not_dispatch_or_fake_missing_voices(self):
        record=self.create().json(); record.update(status='storyboard_review',shots=[{'id':'shot_1'}])
        studio.save(studio.directory(1,record['id']),record)
        with patch.object(db,'get_user_by_id',return_value={'id':1}),patch.object(auto,'dispatch') as dispatch:
            auto.tick(1,record['id']);auto.tick(1,record['id'])
        dispatch.assert_not_called();self.assertEqual(auto.load(1,record['id'])['state'],'paused')
        self.assertEqual(self.client.post('/api/one-click/'+record['id'],json={'action':'resume'}).status_code,409)

    def test_explicit_resume_retries_unfinished_planning_once_and_keeps_saved_assets(self):
        record=self.create().json();path=studio.directory(1,record['id'])
        record.update(status='draft',error='ReadTimeout',auto_cast={'status':'designed','characters':[{'name':'已保存角色'}]})
        studio.save(path,record);state=auto.load(1,record['id']);auto._pause(state,'ReadTimeout')
        starts=[]
        class WaitingThread:
            def __init__(self,*,target,daemon):self.target=target
            def start(self):starts.append(self.target)
        with patch.object(studio,'threading',SimpleNamespace(Thread=WaitingThread,Event=threading.Event)):
            response=self.client.post('/api/one-click/'+record['id'],json={'action':'resume'})
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.json()['state'],'running')
            after=studio.read(path)
            self.assertEqual(after['status'],'planning');self.assertEqual(after['error'],'')
            self.assertEqual(after['auto_cast'],record['auto_cast'])
            self.assertEqual(self.client.post('/api/one-click/'+record['id'],json={'action':'resume'}).status_code,200)
            self.assertEqual(len(starts),1)
        # Another worker failure must pause; ticks cannot retry by themselves.
        studio.ACTIVE.remove(str(path));after.update(status='draft',error='ReadTimeout again');studio.save(path,after)
        with patch.object(db,'get_user_by_id',return_value={'id':1}),patch.object(auto,'dispatch') as dispatch:
            auto.tick(1,record['id']);auto.tick(1,record['id'])
        dispatch.assert_not_called();self.assertEqual(auto.load(1,record['id'])['state'],'paused')

    def test_resume_while_image_worker_active_waits_without_changing_or_resubmitting_project(self):
        record=self.create().json();path=studio.directory(1,record['id'])
        record.update(status='image_generating',shots=[{'id':'shot_1','kind':'video','image_status':'running'}])
        studio.save(path,record);studio.ACTIVE.add(str(path))
        self.client.post('/api/one-click/'+record['id'],json={'action':'pause'})
        before=(path/'record.json').read_bytes()
        with patch.object(db,'get_user_by_id',return_value={'id':1}),patch.object(auto,'dispatch') as dispatch:
            response=self.client.post('/api/one-click/'+record['id'],json={'action':'resume'})
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.json()['state'],'running')
            self.assertIn('等待当前阶段完成',response.json()['message'])
            self.assertEqual((path/'record.json').read_bytes(),before)
            auto.tick(1,record['id']);auto.tick(1,record['id'])
            dispatch.assert_not_called()
            studio.ACTIVE.remove(str(path));record['status']='image_review'
            record['shots'][0]['image_status']='completed';studio.save(path,record)
            with patch.object(auto,'require_native_voices'):
                auto.tick(1,record['id'])
            self.assertEqual(dispatch.call_count,1)
            self.assertEqual(dispatch.call_args.args[0],'confirm_images')

    def test_resume_does_not_cancel_pending_stop_or_ignore_busy_stage_errors(self):
        record=self.create().json();path=studio.directory(1,record['id']);studio.ACTIVE.add(str(path))
        for changes in ({'status':'image_stopping','error':''},
                        {'status':'image_generating','error':'需要检查原请求'},
                        {'status':'image_generating','error':'','reedit_shot_id':'shot_1'}):
            record.update(changes);studio.save(path,record)
            self.client.post('/api/one-click/'+record['id'],json={'action':'pause'})
            before=(path/'record.json').read_bytes()
            result=self.client.post('/api/one-click/'+record['id'],json={'action':'resume'})
            self.assertEqual(result.status_code,409,result.text)
            self.assertEqual(auto.load(1,record['id'])['state'],'paused')
            self.assertEqual((path/'record.json').read_bytes(),before)

    def test_paused_status_reports_latest_failure_without_mutating_run(self):
        record=self.create().json();state=auto.load(1,record['id']);auto._pause(state,'obsolete schema error')
        record['error']='当前动作需要修订';studio.save(studio.directory(1,record['id']),record)
        before=auto._path(1,record['id']).read_bytes()
        result=self.client.get('/api/one-click/'+record['id']).json()
        self.assertEqual(result['message'],record['error'])
        self.assertEqual(before,auto._path(1,record['id']).read_bytes())

    def test_local_dialogue_edit_preserves_other_shots_and_core_images(self):
        record=self.create(one_click=False).json()
        fixture=NativeDirectorTests();fixture.setUp()
        fixture.raw=with_performance(fixture.raw)
        record['scenes'],record['shots']=native.compile_storyboard(fixture.raw,fixture.record,fixture.cast,fixture.refs)
        record.update(status='completed',export={'raw':'old.mp4'})
        for shot in record['shots']:
            shot.update(image='unchanged.jpg',image_status='completed',video_status='completed',
                video_task_id=shot['id']+'-accepted',video=shot['id']+'.mp4',video_request={'old':True})
        before=copy.deepcopy(record)
        path=studio.directory(1,record['id']);studio.save(path,record)
        for shot in record['shots']:
            (path/shot['video']).write_bytes(b'existing-clip-fixture')
        turns=copy.deepcopy(record['shots'][0]['native_dialogue']['turns']);turns[0]['text']='一起回家吧。'
        route='/api/video-studio/'+record['id']+'/shots/shot_001/dialogue'
        result=self.client.put(route,json={'revision':record['revision'],'turns':turns})
        self.assertEqual(result.status_code,200,result.text);after=result.json()
        self.assertEqual(after['shots'][1],before['shots'][1])
        self.assertEqual(after['shots'][0]['image'],before['shots'][0]['image'])
        self.assertEqual(after['shots'][0]['video_history'][0]['video_task_id'],'shot_001-accepted')
        self.assertTrue(before['shots'][0]['native_dialogue']['performance_beats'])
        self.assertNotIn('performance_beats',after['shots'][0]['native_dialogue'])
        self.assertNotIn('video_request',after['shots'][0]);self.assertNotIn('export',after)
        self.assertEqual(after['status'],'video_review')
        self.assertEqual(self.client.put(route,json={'revision':1,'turns':turns}).status_code,409)
        self.assertEqual(self.client.put(route,json={'revision':after['revision'],'turns':turns}).json(),after)

    def test_local_edit_cannot_discard_unconfirmed_paid_submission(self):
        record=self.create(one_click=False).json();fixture=NativeDirectorTests();fixture.setUp()
        record['scenes'],record['shots']=native.compile_storyboard(fixture.raw,fixture.record,fixture.cast,fixture.refs)
        record['status']='video_review';shot=record['shots'][0]
        shot.update(video_status='failed',video_task_id='still-owned',video_terminal=False,video_request={'frozen':'input'})
        path=studio.directory(1,record['id']);studio.save(path,record);before=(path/'record.json').read_bytes()
        turns=copy.deepcopy(shot['native_dialogue']['turns']);turns[0]['text']='一起回家吧。'
        result=self.client.put('/api/video-studio/'+record['id']+'/shots/shot_001/dialogue',json={'revision':1,'turns':turns})
        self.assertEqual(result.status_code,409,result.text)
        self.assertEqual((path/'record.json').read_bytes(),before)


class NativeDirectorTests(unittest.TestCase):
    def setUp(self):
        self.ids=['pc_'+digit*32 for digit in ('1','2','3')]
        self.cast={'version':1,'source':'user_confirmed','project_id':'a'*32,'characters':[
            dict(project_character_id=identity,name=name,reference_id=f'ref_{i}',constraints='',
                 reference_sha256='b'*64,identity_sha256='c'*64)
            for i,(identity,name) in enumerate(zip(self.ids,['小兔','小熊','小猫']),1)]}
        from backend.app.video_project_cast import SOURCE
        self.cast['source']=SOURCE
        self.cast['cast_sha256']=_digest(self.cast)
        self.refs=[dict(id=f'ref_{i}',label=f'图{i}',kind='character',description=name,file=f'{i}.jpg')
                   for i,name in enumerate(['小兔','小熊','小猫'],1)]
        self.record=dict(id='a'*32,native_story={'text':'小兔拿伞。小熊避雨。','minimum_shots':2,'duration_seconds':16},
            settings={'style':'水彩','characters':'','world':''},creation_parameters={'speech_mode':'dialogue'})
        self.raw={'shots':[dict(source_text=source,intent='解决避雨问题',action='举起伞并看向朋友',
            opening='两位朋友在门前',image_prompt='水彩绘本，门前两位朋友',native_dialogue=dict(
                duration=8,scene='The friends stand near the door. One raises an umbrella.',
                speakers=[dict(character_id=identity,name=name,picture=j+2,position='beside the door')
                          for j,(identity,name) in enumerate(zip(self.ids[:2],['小兔','小熊']))],
                turns=[dict(character_id=self.ids[i],text=text,start=0.5,end=3.5)]))
            for i,(source,text) in enumerate([('小兔拿伞。','一起走吧。'),('小熊避雨。','我来撑伞。')])]}

    def test_compiles_complete_story_into_distinct_shots_and_real_cast_references(self):
        scenes,shots=native.compile_storyboard(self.raw,self.record,self.cast,self.refs)
        self.assertEqual([s['start'] for s in scenes],[0,8])
        self.assertEqual(''.join(s['text'] for s in scenes),self.record['native_story']['text'])
        self.assertEqual(shots[0]['reference_ids'],['ref_1','ref_2'])
        self.assertNotIn('ref_3',shots[0]['reference_ids'])
        self.assertIsNone(shots[0]['native_dialogue']['speakers'][0]['voice_id'])
        from backend.app.video_agents import audit_storyboard
        self.assertEqual(len(audit_storyboard(shots,{r['id'] for r in self.refs})),2)

    def test_single_character_two_lines_preserve_silent_listeners_and_distinct_shots(self):
        raw=copy.deepcopy(self.raw)
        expected=['等一下……','我想和你们一起走。']
        for row,text in zip(raw['shots'],expected):
            row['native_dialogue']['turns'][0].update(character_id=self.ids[0],text=text)
            row['native_dialogue']['speakers'].append(dict(
                character_id=self.ids[2],name='小猫',picture=4,position='beside the door'))
        before=copy.deepcopy(raw)
        _,shots=native.compile_storyboard(raw,self.record,self.cast,self.refs)
        self.assertEqual(len(shots),2)
        turns=[turn for shot in shots for turn in shot['native_dialogue']['turns']]
        self.assertEqual([turn['text'] for turn in turns],expected)
        self.assertEqual({turn['character_id'] for turn in turns},{self.ids[0]})
        for shot in shots:
            self.assertEqual(shot['reference_ids'],['ref_1','ref_2','ref_3'])
            self.assertEqual([speaker['character_id'] for speaker in shot['native_dialogue']['speakers']],self.ids)
            self.assertEqual([row['project_character_id'] for row in shot['cast_plan']['characters']],self.ids)
        self.assertEqual(raw,before)

    def test_dialogue_rejects_zero_speaking_characters_even_with_visible_listeners(self):
        raw=copy.deepcopy(self.raw)
        for row in raw['shots']:row['native_dialogue']['turns']=[]
        with self.assertRaisesRegex(ValueError,'至少需要一名实际发言的角色'):
            native.compile_storyboard(raw,self.record,self.cast,self.refs)

    def test_mixed_allows_one_character_and_narrator_without_inventing_dialogue(self):
        record=copy.deepcopy(self.record)
        record['creation_parameters']['speech_mode']='mixed'
        record['native_narrator']={'id':self.ids[2]}
        raw=copy.deepcopy(self.raw)
        raw['shots'][1]['native_dialogue']['speakers'].append(dict(
            character_id=self.ids[2],name='旁白',kind='narrator',picture=None,
            on_screen=False,position=''))
        raw['shots'][1]['native_dialogue']['turns'][0]['character_id']=self.ids[2]
        _,shots=native.compile_storyboard(raw,record,self.cast,self.refs)
        self.assertEqual(shots[1]['native_dialogue']['turns'][0]['character_id'],self.ids[2])
        raw['shots'][0]['native_dialogue']['turns']=[]
        with self.assertRaisesRegex(ValueError,'混合故事'):
            native.compile_storyboard(raw,record,self.cast,self.refs)

    def test_photo_identity_references_do_not_invent_live_speakers(self):
        raw=copy.deepcopy(self.raw)
        photo=copy.deepcopy(raw['shots'][0])
        photo.update(source_text='看照片。', opening='桌上照片的特写，没有真人入画。',
            image_prompt='照片中是图1小兔和图3小猫。',
            depicted_characters=[dict(project_character_id=self.ids[i],basis='桌上照片中的人物') for i in (0,2)])
        photo['native_dialogue'].update(speakers=[],turns=[])
        raw['shots'].append(photo)
        record=copy.deepcopy(self.record);record['native_story']['text']+='看照片。'
        _,shots=native.compile_storyboard(raw,record,self.cast,self.refs)
        result=shots[-1]
        self.assertEqual(result['reference_ids'],['ref_1','ref_3'])
        self.assertEqual(result['native_dialogue']['speakers'],[])
        self.assertEqual(result['native_dialogue']['turns'],[])
        self.assertEqual(result['motion_plan']['participants'],[])
        self.assertEqual([c['presence'] for c in result['cast_plan']['characters']],['depicted','depicted'])
        from backend.app.video_agents import _project_cast
        _project_cast({'project_cast':self.cast},shots,self.refs)
        photo.pop('depicted_characters')
        with self.assertRaisesRegex(ValueError,'未绑定的角色图号'):
            native.compile_storyboard(raw,record,self.cast,self.refs)

    def test_reference_tag_cannot_reintroduce_a_later_character(self):
        raw=copy.deepcopy(self.raw)
        raw['shots'][0]['native_dialogue']['speakers'][1]['presence']='later'
        raw['shots'][0]['image_prompt']='图1小兔独自等待，图2小熊还未出现。'
        with self.assertRaisesRegex(ValueError,'未绑定的角色图号'):
            native.compile_storyboard(raw,self.record,self.cast,self.refs)

    def test_live_character_and_their_photo_share_identity_reference(self):
        raw=copy.deepcopy(self.raw)
        raw['shots'][0]['depicted_characters']=[dict(project_character_id=self.ids[0],basis='桌上的照片')]
        _,shots=native.compile_storyboard(raw,self.record,self.cast,self.refs)
        self.assertEqual(shots[0]['reference_ids'],['ref_1','ref_2'])
        self.assertEqual(len(shots[0]['cast_plan']['characters']),2)
        self.assertEqual(len(shots[0]['native_dialogue']['speakers']),2)
        raw['shots'][0]['depicted_characters']*=2
        with self.assertRaisesRegex(ValueError,'无效或重复'):
            native.compile_storyboard(raw,self.record,self.cast,self.refs)

    def test_photo_binding_never_accepts_unknown_identity_or_changes_later_entrance(self):
        for change in ('unknown','blank_basis','wrong_type','later'):
            with self.subTest(change=change):
                raw=copy.deepcopy(self.raw)
                entry=dict(project_character_id=self.ids[0],basis='A framed photograph')
                if change=='unknown':entry['project_character_id']='pc_'+'f'*32
                elif change=='blank_basis':entry['basis']=' '
                elif change=='wrong_type':entry['project_character_id']=[self.ids[0]]
                else:raw['shots'][0]['native_dialogue']['speakers'][0]['presence']='later'
                raw['shots'][0]['depicted_characters']=[entry]
                with self.assertRaises(ValueError):
                    native.compile_storyboard(raw,self.record,self.cast,self.refs)

    def test_missing_shots_cannot_crash_repair_or_erase_previous_candidate(self):
        state=dict(candidate={'message':'无法满足条件'},error='分镜格式无效',
                   repair={'previous':copy.deepcopy(self.raw)})
        request=native._repair_request(state)
        self.assertEqual(request['previous'],self.raw)
        self.assertEqual(request['shot_ids'],[])
        with self.assertRaisesRegex(ValueError,'需要非空 shots'):
            native._generate_candidate(Mock(return_value={'message':'无法满足条件'}),'system',{},request)

    def test_invalid_response_recovers_saved_story_only_for_matching_inputs(self):
        from backend.app.video_dialogue_story_review import REVISION
        params=dict(speech_mode='dialogue',project_cast=self.cast)
        basis=dict(story=self.record['native_story'],speech_mode='dialogue',cast=self.cast,
                   narrator=None,settings=self.record['settings'],references=self.refs)
        fingerprint=native._hash({**basis,'story_review_revision':REVISION,
            'native_director_revision':native.NATIVE_DIRECTOR_REVISION})
        previous=with_performance(self.raw)
        state=dict(fingerprint=fingerprint,attempt=0,candidate={'message':'invalid'},
                   error='分镜格式无效',repair={'previous':previous})
        self.record['native_planning_state']=copy.deepcopy(state)
        with patch.object(studio,'planning_parameters',return_value=params),patch.object(studio,'planning_references',return_value=self.refs):
            native.retry_failed_plan(self.record)
        self.assertEqual(self.record['native_planning_history'][-1],state)
        self.assertEqual(self.record['native_planning_state']['candidate'],previous)
        self.assertNotIn('error',self.record['native_planning_state'])
        self.assertNotIn('story_review',self.record['native_planning_state'])

    def test_missing_source_wrong_identity_and_mode_are_rejected(self):
        broken=copy.deepcopy(self.raw);broken['shots'][1]['source_text']='大家回家了。'
        with self.assertRaisesRegex(ValueError,'完整覆盖'):native.compile_storyboard(broken,self.record,self.cast,self.refs)
        broken=copy.deepcopy(self.raw)
        broken['shots'][0]['native_dialogue']['speakers'][1]['character_id']='pc_'+'f'*32
        with self.assertRaises(ValueError):native.compile_storyboard(broken,self.record,self.cast,self.refs)
        mixed=copy.deepcopy(self.record);mixed['native_narrator']={'id':self.ids[2]}
        with self.assertRaisesRegex(ValueError,'混合'):native.compile_storyboard(self.raw,mixed,self.cast,self.refs)

    def test_later_character_keeps_video_identity_but_is_absent_from_core_image_references(self):
        raw=copy.deepcopy(self.raw)
        raw['shots'][0]['native_dialogue']['speakers'][1]['presence']='later'
        raw['shots'][0]['action']='小兔等在门边，小熊打开门后走出来。'
        raw['shots'][0]['opening']='小兔独自在关着的门旁。'
        raw['shots'][0]['image_prompt']='小兔独自在关着的门旁。'
        _,shots=native.compile_storyboard(raw,self.record,self.cast,self.refs)
        first=shots[0]
        self.assertEqual(first['reference_ids'],['ref_1'])
        self.assertEqual(first['motion_plan']['reference_participants'],['小兔'])
        self.assertEqual(first['motion_plan']['participants'],['小兔','小熊'])
        late=first['cast_plan']['characters'][1]
        self.assertEqual(late['presence'],'later')
        self.assertEqual(late['project_character_id'],self.ids[1])
        self.assertEqual(late['basis'],raw['shots'][0]['action'])
        self.assertEqual(first['native_dialogue']['speakers'][1]['picture'],3)
        self.assertEqual(shots[1]['reference_ids'],['ref_1','ref_2'])


    def test_silent_action_beat_does_not_lose_other_speakers_or_source(self):
        raw=copy.deepcopy(self.raw)
        action=copy.deepcopy(raw['shots'][0]);action['source_text']='递过伞。'
        action['native_dialogue']['turns']=[]
        raw['shots'].insert(1,action)
        record=copy.deepcopy(self.record);record['native_story']['text']='小兔拿伞。递过伞。小熊避雨。'
        scenes,shots=native.compile_storyboard(raw,record,self.cast,self.refs)
        self.assertEqual(''.join(s['text'] for s in scenes),record['native_story']['text'])
        self.assertEqual(shots[1]['native_dialogue']['turns'],[])
        self.assertEqual(shots[1]['reference_ids'],['ref_1','ref_2'])
        self.assertTrue(shots[0]['native_dialogue']['turns'])
        self.assertTrue(shots[2]['native_dialogue']['turns'])

    def test_planner_resume_uses_saved_candidate_and_review(self):
        ask=Mock(side_effect=[with_performance(self.raw),good_story_review(),{'reviews':[{'id':f'shot_{i:03d}','problems':[]} for i in (1,2)]}])
        params={'speech_mode':'dialogue','project_cast':self.cast}
        first=native.plan_native_story(self.record,self.refs,params,progress=lambda _:None,persist=lambda:None,ask=ask)
        second=native.plan_native_story(self.record,self.refs,params,progress=lambda _:None,persist=lambda:None,ask=ask)
        self.assertEqual(first,second);self.assertEqual(ask.call_count,3)

    def test_director_explicit_envelopes_preserve_full_catalog_without_extra_call(self):
        import json
        raw=with_performance(self.raw)
        for response in ({'result':raw},{'data':{'output':raw}},
                         {**raw,'shots':json.dumps(raw['shots'])}):
            ask=Mock(return_value=response)
            self.assertEqual(native._generate_candidate(ask,'system',{},None),raw)
            ask.assert_called_once()
        for response in ({'result':raw,'output':raw},{'result':raw,'props':[]},
                         {'shots':[]},{'replacements':[]}):
            with self.assertRaisesRegex(ValueError,'需要非空 shots'):
                native._generate_candidate(Mock(return_value=response),'system',{},None)

    def seed_completed_plan(self):
        params={'speech_mode':'dialogue','project_cast':self.cast}
        ask=Mock(side_effect=[with_performance(self.raw),good_story_review(),
            {'reviews':[{'id':f'shot_{i:03d}','problems':[]} for i in (1,2)]}])
        native.plan_native_story(self.record,self.refs,params,progress=lambda _:None,persist=lambda:None,ask=ask)
        return params

    def test_director_upgrade_reuses_candidate_and_only_rechecks_reviews(self):
        params=self.seed_completed_plan()
        candidate=copy.deepcopy(self.record['native_planning_state']['candidate'])
        ask=Mock(side_effect=[good_story_review(),{'reviews':[{'id':f'shot_{i:03d}','problems':[]} for i in (1,2)]}])
        with patch.object(native,'NATIVE_DIRECTOR_REVISION','offline-new-director'):
            native.plan_native_story(self.record,self.refs,params,progress=lambda _:None,persist=lambda:None,ask=ask)
            native.plan_native_story(self.record,self.refs,params,progress=lambda _:None,persist=lambda:None,ask=ask)
        self.assertEqual(ask.call_count,2)
        self.assertEqual(self.record['native_planning_state']['candidate'],candidate)
        self.assertNotIn('version_revalidation',self.record['native_planning_state'])

    def test_upgrade_resumes_saved_local_repair_without_replanning_other_shots(self):
        params=self.seed_completed_plan()
        state=self.record['native_planning_state']
        previous=state.pop('candidate')
        state['repair']=dict(previous=copy.deepcopy(previous),shot_ids=['shot_002'],problems='repair')
        state['attempt']=1
        replacement=copy.deepcopy(previous['shots'][1])
        ask=Mock(side_effect=[dict(replacements=[dict(id='shot_002',shots=[replacement])]),
            good_story_review(),{'reviews':[dict(id=f'shot_{i:03d}',problems=[]) for i in (1,2)]}])
        with patch.object(native,'NATIVE_DIRECTOR_REVISION','offline-repair-upgrade'):
            native.plan_native_story(self.record,self.refs,params,progress=lambda _:None,persist=lambda:None,ask=ask)
        self.assertEqual(ask.call_count,3)
        self.assertEqual(ask.call_args_list[0].args[1]['repair']['shot_ids'],['shot_002'])
        self.assertEqual(state['candidate']['shots'][0],previous['shots'][0])
        self.assertEqual(state['repair']['previous'],previous)

    def test_failed_local_reply_is_saved_and_explicit_retry_gets_exact_error(self):
        params=self.seed_completed_plan()
        state=self.record['native_planning_state'];previous=state.pop('candidate')
        state.pop('review',None);state.pop('story_review',None)
        state['repair']=dict(previous=copy.deepcopy(previous),shot_ids=['shot_002'],problems='repair')
        state['attempt']=1
        broken=dict(replacements=[])
        first=Mock(return_value=broken)
        with self.assertRaisesRegex(ValueError,'完整对应'):
            native.plan_native_story(self.record,self.refs,params,progress=lambda _:None,persist=lambda:None,ask=first)
        first.assert_called_once()
        self.assertEqual(state['pending_director_response']['response'],broken)
        self.assertEqual(state['repair']['previous'],previous)
        fixed=dict(replacements=[dict(id='shot_002',shots=[copy.deepcopy(previous['shots'][1])])])
        retry=Mock(side_effect=[fixed,good_story_review(),{'reviews':[dict(id=f'shot_{i:03d}',problems=[]) for i in (1,2)]}])
        native.plan_native_story(self.record,self.refs,params,progress=lambda _:None,persist=lambda:None,ask=retry)
        self.assertEqual(retry.call_args_list[0].args[1]['previous_invalid_response'],broken)
        self.assertIn('完整对应',retry.call_args_list[0].args[1]['validation_error'])
        self.assertEqual(state['candidate']['shots'][0],previous['shots'][0])
        self.assertNotIn('pending_director_response',state)

    def test_saved_director_response_revalidates_without_a_new_director_call(self):
        params=self.seed_completed_plan()
        state=self.record['native_planning_state'];previous=state.pop('candidate')
        state.pop('review',None);state.pop('story_review',None)
        repair=dict(previous=copy.deepcopy(previous),shot_ids=['shot_002'],problems='repair')
        state['repair']=repair
        basis=dict(story=self.record['native_story'],speech_mode=params['speech_mode'],cast=self.cast,
            narrator=self.record.get('native_narrator'),settings=self.record['settings'],references=self.refs)
        state['pending_director_response']=dict(request_key=native._hash(dict(basis=basis,repair=repair)),
            response=dict(replacements=[dict(id='shot_002',shots=[copy.deepcopy(previous['shots'][1])])]))
        ask=Mock(side_effect=[good_story_review(),{'reviews':[dict(id=f'shot_{i:03d}',problems=[]) for i in (1,2)]}])
        native.plan_native_story(self.record,self.refs,params,progress=lambda _:None,persist=lambda:None,ask=ask)
        self.assertEqual(ask.call_count,2)
        self.assertEqual(state['candidate'],previous)

    def test_upgrade_with_incompatible_candidate_never_calls_model_or_drops_candidate(self):
        params=self.seed_completed_plan()
        state=self.record['native_planning_state']
        state['candidate']['shots'][0]['source_text']='not the adopted story'
        candidate=copy.deepcopy(state['candidate']);ask=Mock()
        with patch.object(native,'NATIVE_DIRECTOR_REVISION','offline-incompatible'):
            for _ in range(2):
                with self.assertRaises(ValueError):
                    native.plan_native_story(self.record,self.refs,params,progress=lambda _:None,persist=lambda:None,ask=ask)
        ask.assert_not_called()
        self.assertEqual(state['candidate'],candidate)
        self.assertTrue(state['version_revalidation'])
        self.assertEqual(state['attempt'],0)

    def test_explicit_retry_before_upgrade_does_not_erase_candidate_before_compatibility_gate(self):
        params=self.seed_completed_plan()
        self.record['native_planning_state']['error']='Previous validator rejected this plan'
        before=copy.deepcopy(self.record)
        with patch.object(native,'NATIVE_DIRECTOR_REVISION','offline-next-version'), \
             patch.object(studio,'planning_parameters',return_value=params), \
             patch.object(studio,'planning_references',return_value=self.refs):
            native.retry_failed_plan(self.record)
        self.assertEqual(self.record,before)

    def test_upgrade_review_failure_never_spends_automatic_creative_retry(self):
        params=self.seed_completed_plan();candidate=copy.deepcopy(self.record['native_planning_state']['candidate'])
        bad=good_story_review();bad['problems']=[dict(code='missing_event',shot_ids=['shot_001'],
            evidence='The event is unclear.',repair='Clarify the existing event.')]
        ask=Mock(side_effect=[bad,{'reviews':[dict(id=f'shot_{i:03d}',problems=[]) for i in (1,2)]}])
        with patch.object(native,'NATIVE_DIRECTOR_REVISION','offline-review-change'):
            with self.assertRaisesRegex(ValueError,'对白故事表达'):
                native.plan_native_story(self.record,self.refs,params,progress=lambda _:None,persist=lambda:None,ask=ask)
        self.assertEqual(ask.call_count,2)
        self.assertEqual(self.record['native_planning_state']['candidate'],candidate)

    def test_unknown_legacy_provenance_requires_explicit_replan_and_preserves_history(self):
        params={'speech_mode':'dialogue','project_cast':self.cast}
        state=dict(fingerprint='unknown-legacy',attempt=0,candidate=with_performance(self.raw))
        self.record['native_planning_state']=copy.deepcopy(state);ask=Mock()
        with self.assertRaisesRegex(ValueError,'内容来源'):
            native.plan_native_story(self.record,self.refs,params,progress=lambda _:None,persist=lambda:None,ask=ask)
        ask.assert_not_called()
        self.assertEqual(self.record['native_planning_state']['candidate'],state['candidate'])
        native.retry_failed_plan(self.record)
        self.assertNotIn('native_planning_state',self.record)
        self.assertEqual(self.record['native_planning_history'][-1]['candidate'],state['candidate'])

    def test_changed_content_archives_prior_candidate_before_new_planning(self):
        params=self.seed_completed_plan();previous=copy.deepcopy(self.record['native_planning_state'])
        self.record['settings']['world']='Changed adopted world'
        ask=Mock(side_effect=[with_performance(self.raw),good_story_review(),
            {'reviews':[dict(id=f'shot_{i:03d}',problems=[]) for i in (1,2)]}])
        native.plan_native_story(self.record,self.refs,params,progress=lambda _:None,persist=lambda:None,ask=ask)
        self.assertEqual(ask.call_count,3)
        self.assertEqual(self.record['native_planning_history'][-1],previous)
        self.assertNotEqual(self.record['native_planning_state']['basis_sha256'],previous['basis_sha256'])

    def test_compatible_validator_recovery_preserves_candidate_but_still_reviews(self):
        from backend.app.video_dialogue_story_review import REVISION
        params=dict(speech_mode='dialogue',project_cast=self.cast)
        basis=dict(story=self.record['native_story'],speech_mode='dialogue',cast=self.cast,
                   narrator=None,settings=self.record['settings'],references=self.refs)
        fingerprint=native._hash({**basis,'story_review_revision':REVISION,
            'native_director_revision':'native-performance-timeline-v9-held-prop-relations'})
        state=dict(fingerprint=fingerprint,attempt=1,candidate=with_performance(self.raw),
                   error='旧校验器拒绝无对白动作细分')
        self.record['native_planning_state']=copy.deepcopy(state)
        with patch.object(studio,'planning_parameters',return_value=params),patch.object(studio,'planning_references',return_value=self.refs):
            for mode in ('changed_input','semantic_failure','still_invalid'):
                changed=copy.deepcopy(self.record)
                if mode=='changed_input':changed['settings']['world']='Another world'
                elif mode=='semantic_failure':changed['native_planning_state']['story_review']={'problems':[]}
                else:changed['native_planning_state']['candidate']['shots'][0]['source_text']='改掉了原文'
                before=copy.deepcopy(changed)
                with self.subTest(mode=mode):
                    self.assertFalse(native._resume_structurally_valid_candidate(changed,changed['native_planning_state']))
                    self.assertEqual(changed,before)
            native.retry_failed_plan(self.record)
        self.assertEqual(self.record['native_planning_history'][-1],state)
        self.assertEqual(self.record['native_planning_state']['candidate'],state['candidate'])
        ask=Mock(side_effect=[good_story_review(),{'reviews':[dict(id=f'shot_{i:03d}',problems=[]) for i in (1,2)]}])
        native.plan_native_story(self.record,self.refs,params,progress=lambda _:None,persist=lambda:None,ask=ask)
        self.assertEqual(ask.call_count,2)  # Both reviews, no new director call.

    def test_story_problem_gets_one_combined_repair_then_stops_before_approval(self):
        bad=good_story_review()
        bad['problems']=[dict(code='missing_event',shot_ids=['shot_001'],
            evidence='画面只有举伞，台词没有解释为何朋友不能一起走。',repair='在原文事实内明确当前困难，并由角色自然回应。')]
        timed=with_performance(self.raw)
        repair={'replacements':[dict(id='shot_001',shots=[copy.deepcopy(timed['shots'][0])])]}
        clear={'reviews':[dict(id=f'shot_{i:03d}',problems=[]) for i in (1,2)]}
        ask=Mock(side_effect=[timed,bad,clear,repair,bad,clear])
        with self.assertRaisesRegex(ValueError,'对白故事表达未通过'):
            native.plan_native_story(self.record,self.refs,{'speech_mode':'dialogue','project_cast':self.cast},
                progress=lambda _:None,persist=lambda:None,ask=ask)
        self.assertEqual(ask.call_count,6)
        state=self.record['native_planning_state']
        self.assertEqual(state['attempt'],1)
        self.assertEqual(state['story_review']['problems'],bad['problems'])
        self.assertEqual(state['review'],clear['reviews'])

    def test_editorial_advice_is_visible_without_creative_retry(self):
        bad=good_story_review()
        bad['problems']=[dict(code='unearned_ending',shot_ids=['shot_001'],
            evidence='原稿采用开放结尾，观众无法判断梦境是否留下真实物品。',repair='作者可以明确梦境含义，也可以保留暧昧。')]
        ask=Mock(side_effect=[with_performance(self.raw),bad,
            {'reviews':[dict(id=f'shot_{i:03d}',problems=[]) for i in (1,2)]}])
        native.plan_native_story(self.record,self.refs,dict(speech_mode='dialogue',project_cast=self.cast),
            progress=lambda _:None,persist=lambda:None,ask=ask)
        self.assertEqual(ask.call_count,3)
        self.assertEqual(self.record['story_review_advisories'],bad['problems'])
        self.assertNotIn('repair',self.record['native_planning_state'])

    def test_editorial_advice_never_leaks_into_local_staging_repair(self):
        state=dict(candidate=copy.deepcopy(self.raw),error='原稿梦境结局需要作者改稿',
            story_review=dict(problems=[dict(code='unearned_ending',shot_ids=['shot_001'],
                evidence='原稿梦境含义模糊',repair='请作者改稿')]),
            review=[dict(id='shot_002',problems=[dict(code='opening_conflict',
                evidence='走到窗前的动作缺失',repair='补齐既定走位')])])
        request=native._repair_request(state)
        self.assertEqual(request['shot_ids'],['shot_002'])
        self.assertEqual(request['story_problems'],[])
        self.assertNotIn('梦境',request['problems'])
        self.assertEqual(request['previous'],self.raw)

    def test_source_advice_remains_advisory_but_adaptation_errors_block(self):
        from backend.app.video_dialogue_story_review import partition_problems
        source=dict(code='missing_event',origin='adopted_story',shot_ids=['shot_001'])
        wrong=dict(code='wrong_speaker',shot_ids=['shot_002'])
        for strict in (False,True):
            blocking,advisory=partition_problems(dict(problems=[source,wrong]),strict=strict)
            self.assertEqual(blocking,[wrong]);self.assertEqual(advisory,[source])

    def test_explicit_retry_with_only_saved_advice_reuses_candidate_and_reviews(self):
        params=self.seed_completed_plan()
        state=self.record['native_planning_state']
        candidate=copy.deepcopy(state['candidate'])
        advice=dict(code='unearned_ending',shot_ids=['shot_001'],evidence='原稿结尾保持开放',repair='作者可以自行选择')
        state['story_review']['problems']=[advice];state['error']='old gate'
        native.retry_failed_plan(self.record)
        ask=Mock()
        native.plan_native_story(self.record,self.refs,params,progress=lambda _:None,persist=lambda:None,ask=ask)
        ask.assert_not_called()
        self.assertEqual(state['candidate'],candidate)

    def test_story_and_staging_feedback_are_combined_before_the_only_repair(self):
        self.record['creation_parameters']={'quality_review_policy':'strict'}
        timed=with_performance(self.raw)
        bad=good_story_review()
        bad['problems']=[dict(code='missing_motivation',shot_ids=['shot_001'],
            evidence='The motivation is not performed.',repair='Express the existing motivation briefly.')]
        staging={'reviews':[dict(id='shot_001',problems=[]),dict(id='shot_002',problems=[dict(
            code='contact_ambiguity',evidence='A new movement starts before existing contact is released.',
            repair='Release contact before moving.')])]}
        repair={'replacements':[dict(id=f'shot_{i+1:03d}',shots=[copy.deepcopy(s)]) for i,s in enumerate(timed['shots'])]}
        clear={'reviews':[dict(id=f'shot_{i:03d}',problems=[]) for i in (1,2)]}
        ask=Mock(side_effect=[timed,bad,staging,repair,good_story_review(),clear])
        native.plan_native_story(self.record,self.refs,{'speech_mode':'dialogue','project_cast':self.cast},
            progress=lambda _:None,persist=lambda:None,ask=ask)
        request=ask.call_args_list[3].args[1]['repair']
        self.assertEqual(request['shot_ids'],['shot_001','shot_002'])
        self.assertEqual(request['story_problems'],bad['problems'])
        self.assertEqual(request['staging_problems'],[staging['reviews'][1]])
        self.assertEqual(self.record['native_planning_state']['attempt'],1)
        self.assertEqual(ask.call_count,6)

    def test_targeted_repair_preserves_unaffected_shot_and_rejects_lost_source(self):
        replacement=copy.deepcopy(self.raw['shots'][1]);replacement['action']='小熊自然接住伞柄。'
        repair={'previous':self.raw,'problems':'动作需澄清','shot_ids':['shot_002']}
        response={'replacements':[{'id':'shot_002','shots':[replacement]}]}
        changed=native._generate_candidate(Mock(return_value=response),'system',{},repair)
        self.assertEqual(changed['shots'][0],self.raw['shots'][0])
        self.assertEqual(changed['shots'][1]['action'],replacement['action'])
        replacement['source_text']='删掉原文。'
        with self.assertRaisesRegex(ValueError,'删改了原文'):
            native._generate_candidate(Mock(return_value=response),'system',{},repair)

    def test_explicit_retry_preserves_failed_evidence_and_creates_local_repair_request(self):
        from backend.app.video_renderability import REVISION
        self.record['native_planning_state']={'candidate':copy.deepcopy(self.raw),'attempt':1,'error':'动作矛盾',
            'fingerprint':'frozen','staging_review_revision':REVISION,
            'review':[{'id':'shot_002','problems':[{'code':'opening_conflict'}]}]}
        before=copy.deepcopy(self.record['native_planning_state'])
        native.retry_failed_plan(self.record)
        self.assertEqual(self.record['native_planning_history'][-1],before)
        self.assertEqual(self.record['native_planning_state']['repair']['shot_ids'],['shot_002'])
        self.assertEqual(self.record['native_planning_state']['repair']['previous'],self.raw)
        self.assertNotIn('candidate',self.record['native_planning_state'])

    def test_continuity_repair_includes_only_explicit_existing_dependencies(self):
        raw={'shots':[{'source_text':str(i)} for i in range(4)]}
        state={'candidate':raw,'error':'continuity conflict','review':[
            {'id':'shot_003','problems':[{'code':'opening_conflict','evidence':'此前shot_002结束时物件打开；shot_003开场却关闭。','repair':'Repair the transition, not nonexistent shot_999 or nested_shot_001.'}]},
            {'id':'shot_004','problems':[{'code':'temporal_overload','evidence':'shot_001 is earlier context.','repair':'Reduce this shot only.'}]}]}
        repair=native._repair_request(state)
        self.assertEqual(repair['shot_ids'],['shot_002','shot_003','shot_004'])
        self.assertEqual(repair['previous'],raw)

    def test_story_review_cannot_omit_shots_or_repair_unknown_shot(self):
        from backend.app.video_dialogue_story_review import review_story
        _,shots=native.compile_storyboard(self.raw,self.record,self.cast,self.refs)
        context=dict(production_text=self.record['native_story']['text'],native_speech_mode='dialogue',project_cast=self.cast)
        bad=good_story_review();bad['reviewed_shot_ids'].pop()
        with self.assertRaisesRegex(ValueError,'完整镜头'):
            review_story(context,shots,ask=Mock(return_value=bad))
        bad=good_story_review();bad['problems']=[dict(code='wrong_speaker',shot_ids=['other'],
            evidence='具体一句台词说错了角色，需要修改归属。',repair='保持逐字台词不变，改成原故事中对应的说话人。')]
        with self.assertRaisesRegex(ValueError,'错误镜头'):
            review_story(context,shots,ask=Mock(return_value=bad))

    def test_invalid_candidate_gets_one_revision_then_stops_and_keeps_evidence(self):
        broken=copy.deepcopy(self.raw);broken['shots'][0]['source_text']='错误原文。'
        ask=Mock(return_value=broken)
        with self.assertRaisesRegex(ValueError,'完整覆盖'):
            native.plan_native_story(self.record,self.refs,{'speech_mode':'dialogue','project_cast':self.cast},
                progress=lambda _:None,persist=lambda:None,ask=ask)
        self.assertEqual(ask.call_count,2)
        self.assertEqual(self.record['native_planning_state']['attempt'],1)
        self.assertIn('candidate',self.record['native_planning_state'])


def good_story_review(shots=None):
    return dict(reviewed_shot_ids=[s['id'] for s in shots] if shots else ['shot_001','shot_002'],
        viewer_question='朋友如何一起避雨，台词和举伞动作给出回答。',
        motivation_and_payoff='两人需要一起回家，一人提出同行，另一人愿意撑伞。',problems=[])


if __name__=='__main__':unittest.main()
