import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from contextlib import ExitStack

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from backend.app import one_click as auto, video_studio as studio, pipeline, db, auth


class OneClickTests(unittest.TestCase):
    def test_explicit_unsubmitted_reroll_keeps_history_without_blocking_run(self):
        pending={'id':'shot_009','kind':'video','video_status':'pending',
                 'video_attempt':4,'video_not_submitted':True,'video_execution_started':False,
                 'video_history':[{'video':'prior.mp4','video_status':'completed'}]}
        project={'status':'video_review','shots':[pending]}
        before=copy.deepcopy(project)
        self.assertEqual(auto.next_action(project),('videos',''))
        self.assertEqual(project,before)
        # An old flag never authorizes replay of a live or uncertain receipt.
        for key,value in (('video_task_id','live'),('video_request',{'receipt':'live'}),
                          ('video_execution_started',True),('video_resume_available',True),
                          ('video_error','timeout'),('video_state_file','receipt')):
            project['shots']=[{**pending,key:value}]
            self.assertIsNone(auto.next_action(project)[0],key)
        for status in ('failed','unknown','running','stopped'):
            project['shots']=[{**pending,'video_status':status}]
            self.assertIsNone(auto.next_action(project)[0],status)

    def test_voice_change_does_not_block_never_completed_shots(self):
        project={'status':'video_generation_ready','shots':[{'id':'a','kind':'video','image_status':'completed',
                  'video_status':'pending','dialogue_stale':True}]}
        self.assertEqual(auto.next_action(project),('videos',''))
        project['shots'][0]['video_status']='completed'
        self.assertIn('音色已修改',auto.next_action(project)[1])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(auto, 'ROOT', Path(self.tmp.name)/'runs'))
        self.stack.enter_context(patch.object(studio, 'ROOT', Path(self.tmp.name)/'videos'))
        self.stack.enter_context(patch.object(studio, 'ACTIVE', set()))
        self.stack.enter_context(patch.object(studio, 'project_has_image_edits', return_value=False))
        self.job = SimpleNamespace(id='a'*12,user_id=1,status='waiting_confirmation',message='',
            request={'dynamic_video':True,'dynamic_auto_advance':True,'_step_mode_stage':'audio_review',
                     'dynamic_execution_owner':'server'})
        self.stack.enter_context(patch.object(pipeline.store, 'get', side_effect=lambda identity: self.job if identity==self.job.id else None))
        self.stack.enter_context(patch.object(db, 'get_user_by_id', side_effect=lambda identity: {'id':1} if identity==1 else None))
        self.project={'id':'b'*32,'revision':1,'status':'draft','shots':[],'logs':[],'error':'',
                      'creation_parameters':{'dynamic_execution_owner':'server'},'source_project':{'id':self.job.id}}
        studio.save(studio.directory(1,self.project['id']),self.project)
        self.importer=self.stack.enter_context(patch.object(studio,'import_project_for_user',return_value=self.project))
        self.dispatched=[]
        def dispatch(action,user,record):
            self.dispatched.append((action,user['id'],record['id']))
            # Like real services, persist the accepted stage before returning.
            record.update(status='planning', revision=record['revision']+1)
            studio.ACTIVE.add(str(studio.directory(1,record['id'])))
            studio.save(studio.directory(1,record['id']),record)
        self.dispatch=self.stack.enter_context(patch.object(auto,'dispatch',side_effect=dispatch))

    def test_no_opt_in_means_no_import_or_dispatch(self):
        auto.tick(1,self.job.id)
        self.importer.assert_not_called();self.dispatch.assert_not_called()

    def test_audio_handoff_runs_without_browser_and_does_not_repeat_stage(self):
        auto.register(self.job)
        self.job.status='running';auto.tick(1,self.job.id)
        self.importer.assert_not_called()
        self.job.status='waiting_confirmation'
        auto.tick(1,self.job.id);auto.tick(1,self.job.id)
        self.assertEqual(self.importer.call_count,1)
        self.assertEqual(self.dispatched,[('plan',1,self.project['id'])])

    def test_failed_dispatch_is_durable_and_never_automatically_retried(self):
        auto.register(self.job);self.dispatch.side_effect=RuntimeError('provider timeout')
        auto.tick(1,self.job.id);auto.tick(1,self.job.id)
        self.assertEqual(self.dispatch.call_count,1)
        self.assertEqual(auto.load(1,self.job.id)['state'],'paused')
        self.assertIn('provider timeout',auto.load(1,self.job.id)['message'])

    def test_restart_with_unconfirmed_dispatch_does_not_resubmit(self):
        state=auto.register(self.job);state['claim']={'action':'images','revision':4};auto.save(state)
        auto.tick(1,self.job.id)
        self.dispatch.assert_not_called()
        self.assertEqual(auto.load(1,self.job.id)['state'],'paused')

    def test_another_active_project_waits_without_claiming_or_repeating(self):
        auto.register(self.job);studio.ACTIVE.add('other')
        auto.tick(1,self.job.id)
        self.dispatch.assert_not_called();self.assertIsNone(auto.load(1,self.job.id)['claim'])
        studio.ACTIVE.clear();auto.tick(1,self.job.id)
        self.assertEqual(self.dispatch.call_count,1)

    def test_wrong_owner_and_cancelled_job_never_dispatch(self):
        state=auto.register(self.job);self.job.user_id=2
        auto.tick(1,self.job.id);self.dispatch.assert_not_called()
        self.assertEqual(auto.load(1,self.job.id)['state'],'paused')
        self.job.user_id=1;auto.register(self.job);self.job.status='cancelled'
        auto.tick(1,self.job.id);self.dispatch.assert_not_called()

    def test_manual_edit_requires_pause_and_internal_dispatch_can_use_same_services(self):
        state=auto.register(self.job)
        with self.assertRaises(HTTPException):studio.editable(self.project,1)
        token=auto._executing.set(True)
        try:studio.editable(self.project,1)
        finally:auto._executing.reset(token)
        auto._pause(state,'user paused');studio.editable(self.project,1)

    def test_progress_and_controls_are_owner_checked(self):
        app=FastAPI();app.include_router(auto.router);client=TestClient(app)
        with patch.object(auth,'require_user',return_value={'id':2}):
            self.assertEqual(client.get('/api/one-click/'+self.job.id).status_code,404)
            self.assertEqual(client.post('/api/one-click/'+self.job.id,json={'action':'resume'}).status_code,404)

    def test_missing_images_or_videos_stop_instead_of_auto_retry(self):
        for status,shots in [('image_review',[{'image_status':'failed'}]),('video_review',[{'kind':'video','video_status':'failed'}])]:
            record={**self.project,'status':status,'shots':shots}
            action,error=auto.next_action(record)
            self.assertIsNone(action);self.assertTrue(error)

    def test_safe_stop_can_continue_untouched_images_without_repeating_attempts(self):
        done={'id':'shot_001','image_status':'completed','image':'assets/ready.jpg','image_attempt_output':'prior'}
        pending={'id':'shot_002','image_status':'pending'}
        record={**self.project,'status':'image_review','shots':[done,pending]}
        before=copy.deepcopy(record)
        self.assertEqual(auto.next_action(record),('images',''))
        self.assertEqual(record,before)
        for status in ('failed','unknown','running','stopped','cancelled'):
            record['shots']=[done,{**pending,'image_status':status}]
            self.assertIsNone(auto.next_action(record)[0],status)
        for key,value in (('image','old.jpg'),('image_error','timeout'),('image_attempt_output','receipt'),
                          ('image_task',{'status':'failed'}),('image_history',[{}]),('native_image_repairs',[{}])):
            record['shots']=[done,{**pending,key:value}]
            self.assertIsNone(auto.next_action(record)[0],key)
        record['shots']=[done]
        self.assertEqual(auto.next_action(record),('confirm_images',''))

    def test_completed_legacy_run_is_not_restarted(self):
        record={**self.project,'status':'completed'}
        self.assertEqual(auto.next_action(record),('complete',''))

    def test_completed_banner_does_not_claim_a_manual_regeneration_is_already_exported(self):
        state=auto.register(self.job)
        state.update(state='completed',project_id=self.project['id']);auto.save(state)
        app=FastAPI();app.include_router(auto.router);client=TestClient(app)
        with patch.object(auth,'require_user',return_value={'id':1}):
            for stage in ('video_generating','video_review','image_review'):
                self.project['status']=stage;studio.save(studio.directory(1,self.project['id']),self.project)
                response=client.get('/api/one-click/'+self.job.id)
                self.assertEqual(response.status_code,200)
                self.assertEqual(response.json()['state'],'paused')
                self.assertIn('局部修改',response.json()['message'])
                self.assertEqual(auto.load(1,self.job.id)['state'],'completed')
                auto.tick(1,self.job.id);self.dispatch.assert_not_called()
            self.project['status']='completed';studio.save(studio.directory(1,self.project['id']),self.project)
            self.assertEqual(client.get('/api/one-click/'+self.job.id).json()['state'],'completed')
            self.project['archived']=True;studio.save(studio.directory(1,self.project['id']),self.project)
            self.assertEqual(client.get('/api/one-click/'+self.job.id).json()['state'],'paused')
            (studio.directory(1,self.project['id'])/'record.json').unlink()
            self.assertEqual(client.get('/api/one-click/'+self.job.id).json()['state'],'paused')

    def test_recovered_clip_allows_only_untouched_remainder_to_continue(self):
        finished={'kind':'video','video_status':'completed','video_task_id':'accepted-clip','video_attempt':1}
        pending={'kind':'video','video_status':'pending'}
        for status in ('video_review','video_generation_ready'):
            record={**self.project,'status':status,'shots':[finished,pending,{'kind':'static'}]}
            self.assertEqual(auto.next_action(record),('videos',''))
            record['shots']=[finished,{'kind':'static'}]
            self.assertEqual(auto.next_action(record),('export',''))
            for uncertain in ('failed','unknown','running','stopped','cancelled'):
                record['shots']=[finished,{**pending,'video_status':uncertain}]
                self.assertIsNone(auto.next_action(record)[0])
            for key,value in (('video_request',{'identity':'accepted'}),('video_task_id','old-task'),
                              ('video_attempt',1),('video_execution_started',True),
                              ('video_state_file','receipt'),('video_resume_available',True),('video_error','timeout')):
                record['shots']=[finished,{**pending,key:value}]
                self.assertIsNone(auto.next_action(record)[0],key)

    def test_manual_export_reconciles_paused_banner_without_resuming_or_writing(self):
        state=auto.register(self.job)
        state.update(state='paused',project_id=self.project['id'],message='old failure');auto.save(state)
        self.project['status']='completed';studio.save(studio.directory(1,self.project['id']),self.project)
        app=FastAPI();app.include_router(auto.router);client=TestClient(app)
        with patch.object(auth,'require_user',return_value={'id':1}):
            response=client.get('/api/one-click/'+self.job.id)
        self.assertEqual(response.json()['state'],'completed')
        self.assertEqual(auto.load(1,self.job.id)['state'],'paused')
        self.dispatch.assert_not_called()

    def test_changed_project_banner_uses_current_resume_blocker_without_writes(self):
        state=auto.register(self.job)
        state.update(state='paused',project_id=self.project['id'],message='Old image failure',paused_project_revision=1)
        auto.save(state)
        self.project.update(status='video_review',revision=2,shots=[dict(
            id='shot_003',kind='video',video_status='failed',video_error='Current preparation failed')])
        path=studio.directory(1,self.project['id']);studio.save(path,self.project)
        before=(path/'record.json').read_bytes()
        app=FastAPI();app.include_router(auto.router)
        with TestClient(app) as client, patch.object(auth,'require_user',return_value={'id':1}):
            value=client.get('/api/one-click/'+self.job.id).json()
        self.assertEqual(value['message'],auto.next_action(self.project)[1])
        self.assertIn('shot_003',value['message'])
        self.assertIn('Current preparation failed',value['message'])
        self.assertNotIn('Old image failure',value['message'])
        self.assertEqual((path/'record.json').read_bytes(),before)
        self.assertEqual(auto.load(1,self.job.id),state)


class PlanningDispatchFreshnessTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(studio,'ROOT',Path(temporary.name)/'projects'))
        self.stack.enter_context(patch.object(auto,'ROOT',Path(temporary.name)/'runs'))
        self.project={'id':'c'*32,'revision':3,'status':'draft','shots':[], 'logs':[], 'error':'',
                      'native_story':{'text':'Adopted story'},'native_owner_id':1}
        self.path=studio.directory(1,self.project['id']);studio.save(self.path,self.project)
        self.state=auto.register_native(1,self.project)
        self.state['claim']={'action':'plan','revision':3};auto.save(self.state)
        self.plan=self.stack.enter_context(patch.object(studio,'plan_for_user',return_value={'status':'planning'}))
        token=auto._executing.set(True);self.addCleanup(auto._executing.reset,token)

    def test_current_claim_can_plan_for_native_and_source_job_routes(self):
        self.assertEqual(auto.dispatch('plan',{'id':1},self.project),{'status':'planning'})
        self.plan.assert_called_once_with(self.project['id'],{'id':1})
        self.plan.reset_mock()
        legacy=copy.deepcopy(self.project);legacy.pop('native_story')
        legacy['source_project']={'id':'d'*12};studio.save(self.path,legacy)
        state={**self.state,'job_id':'d'*12};auto.save(state)
        auto.dispatch('plan',{'id':1},legacy)
        self.plan.assert_called_once()

    def test_pause_between_claim_and_dispatch_cannot_start_planning(self):
        auto._pause(self.state,'User paused')
        before=(self.path/'record.json').read_bytes()
        auto.dispatch('plan',{'id':1},self.project)
        self.plan.assert_not_called()
        self.assertEqual((self.path/'record.json').read_bytes(),before)
        self.assertEqual(auto.load(1,self.project['id'])['state'],'paused')

    def test_stale_revision_or_stage_does_not_plan_or_change_record(self):
        for changes in ({'revision':4},{'status':'storyboard_review','shots':[{'id':'shot_1'}]}):
            with self.subTest(changes=changes):
                current={**self.project,**changes};studio.save(self.path,current)
                before=(self.path/'record.json').read_bytes()
                with self.assertRaisesRegex(ValueError,'版本或阶段'):
                    auto.dispatch('plan',{'id':1},self.project)
                self.plan.assert_not_called()
                self.assertEqual((self.path/'record.json').read_bytes(),before)

    def test_replaced_claim_never_authorizes_an_old_planning_dispatch(self):
        self.state['claim']={'action':'images','revision':3};auto.save(self.state)
        with self.assertRaisesRegex(ValueError,'提交记录已变化'):
            auto.dispatch('plan',{'id':1},self.project)
        self.plan.assert_not_called()
        self.assertEqual(auto.load(1,self.project['id'])['claim'],self.state['claim'])


if __name__=='__main__':unittest.main()
