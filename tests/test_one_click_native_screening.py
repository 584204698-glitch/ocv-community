import unittest
import copy
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch
from backend.app import one_click as auto, video_generation, video_dialogue_quality, video_export
from backend.app import video_studio as studio, db, video_native_image_quality as images, video_quality


class NativeIncrementalScreeningTest(unittest.TestCase):
    def test_next_video_checks_own_inputs_while_export_still_checks_entire_project(self):
        record=self.record();record['creation_parameters']['quality_review_policy']='manual'
        def readiness(path,project,*,shot_id=None):
            return 'older completed core reference changed' if shot_id is None else ''
        with patch.object(studio,'storyboard_review_error',side_effect=readiness) as check, \
             patch.object(video_generation,'generate_for_user') as generate:
            auto.dispatch('videos',{'id':1},record)
            self.assertEqual(check.call_args.kwargs,{'shot_id':'shot_002'})
            self.assertEqual(generate.call_args.args[1].shot_ids,['shot_002'])
            with self.assertRaisesRegex(ValueError,'older completed core reference changed'):
                auto.dispatch('export',{'id':1},record)
        with patch.object(studio,'storyboard_review_error',return_value='current image stale'), \
             patch.object(video_generation,'generate_for_user') as generate:
            with self.assertRaisesRegex(ValueError,'current image stale'):auto.dispatch('videos',{'id':1},record)
            generate.assert_not_called()

    def setUp(self):
        patcher=patch.object(studio,'storyboard_review_error',return_value='')
        patcher.start();self.addCleanup(patcher.stop)
        patcher=patch.object(images,'check_native_core_images',return_value=[])
        self.image_check=patcher.start();self.addCleanup(patcher.stop)
        patcher=patch.object(video_quality,'check_automatic_export',return_value=[])
        self.visual_check=patcher.start();self.addCleanup(patcher.stop)

    def test_bad_core_image_pauses_without_redraw_or_confirmation_at_both_entries(self):
        reports=[{'review':{'issues':[{'shot_id':'shot_002','correction':'Restore the approved medium shot.'}]}}]
        self.image_check.side_effect=images.CoreImageReviewRequired('wrong hand',reports)
        for action in ('confirm_images', 'videos'):
            record=self.record();before=copy.deepcopy(record)
            with self.subTest(action=action), \
                 patch('backend.app.video_native_image_repair.start_repair') as repair, \
                 patch.object(studio,'confirm_storyboard_images_for_user') as confirm, \
                 patch.object(video_generation,'generate_for_user') as generate:
                with self.assertRaisesRegex(images.CoreImageReviewRequired, 'wrong hand'):
                    auto.dispatch(action,{'id':1},record)
            self.assertEqual(record,before)
            repair.assert_not_called();confirm.assert_not_called();generate.assert_not_called()

    def test_new_image_review_never_automatically_replaces_completed_video(self):
        import copy
        reports=[{'review':{'issues':[{'shot_id':'shot_001'}]}}]
        self.image_check.side_effect=images.CoreImageReviewRequired('changed decoration',reports)
        record=self.record();before=copy.deepcopy(record)
        with patch('backend.app.video_native_image_repair.start_repair') as repair, \
             patch.object(video_generation,'generate_for_user') as generate:
            with self.assertRaisesRegex(ValueError,'自动修图不会回退'):
                auto.dispatch('videos',{'id':1},record)
        self.assertEqual(record,before)
        repair.assert_not_called();generate.assert_not_called()

    def test_pending_video_receipt_is_preserved_even_without_completed_status(self):
        record=self.record();record['shots'][1]['video_task_id']='accepted-job'
        reports=[{'review':{'issues':[{'shot_id':'shot_002'}]}}]
        with self.assertRaisesRegex(ValueError,'shot_002'):
            auto._preserve_produced_shots(record,reports)
        # A failed preparation with no GPU request can still repair its image.
        record['shots'][1].pop('video_task_id')
        auto._preserve_produced_shots(record,reports)

    def test_automatic_image_repair_requires_live_matching_claim_and_explicit_correction(self):
        record=self.record();record['status']='image_review'
        reports=[{'review':{'issues':[{'shot_id':'shot_002','correction':'Restore the approved medium shot.'}]}}]
        failure=images.CoreImageReviewRequired('wrong framing',reports)
        self.image_check.side_effect=failure
        state={'state':'running','project_id':record['id'],
               'claim':{'action':'confirm_images','revision':3}}
        token=auto._executing.set(True)
        self.addCleanup(auto._executing.reset,token)
        with patch.object(auto,'load',return_value=state), \
             patch.object(studio,'read',return_value=record), \
             patch('backend.app.video_native_image_repair.start_repair',return_value=record) as repair, \
             patch.object(studio,'confirm_storyboard_images_for_user') as confirm:
            def submit(*args,**kwargs):
                self.assertTrue(studio.LOCK._is_owned())
                return record
            repair.side_effect=submit
            self.assertIs(auto.dispatch('confirm_images',{'id':1},record),record)
            repair.assert_called_once_with(record['id'],3,{'id':1},reports,allow_design_adaptation=False)
            confirm.assert_not_called()
            repair.reset_mock()
            for change in ('history','missing_correction','uncertain','unknown_shot',
                           'claim','project','revision','stage','completed_video','terminal_request','dependent_video'):
                current=copy.deepcopy(record);current_state=copy.deepcopy(state)
                current_reports=copy.deepcopy(reports)
                if change=='history':current['shots'][1]['native_image_repairs']=[{'repair_revision':'old'}]
                if change=='missing_correction':current_reports[0]['review']['issues'][0]['correction']='  '
                if change=='uncertain':current_reports[0]['review']['issues']=[]
                if change=='unknown_shot':current_reports[0]['review']['issues'][0]['shot_id']='unknown'
                if change=='claim':current_state['claim']['action']='videos'
                if change=='project':current_state['project_id']='b'*32
                if change=='revision':current['revision']=4
                if change=='stage':current['status']='video_review'
                if change=='completed_video':current['shots'][1]['video_status']='completed'
                if change=='terminal_request':
                    current['shots'][1].update(video_request={'prompt':'approved'},video_terminal=True)
                if change=='dependent_video':
                    current['shots'][0]['core_reference_used']={'source_shot_id':'shot_002'}
                self.image_check.side_effect=images.CoreImageReviewRequired('wrong framing',current_reports)
                with self.subTest(change=change), patch.object(auto,'load',return_value=current_state), \
                     patch.object(studio,'read',return_value=current):
                    with self.assertRaises(ValueError):auto.dispatch('confirm_images',{'id':1},record)
                repair.assert_not_called();confirm.assert_not_called()

    def test_pause_between_review_and_repair_acceptance_wins(self):
        record=self.record();record['status']='image_review'
        reports=[{'review':{'issues':[{'shot_id':'shot_002','correction':'Restore the approved medium shot.'}]}}]
        self.image_check.side_effect=images.CoreImageReviewRequired('wrong framing',reports)
        running={'state':'running','project_id':record['id'],
                 'claim':{'action':'confirm_images','revision':3}}
        token=auto._executing.set(True)
        try:
            with patch.object(auto,'load',side_effect=[running,{'state':'paused'}]), \
                 patch.object(studio,'read',return_value=record), \
                 patch('backend.app.video_native_image_repair.start_repair') as repair, \
                 patch.object(studio,'confirm_storyboard_images_for_user') as confirm:
                self.assertIs(auto.dispatch('confirm_images',{'id':1},record),record)
                repair.assert_not_called();confirm.assert_not_called()
        finally:auto._executing.reset(token)

    def record(self,native=True):
        result={'id':'a'*32,'revision':3,'creation_parameters':{'quality_review_policy':'strict'},'shots':[
            {'id':'shot_001','kind':'video','video_status':'completed'},
            {'id':'shot_002','kind':'video','video_status':'pending'},
            {'id':'shot_003','kind':'video','video_status':'pending'}]}
        if native:result['native_story']={'text':'A full adopted story.'}
        return result

    def test_native_checks_completed_media_then_submits_only_next_untouched_shot(self):
        record=self.record()
        with patch.object(video_dialogue_quality,'check_native_speech',return_value=[]) as screen, \
             patch.object(video_generation,'generate_for_user') as generate:
            auto.dispatch('videos',{'id':1},record)
            self.assertEqual([s['id'] for s in screen.call_args.args[1]['shots']],['shot_001'])
            self.assertEqual(generate.call_args.args[1].shot_ids,['shot_002'])
            self.assertEqual([s['id'] for s in self.visual_check.call_args.args[1]['shots']],['shot_001'])
            self.assertEqual(record,self.record())

    def test_scene_change_blocks_next_shot_without_rerendering_or_rewriting(self):
        self.visual_check.side_effect=ValueError('scene changed at 2 seconds')
        record=self.record()
        with patch.object(video_dialogue_quality,'check_native_speech',return_value=[]), \
             patch.object(video_generation,'generate_for_user') as generate:
            with self.assertRaisesRegex(ValueError,'scene changed'):
                auto.dispatch('videos',{'id':1},record)
        generate.assert_not_called()
        self.assertEqual(record,self.record())

    def test_speech_and_visual_failure_are_reported_together_without_blind_speech_retake(self):
        self.visual_check.side_effect=ValueError('scene changed at 2 seconds')
        failure=video_dialogue_quality.NativeSpeechReviewRequired('extra speech','shot_001',{'review':{}})
        with patch.object(video_dialogue_quality,'check_native_speech',side_effect=failure), \
             patch('backend.app.video_native_speech_repair.retake') as retake, \
             patch.object(video_generation,'generate_for_user') as generate:
            with self.assertRaisesRegex(ValueError,'extra speech.*scene changed'):
                auto.dispatch('videos',{'id':1},self.record())
        retake.assert_not_called();generate.assert_not_called()

    def test_pause_during_visual_review_prevents_next_gpu_submission(self):
        state={'state':'running'}
        def pause_in_review(*args):
            state['state']='paused'
            return []
        self.visual_check.side_effect=pause_in_review
        token=auto._executing.set(True)
        try:
            with patch.object(video_dialogue_quality,'check_native_speech',return_value=[]), \
                 patch.object(auto,'load',side_effect=lambda *args:state), \
                 patch.object(video_generation,'generate_for_user') as generate:
                auto.dispatch('videos',{'id':1},self.record())
                self.visual_check.assert_called_once()
                generate.assert_not_called()
        finally:auto._executing.reset(token)

    def test_mismatched_or_unavailable_speech_check_never_submits_next_shot(self):
        for error in ['speech mismatch','ASR unavailable']:
            with self.subTest(error=error), \
                 patch.object(video_dialogue_quality,'check_native_speech',side_effect=ValueError(error)), \
                 patch.object(video_generation,'generate_for_user') as generate:
                with self.assertRaisesRegex(ValueError,error):auto.dispatch('videos',{'id':1},self.record())
                generate.assert_not_called()

    def test_pause_during_recognition_prevents_following_gpu_submission(self):
        token=auto._executing.set(True)
        try:
            with patch.object(video_dialogue_quality,'check_native_speech',return_value=[]), \
                 patch.object(auto,'load',return_value={'state':'paused'}), \
                 patch.object(video_generation,'generate_for_user') as generate:
                auto.dispatch('videos',{'id':1},self.record());generate.assert_not_called()
        finally:auto._executing.reset(token)

    def test_existing_narration_retains_batch_behavior(self):
        with patch.object(video_dialogue_quality,'check_native_speech') as screen, \
             patch.object(video_generation,'generate_for_user') as generate:
            auto.dispatch('videos',{'id':1},self.record(False))
            screen.assert_not_called();self.assertEqual(generate.call_args.args[1].shot_ids,['shot_002','shot_003'])
            self.visual_check.assert_not_called()

    def test_mismatch_preserves_original_clip_without_retake_including_before_export(self):
        for action in ('videos', 'export'):
            record=self.record()
            if action=='export':
                for shot in record['shots']:shot['video_status']='completed'
            record['shots'][0].update(video='assets/original.mp4', video_task_id='accepted-original',
                                     video_request={'prompt':'approved'}, video_attempt=1)
            record['export']={'raw':'previous-export.mp4'}
            before=copy.deepcopy(record)
            failure=video_dialogue_quality.NativeSpeechReviewRequired('mismatch','shot_001',{'review':{}})
            with self.subTest(action=action), \
                 patch.object(video_dialogue_quality,'check_native_speech',side_effect=failure), \
                 patch('backend.app.video_native_speech_repair.retake') as retake, \
                 patch.object(video_generation,'generate_for_user') as generate, \
                 patch.object(video_export,'start_export_for_user') as export:
                with self.assertRaises(video_dialogue_quality.NativeSpeechReviewRequired) as raised:
                    auto.dispatch(action,{'id':1},record)
                self.assertIs(raised.exception,failure)
                self.assertEqual(record,before)
                retake.assert_not_called()
                generate.assert_not_called();export.assert_not_called()

    def test_export_screening_failure_and_pause_never_start_export(self):
        with patch.object(video_dialogue_quality,'check_native_speech',side_effect=ValueError('ASR unavailable')), \
             patch.object(video_export,'start_export_for_user') as export:
            with self.assertRaisesRegex(ValueError,'ASR unavailable'):
                auto.dispatch('export',{'id':1},self.record())
            export.assert_not_called()
        token=auto._executing.set(True)
        try:
            with patch.object(video_dialogue_quality,'check_native_speech',return_value=[]), \
                 patch.object(auto,'load',return_value={'state':'paused'}), \
                 patch.object(video_export,'start_export_for_user') as export:
                auto.dispatch('export',{'id':1},self.record());export.assert_not_called()
        finally:auto._executing.reset(token)

    def test_successful_screening_preserves_native_export_review_and_narration_route(self):
        for native in (True,False):
            with self.subTest(native=native), \
                 patch.object(video_dialogue_quality,'check_native_speech',return_value=[]) as screen, \
                 patch.object(video_export,'start_export_for_user') as export:
                auto.dispatch('export',{'id':1},self.record(native))
                self.assertEqual(screen.call_count,int(native))
                self.assertTrue(export.call_args.kwargs['review_generated'])

    def test_real_tick_persists_quality_pause_and_does_not_repeat_or_mutate_project(self):
        for stage in ('image_review', 'video_review', 'video_generation_ready'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as folder, \
                 patch.object(auto,'ROOT',Path(folder)/'runs'), \
                 patch.object(studio,'ROOT',Path(folder)/'videos'), \
                 patch.object(studio,'ACTIVE',set()), \
                 patch.object(studio,'project_has_image_edits',return_value=False), \
                 patch.object(db,'get_user_by_id',return_value={'id':1}), \
                 patch.object(auto,'require_native_voices'), \
                 patch('backend.app.video_native_image_repair.start_repair') as repair, \
                 patch('backend.app.video_native_speech_repair.retake') as retake, \
                 patch.object(video_generation,'generate_for_user') as generate, \
                 patch.object(video_export,'start_export_for_user') as export, \
                 patch.object(studio,'confirm_storyboard_images_for_user') as confirm, \
                 patch.object(video_dialogue_quality,'check_native_speech',side_effect=
                     video_dialogue_quality.NativeSpeechReviewRequired('shot_001: extra words','shot_001',{})) as speech:
                self.image_check.reset_mock()
                self.image_check.side_effect=(images.CoreImageReviewRequired('shot_002: wrong hand',
                    [{'review':{'issues':[{'shot_id':'shot_002'}]}}]) if stage=='image_review' else None)
                record=self.record();record.update(status=stage,native_owner_id=1)
                path=studio.directory(1,record['id']);path.mkdir(parents=True,exist_ok=True)
                for shot in record['shots']:
                    shot['image_status']='completed'
                    if stage=='video_generation_ready':shot['video_status']='completed'
                    if shot['video_status']=='completed':
                        shot.update(video=shot['id']+'.mp4',video_task_id='accepted-'+shot['id'],
                                    video_request={'prompt':'approved'},video_attempt=1)
                        (path/shot['video']).write_bytes(b'original media; recognition mocked')
                studio.save(path,record);auto.register_native(1,record)
                before={p.name:p.read_bytes() for p in path.iterdir() if p.is_file()}
                auto.tick(1,record['id']);auto.tick(1,record['id'])
                state=auto.load(1,record['id'])
                self.assertEqual(state['state'],'paused');self.assertIsNone(state['claim'])
                self.assertIn('wrong hand' if stage=='image_review' else 'extra words',state['message'])
                self.assertEqual(state['paused_project_revision'],3)
                self.assertEqual({p.name:p.read_bytes() for p in path.iterdir() if p.is_file()},before)
                for service in (repair,retake,generate,export,confirm):service.assert_not_called()
                self.assertEqual(speech.call_count,0 if stage=='image_review' else 1)
                self.assertEqual(self.image_check.call_count,0 if stage=='video_generation_ready' else 1)

    def test_real_tick_releases_project_lock_during_screening_and_preserves_manual_pause(self):
        entered,release=threading.Event(),threading.Event()
        def screen(*args):
            entered.set()
            if not release.wait(5):raise RuntimeError('test recognition timeout')
            return []
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(auto,'ROOT',Path(folder)/'runs'), \
             patch.object(studio,'ROOT',Path(folder)/'videos'), \
             patch.object(studio,'ACTIVE',set()), \
             patch.object(studio,'project_has_image_edits',return_value=False), \
             patch.object(db,'get_user_by_id',return_value={'id':1}), \
             patch.object(auto,'require_native_voices'), \
             patch.object(video_dialogue_quality,'check_native_speech',side_effect=screen), \
             patch.object(video_generation,'generate_for_user') as generate:
            record=self.record();record.update(status='video_review',native_owner_id=1)
            project_path=studio.directory(1,record['id']);project_path.mkdir(parents=True,exist_ok=True)
            (project_path/'clip.mp4').write_bytes(b'test media bytes; recognition is mocked')
            record['shots'][0]['video']='clip.mp4'
            studio.save(project_path,record);auto.register_native(1,record)
            worker=threading.Thread(target=auto.tick,args=(1,record['id']))
            worker.start()
            try:
                self.assertTrue(entered.wait(3),auto.load(1,record['id']))
                acquired=studio.LOCK.acquire(timeout=.5)
                self.assertTrue(acquired,'status/pause must not wait behind CPU ASR')
                try:
                    state=auto.load(1,record['id']);auto._pause(state,'User paused during recognition')
                finally:studio.LOCK.release()
            finally:
                release.set();worker.join(3)
            self.assertFalse(worker.is_alive());generate.assert_not_called()
            self.assertEqual(auto.load(1,record['id'])['state'],'paused')

    def test_status_preserves_current_stage_error_but_not_after_project_revision_changes(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(auto,'ROOT',Path(folder)/'runs'), \
             patch.object(studio,'ROOT',Path(folder)/'videos'), \
             patch.object(auto,'_owned_job',return_value=({'id':1},None)):
            record=self.record();record.update(status='video_review',native_owner_id=1)
            studio.save(studio.directory(1,record['id']),record)
            state=auto.register_native(1,record);state['claim']={'action':'videos','revision':3}
            auto._pause(state,'shot_001 speech needs review')
            app=FastAPI();app.include_router(auto.router);client=TestClient(app)
            self.assertEqual(client.get('/api/one-click/'+record['id']).json()['message'],'shot_001 speech needs review')
            record['revision']=4;studio.save(studio.directory(1,record['id']),record)
            self.assertNotEqual(client.get('/api/one-click/'+record['id']).json()['message'],'shot_001 speech needs review')

    def test_missing_voice_pause_reason_reaches_web_status_after_planning(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(auto,'ROOT',Path(folder)/'runs'), \
             patch.object(studio,'ROOT',Path(folder)/'videos'), \
             patch.object(db,'get_user_by_id',return_value={'id':1}), \
             patch.object(auto,'_owned_job',return_value=({'id':1},None)), \
             patch.object(auto,'require_native_voices',side_effect=ValueError('请为小兔、旁白选择固定音色')), \
             patch.object(auto,'dispatch') as dispatch:
            record=self.record();record.update(status='storyboard_review',native_owner_id=1,revision=5)
            studio.save(studio.directory(1,record['id']),record)
            auto.register_native(1,record)
            auto.tick(1,record['id'])
            app=FastAPI();app.include_router(auto.router);client=TestClient(app)
            state=client.get('/api/one-click/'+record['id']).json()
            self.assertEqual(state['state'],'paused')
            self.assertEqual(state['message'],'请为小兔、旁白选择固定音色')
            dispatch.assert_not_called()
