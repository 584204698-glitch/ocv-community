import copy
import json
import unittest
import io
import wave
from unittest.mock import patch

from backend.app import voice_design, video_dialogue as dialogue, video_inputs as inputs, video_studio as studio, video_generation as generation
from tests import test_video_project_cast_api as fixtures


def sample_voice(index):
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as writer:
        writer.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
        writer.writeframes(int(index).to_bytes(2, 'little') * (24000 * 4))
    return buffer.getvalue()


class ProjectVoiceApiTest(unittest.TestCase):
    read = fixtures.ProjectCastApiTest.read
    save_cast = fixtures.ProjectCastApiTest.save_cast
    make_cast = fixtures.ProjectCastApiTest.make_cast
    def setUp(self):
        fixtures.ProjectCastApiTest.setUp(self)
        handle = patch.object(voice_design, 'ROOT', self.root / 'voices')
        handle.start(); self.addCleanup(handle.stop)
        for index in ('1', '2'):
            folder = voice_design.ROOT / '1' / (index * 32)
            (folder / 'design').mkdir(parents=True)
            (folder / 'record.json').write_text(json.dumps({'id':index*32, 'status':'saved'}))
            (folder / 'design/chunk-0001.wav').write_bytes(sample_voice(index))

    def bind(self, identity, voice='qwen3:'+'1'*32, revision=None):
        return self.client.put(self.base + '/project-voices', json={
            'revision': self.read()['revision'] if revision is None else revision,
            'character_id': identity, 'voice_id': voice})

    def test_bind_persists_without_altering_images_or_completed_requests(self):
        cast=self.make_cast();before=self.read()
        response=self.bind(cast['id'])
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['characters'][0]['status'],'current')
        self.assertEqual(self.read()['shots'],before['shots'])
        self.assertEqual(self.read()['project_cast'],before['project_cast'])
        self.assertNotIn('path',response.text)
        stable=self.read()
        self.assertEqual(self.bind(cast['id']).status_code,200)
        self.assertEqual(self.read(),stable)
        self.assertEqual(self.bind(cast['id'],None).json()['characters'][0]['status'],'unbound')
        self.assertTrue((voice_design.ROOT/'1'/('1'*32)/'design/chunk-0001.wav').is_file())

    def test_revision_project_account_and_saved_voice_are_checked(self):
        cast=self.make_cast();before=self.read()
        for identity,voice,revision,code in [
            ('pc_'+'f'*32,'qwen3:'+'1'*32,None,400),
            (cast['id'],'qwen3:'+'f'*32,None,400),
            (cast['id'],'qwen3:'+'1'*32,0,409)]:
            self.assertEqual(self.bind(identity,voice,revision).status_code,code)
        self.assertEqual(self.read(),before)
        with patch.object(studio,'require_user',lambda request:{'id':2}):
            self.assertEqual(self.client.get(self.base+'/project-voices').status_code,404)
        record=self.read();record['project_voices']={'version':1,'owner_id':2,'project_id':record['id'],'characters':{}}
        studio.save(self.path,record)
        self.assertEqual(self.client.get(self.base+'/project-voices').status_code,409)

    def test_changed_voice_bytes_are_stale_not_silently_rebound(self):
        cast=self.make_cast();self.bind(cast['id'])
        (voice_design.ROOT/'1'/('1'*32)/'design/chunk-0001.wav').write_bytes(b'changed')
        response=self.client.get(self.base+'/project-voices')
        self.assertEqual(response.json()['characters'][0]['status'],'stale')

    def test_invalid_h3_voice_is_rejected_at_binding_without_changing_project(self):
        from tests.test_h3_voice_reference import pcm
        cast=self.make_cast()
        self.assertEqual(self.bind(cast['id']).status_code,200)
        before=self.read()
        audio=voice_design.ROOT/'1'/('2'*32)/'design/chunk-0001.wav'
        for data, message in [(pcm(1.9),'至少需要 2 秒'),(b'not a wave file','PCM WAV')]:
            audio.write_bytes(data)
            response=self.bind(cast['id'],'qwen3:'+'2'*32)
            self.assertEqual(response.status_code,400,response.text)
            self.assertIn(message,response.text)
            self.assertEqual(self.read(),before)
            self.assertEqual(audio.read_bytes(),data)
        # Existing long samples remain usable through the five-second policy.
        audio.write_bytes(pcm(16))
        self.assertEqual(self.bind(cast['id'],'qwen3:'+'2'*32).status_code,200)
        self.assertEqual(audio.read_bytes(),pcm(16))

    def test_legacy_short_binding_is_not_reported_as_current(self):
        import hashlib
        from tests.test_h3_voice_reference import pcm
        cast=self.make_cast();self.bind(cast['id'])
        audio=voice_design.ROOT/'1'/('1'*32)/'design/chunk-0001.wav'
        audio.write_bytes(pcm(1.9))
        record=self.read()
        record['project_voices']['characters'][cast['id']]['sha256']=hashlib.sha256(audio.read_bytes()).hexdigest()
        studio.save(self.path,record)
        response=self.client.get(self.base+'/project-voices')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['characters'][0]['status'],'stale')

    def test_voice_change_marks_only_spoken_shots_stale_without_losing_old_task(self):
        cast=self.make_cast();self.bind(cast['id'])
        record=self.read();record['status']='completed'
        record['shots'][0].update(video_status='completed',video='old.mp4',video_task_id='old-task',
            video_request={'dialogue':{'plan':{'turns':[{'character_id':cast['id'],'text':'你好。'}]}}})
        studio.save(self.path,record)
        response=self.bind(cast['id'],'qwen3:'+'2'*32)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['stale_shot_ids'],['shot-A'])
        current=self.read()
        self.assertEqual(current['status'],'video_review')
        self.assertEqual(current['shots'][0]['video_task_id'],'old-task')
        self.assertEqual(current['shots'][0]['video_request'],record['shots'][0]['video_request'])
        self.assertEqual(current['shots'][1],record['shots'][1])

    def test_narrator_is_off_screen_and_uses_own_project_voice_binding(self):
        rows,record,shot,images=self.prepared()
        narrator='pc_'+'d'*32
        record['native_narrator']={'id':narrator}
        record['project_voices']=dialogue.bind_voice(1,record,narrator,'qwen3:'+'1'*32)
        shot['generation_duration']=6
        shot['native_dialogue']['duration']=6
        shot['native_dialogue']['speakers'].append({'character_id':narrator,'name':'旁白','kind':'narrator','on_screen':False})
        shot['native_dialogue']['turns'].append({'character_id':narrator,'text':'她笑了。','start':4,'end':5.5})
        prepared=dialogue.prepare_dialogue(1,self.path,record,shot,{'input_mode':'composition_reference'},images)
        self.assertEqual(prepared['voices'][2]['character_id'],narrator)
        self.assertIn('says in an off-screen voiceover',prepared['prompt'])
        self.assertNotIn('<Subject 3>',prepared['prompt'])

    def prepared(self):
        rows=self.save_cast([{'name':'姐姐','reference_id':'actor-a'}, {'name':'妹妹','reference_id':'actor-b'}])['characters']
        for index,row in enumerate(rows,1):
            self.bind(row['id'],'qwen3:'+str(index)*32)
        record=self.read();shot=record['shots'][0]
        # Deliberately reverse both picture selection and speaking order.
        images=[{'id':'core','role':'core','path':self.path/'core.png'},
                {'id':'actor-b','role':'identity_reference','path':self.path/'actor-b.png'},
                {'id':'actor-a','role':'identity_reference','path':self.path/'actor-a.png'}]
        shot['native_dialogue']={'duration':4,'scene':'The two sisters sit across the table.',
            'speakers':[{'character_id':r['id'],'name':'ignored label','picture':9,
                         'position':'the left' if i==0 else 'the right','voice_id':'qwen3:'+'f'*32}
                        for i,r in enumerate(rows)],
            'turns':[{'character_id':rows[1]['id'],'text':'姐姐。','start':0.2,'end':1.5},
                     {'character_id':rows[0]['id'],'text':'来啦。','start':2,'end':3.5}]}
        return rows,record,shot,images

    def confirmed_core_inputs(self):
        from backend.app.video_character_coverage import CharacterDeclaration, build_character_review
        rows,record,shot,images=self.prepared()
        images[0]['role']='composition'
        selection={'input_mode':'composition_reference','reference_ids':['actor-b']}
        shot['video_input_selection']=selection
        declarations=[CharacterDeclaration(name=r['name'],presence='opening',reference_id=r['reference_id'],
            project_character_id=r['id'],core_identity_confirmed=True) for r in rows]
        shot['video_character_review']=build_character_review(1,self.path,record,shot,selection,
                                                             inputs.asset_rows(record),declarations)
        return rows,record,shot,images[:2],selection

    def test_confirmed_core_rebind_preserves_character_subject_and_voice_order(self):
        rows,record,shot,images,selection=self.confirmed_core_inputs()
        before=copy.deepcopy(shot)
        compiled=dialogue.prepare_dialogue(1,self.path,record,shot,selection,images)
        self.assertEqual([s['picture'] for s in compiled['plan']['speakers']],[1,2])
        self.assertEqual([s['character_id'] for s in compiled['plan']['speakers']],[r['id'] for r in rows])
        self.assertEqual([v['character_id'] for v in compiled['voices']],[rows[1]['id'],rows[0]['id']])
        self.assertIn('<Subject 1> is the explicitly confirmed opening character',compiled['prompt'])
        self.assertIn('<Audio 1> is the voice-timbre reference for <Subject 2> (S1)',compiled['prompt'])
        self.assertEqual(compiled['basis']['core_identity_confirmations']['character_ids'],[rows[0]['id']])
        self.assertEqual(shot,before)
        shot['video_input_selection']={**selection,'reference_ids':[]}
        both=dialogue.prepare_dialogue(1,self.path,record,shot,shot['video_input_selection'],images[:1])
        self.assertEqual([s['picture'] for s in both['plan']['speakers']],[1,1])
        self.assertEqual(both['voices'],compiled['voices'])

    def test_core_rebind_rejects_unconfirmed_stale_later_offscreen_or_implicit_selection(self):
        rows,record,shot,images,selection=self.confirmed_core_inputs()
        for change in ('unconfirmed','legacy','stale','later','cast_later','offscreen','implicit','unlinked'):
            candidate=copy.deepcopy(shot)
            if change=='unconfirmed':candidate['video_character_review']['characters'][0]['core_identity_confirmed']=False
            if change=='legacy':candidate.pop('video_character_review')
            if change=='stale':candidate['action']='Changed approved action'
            if change=='later':candidate['native_dialogue']['speakers'][0]['presence']='later'
            if change=='cast_later':candidate['cast_plan']={'characters':[{'project_character_id':rows[0]['id'],'presence':'later'}]}
            if change=='offscreen':candidate['native_dialogue']['speakers'][0]['on_screen']=False
            if change=='implicit':candidate.pop('video_input_selection')
            if change=='unlinked':candidate['video_character_review']['characters'][0]['project_character_id']=''
            with self.subTest(change=change),self.assertRaisesRegex(ValueError,'身份参考图'):
                dialogue.prepare_dialogue(1,self.path,record,candidate,selection,images)

    def test_picture_only_rebind_reuses_verified_audio_without_tts_or_track_rebuild(self):
        import hashlib
        from backend.app import video_external_dialogue as exact
        from backend.app.video_manual_h3 import adopted
        from tests.test_video_external_dialogue import audio
        rows,record,shot,images,selection=self.confirmed_core_inputs()
        old_images=[*images,{'id':'actor-a','role':'identity_reference','path':self.path/'actor-a.png'}]
        old=dialogue.prepare_dialogue(1,self.path,record,shot,selection,old_images)
        clips={}
        for index,turn in enumerate(old['plan']['turns']):
            file=audio(self.path/f'clip-{index}',1)
            clips[index]=dict(file=file.relative_to(self.path).as_posix(),duration=1,
                              sha256=hashlib.sha256(file.read_bytes()).hexdigest())
        visual,timed=exact.retime(old['plan'],clips)
        file=audio(self.path/'guide',visual['duration'])
        external=dict(version=exact.REVISION,plan=visual,clips=timed,
            key=exact.digest(dict(version=exact.REVISION,plan=visual,clips=timed)),
            guide=dict(file=file.relative_to(self.path).as_posix(),duration=visual['duration'],
                       sha256=hashlib.sha256(file.read_bytes()).hexdigest()))
        record['creation_parameters'].update(dialogue_route='external_tts',dialogue_tts_engine='qwen3')
        old['basis'].update(dialogue_route='external_tts',dialogue_tts_engine='qwen3',external_dialogue=external)
        shot['video_request']=dict(dialogue=old,external_dialogue=external)
        shot['manual_h3_prompt']=dict(prompt=old['prompt'],fingerprint=old['fingerprint'])
        studio._invalidate_shot_video(record,shot,'Input pictures explicitly changed')
        self.assertNotIn('video_request',shot)
        with patch.object(exact,'prepare',side_effect=AssertionError('no new audio preparation')):
            new=dialogue.prepare_dialogue(1,self.path,record,shot,selection,images)
            generated=dialogue.prepare_dialogue(1,self.path,record,shot,selection,images,prepare_narration=True)
        self.assertEqual(new,generated)
        self.assertEqual(new['external_dialogue']['guide'],external['guide'])
        self.assertEqual(new['external_dialogue']['clips'],external['clips'])
        self.assertEqual([s['picture'] for s in new['render_plan']['speakers']],[1,2])
        with self.assertRaisesRegex(ValueError,'已变化'):adopted(shot,new)
        file.write_bytes(b'changed guide')
        with patch.object(exact,'prepare') as prepare,self.assertRaisesRegex(ValueError,'已变化'):
            dialogue.prepare_dialogue(1,self.path,record,shot,selection,images)
        prepare.assert_not_called()

    def test_generated_binding_has_no_reference_and_draft_cannot_opt_in(self):
        from backend.app.one_click import require_native_voices
        rows,record,shot,images=self.prepared()
        record['shots']=[shot]
        with patch.object(dialogue, 'resolve_saved_voice', side_effect=AssertionError('must not resolve')):
            record['project_voices']=dialogue.bind_voice(1,record,rows[1]['id'],None,'generated')
        self.assertEqual(record['project_voices']['characters'][rows[1]['id']],{'voice_mode':'generated'})
        view=dialogue.voice_view(1,record)
        self.assertEqual(view['characters'][1]['status'],'generated')
        self.assertIsNone(view['characters'][1]['voice_id'])
        shot['native_dialogue']['speakers'][0]['voice_mode']='generated'
        prepared=dialogue.prepare_dialogue(1,self.path,record,shot,{'input_mode':'composition_reference'},images)
        self.assertNotIn('voice_mode',prepared['plan']['speakers'][0])
        self.assertEqual(prepared['plan']['speakers'][1]['voice_mode'],'generated')
        self.assertEqual(prepared['voices'][0]['speaker'],'S2')
        require_native_voices(1,record)
        record['project_voices']['characters'].pop(rows[0]['id'])
        with self.assertRaises(ValueError):
            dialogue.prepare_dialogue(1,self.path,record,shot,{'input_mode':'composition_reference'},images)
        with self.assertRaises(ValueError): require_native_voices(1,record)
        shot['native_dialogue']['turns']=[]
        prepared=dialogue.prepare_dialogue(1,self.path,record,shot,{'input_mode':'composition_reference'},images)
        self.assertEqual(prepared['voices'],[])
        require_native_voices(1,record)
        with self.assertRaises(ValueError): dialogue.bind_voice(1,record,'pc_'+'f'*32,None,'generated')
        with self.assertRaises(ValueError): dialogue.bind_voice(1,record,rows[0]['id'],None,'automatic')

    def test_automatic_binding_uses_ids_actual_picture_order_and_project_voice(self):
        rows,record,shot,images=self.prepared()
        prepared=dialogue.prepare_dialogue(1,self.path,record,shot,{'input_mode':'composition_reference'},images)
        self.assertEqual(prepared['plan']['speakers'][0]['picture'],3)
        self.assertEqual(prepared['plan']['speakers'][1]['picture'],2)
        self.assertEqual(prepared['voices'][0]['voice_id'],'qwen3:'+'2'*32)
        self.assertEqual(prepared['voices'][1]['voice_id'],'qwen3:'+'1'*32)
        self.assertIn('<Audio 1> is the voice-timbre reference for <Subject 2> (S1)',prepared['prompt'])
        changed=copy.deepcopy(record);changed['project_voices']['characters'][rows[0]['id']]['sha256']='0'*64
        with self.assertRaisesRegex(ValueError,'已变化'):
            dialogue.prepare_dialogue(1,self.path,changed,shot,{'input_mode':'composition_reference'},images)
        with self.assertRaisesRegex(ValueError,'首尾帧'):
            dialogue.prepare_dialogue(1,self.path,record,shot,{'input_mode':'first_last'},images)
        with self.assertRaisesRegex(ValueError,'缺少说话角色'):
            dialogue.prepare_dialogue(1,self.path,record,shot,{'input_mode':'composition_reference'},images[:2])

    def test_normal_worker_freezes_both_voices_and_never_calls_effects_only_converter(self):
        rows,record,shot,_=self.prepared()
        record['status']='video_generation_ready'
        shot['video_input_selection']={'input_mode':'composition_reference',
            'profile_id':'composition_reference','reference_ids':['actor-b','actor-a']}
        record['creation_parameters']['comfyui_h3_prompt_agent']=True
        studio.save(self.path,record)
        def fake_media(*args,**kwargs):
            self.assertEqual([p.read_bytes() for p in kwargs['voice_reference_paths']], [sample_voice(2),sample_voice(1)])
            self.assertIsNone(kwargs['audio_path'])
            self.assertIn('<d>[Chinese] 姐姐.</d>',kwargs['prompt'])
            self.assertNotIn('No intelligible speech',kwargs['prompt'])
            kwargs['on_submitted']('synthetic-dialogue-task')
            kwargs['output_path'].write_bytes(b'synthetic-clip')
        def timeline(system, data):
            return {'scene':'A steady view of two seated sisters facing each other.',
                'performance_beats':[dict(**interval,
                action='The sisters remain seated facing each other.',action_zh='姐妹俩面对面坐着。')
                for interval in data['required_intervals']]}
        def grounded(**kwargs):
            self.assertEqual(kwargs['image_data']['mime_type'],'image/jpeg')
            plan=json.loads(kwargs['user_prompt'])['plan']
            self.assertEqual([s['picture'] for s in plan['speakers']],[3,2])
            return json.dumps(dict(positions=[dict(character_id=s['character_id'],position=s['position'])
                for s in plan['speakers']],performance_beats=plan['performance_beats'],
                observations='核心图中的姐妹俩面对面坐着。',conflicts=[],
                mouth_visibility=[dict(turn_index=i,character_id=t['character_id'],mouth_visible=True,
                    basis='Both faces are visible in the image.') for i,t in enumerate(plan['turns'])]))
        with patch.object(generation.threading,'Thread') as thread, \
             patch('backend.app.video_agents.ask_json',side_effect=timeline), \
             patch('backend.app.gemini_client.generate_gemini_text',side_effect=grounded) as grounding, \
             patch.object(generation,'convert_for_h3') as effects_agent, \
             patch.object(generation,'run_video_profile',side_effect=fake_media) as bridge:
            generation._start_local_worker(self.path,record,['shot-A'],1,'composition_reference',use_h3_agent=True)
            thread.call_args.kwargs['target']()
            current=self.read()
            self.assertEqual(current['shots'][0]['video_status'],'completed',current['logs'])
            bridge.assert_called_once();effects_agent.assert_not_called()
            grounding.assert_called_once()
        frozen=current['shots'][0]['video_request']
        self.assertEqual(current['shots'][0]['native_dialogue']['scene'],
                         'A steady view of two seated sisters facing each other.')
        self.assertIn('A steady view of two seated sisters facing each other.',frozen['prompt'])
        self.assertEqual(frozen['audio_policy']['mode'],'native_dialogue')
        self.assertEqual(frozen['native_visual_grounding']['observations'],'核心图中的姐妹俩面对面坐着。')
        self.assertEqual(len(frozen['voice_manifest']),2)
        self.assertEqual(frozen['voice_manifest'][0]['character_id'],rows[1]['id'])
        self.assertEqual(frozen['voice_manifest'][0]['duration'],4)
        self.assertEqual(frozen['voice_manifest'][0]['policy'],'qwen-pcm-quiet-boundary-5s-v2')
        self.assertEqual(frozen['voice_manifest'][0]['source_sha256'],frozen['voice_manifest'][0]['sha256'])
        self.assertNotIn('path',frozen['voice_manifest'][0])
        self.assertEqual(current['shots'][1],record['shots'][1])
