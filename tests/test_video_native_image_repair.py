import copy
import hashlib
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
import unittest

from fastapi import HTTPException
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app import video_native_image_quality as quality, video_native_image_repair as repair
from backend.app import db, one_click, video_studio as studio
from tests import test_video_image_recovery as fixture


class NativeImageRepairTests(unittest.TestCase):
    write_image = fixture.VideoImageRecoveryTest.write_image
    file_hash = fixture.VideoImageRecoveryTest.file_hash
    shot = fixture.VideoImageRecoveryTest.shot
    read = fixture.VideoImageRecoveryTest.read
    files = fixture.VideoImageRecoveryTest.files
    render = fixture.VideoImageRecoveryTest.render
    run_worker = fixture.VideoImageRecoveryTest.run_worker

    def setUp(self):
        fixture.VideoImageRecoveryTest.setUp(self)
        record = self.read()
        record['native_story'] = {'text': 'A returns a borrowed instrument.'}
        record['shots'][0] = self.shot('failed-A', 'scene-A')
        for shot in record['shots']:
            shot['visual_description'] = 'The instrument rests untouched on the table.'
        studio.save(self.path, record)

    def report(self, target='dependent-B', code='opening_state'):
        record = self.read()
        ids = [s['id'] for s in record['shots']]
        return dict(fingerprint='review-fingerprint',image_order=ids,
            image_versions={s['id']:self.file_hash(s['image']) for s in record['shots']},
            review=dict(observations=[dict(shot_id=i,description='An instrument on a table.') for i in ids],
                issues=[dict(shot_id=target,code=code,evidence='The instrument is incorrectly held in a hand.')],
                uncertainties=[]))

    def start(self, report=None):
        return repair.start_repair(self.identity,self.read()['revision'],{'id':1},[report or self.report()])

    def test_existing_worker_archives_only_bad_frame_and_keeps_the_design(self):
        before = self.read()
        snapshot = self.files()
        old_shot = before['shots'][1]
        old_pixels = (self.path / old_shot['image']).read_bytes()
        updated = self.start()
        self.assertEqual(one_click.next_action(updated),(None,''))
        self.assertEqual(len(self.workers),1)
        self.run_worker()
        after = self.read()
        self.assertEqual(after['shots'][0],before['shots'][0])
        self.assertEqual(after['shots'][2],before['shots'][2])
        self.assertEqual(after['scene_assets'],before['scene_assets'])
        shot = after['shots'][1]
        self.assertEqual(shot['image_prompt'],old_shot['image_prompt'])
        self.assertEqual(shot['image_task']['action'],'auto_repair')
        self.assertEqual(shot['video_status'],'pending')
        self.assertEqual((self.path/shot['image_history'][0]['image']).read_bytes(),old_pixels)
        frozen = shot['native_image_repairs'][0]['references'][0]['snapshot']
        self.assertEqual((self.path/frozen).read_bytes(),old_pixels)
        self.assertNotEqual((self.path/shot['image']).read_bytes(),old_pixels)
        self.assertEqual(one_click.next_action(after),('confirm_images',''))
        # Successful edit is never an image/video acceptance flag.
        self.assertEqual(after['status'],'image_review')
        for name,data in snapshot.items():
            if name not in {'record.json',old_shot['image']}:
                self.assertEqual((self.path/name).read_bytes(),data,name)

    def test_one_click_repairs_once_then_rechecks_and_pauses_without_rebuild(self):
        record=self.read();record.update(status='image_review',native_owner_id=1)
        record.setdefault('creation_parameters',{})['quality_review_policy']='strict'
        for key in list(record['shots'][1]):
            if key == 'video' or key.startswith('video_'):
                record['shots'][1].pop(key)
        record['shots'][1]['video_status']='pending'
        studio.save(self.path,record)
        def screen(*args,**kwargs):
            report=self.report()
            report['review']['issues'][0]['correction']='Restore the approved medium shot framing.'
            raise quality.CoreImageReviewRequired('wrong framing',[report])
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(one_click,'ROOT',Path(folder)/'runs'), \
             patch.object(db,'get_user_by_id',return_value={'id':1}), \
             patch.object(one_click,'require_native_voices'), \
             patch.object(quality,'check_native_core_images',side_effect=screen) as check, \
             patch('backend.app.video_native_handedness.adapt') as adapt, \
             patch.object(repair,'_rebuild_inputs') as rebuild, \
             patch.object(studio,'confirm_storyboard_images_for_user') as confirm:
            one_click.register_native(1,record)
            one_click.tick(1,self.identity)
            self.assertEqual(len(self.workers),1,one_click.load(1,self.identity))
            self.assertEqual(one_click.load(1,self.identity)['state'],'running')
            self.assertEqual(len(self.read()['shots'][1]['native_image_repairs']),1)
            one_click.tick(1,self.identity)
            self.assertEqual(check.call_count,1)
            self.run_worker()
            prompt=self.renderer.call_args.args[0]['image_prompt']
            self.assertIn('已正确的构图',prompt)
            self.assertIn('不保留错误构图',prompt)
            self.assertIn('Restore the approved medium shot framing.',prompt)
            one_click.tick(1,self.identity)
            state=one_click.load(1,self.identity)
            self.assertEqual(state['state'],'paused')
            self.assertIn('wrong framing',state['message'])
            self.assertIsNone(state['claim'])
            one_click.tick(1,self.identity)
            self.assertEqual(check.call_count,2)
            self.assertEqual(self.renderer.call_count,1)
            self.assertEqual(len(self.read()['shots'][1]['native_image_repairs']),1)
            adapt.assert_not_called();rebuild.assert_not_called();confirm.assert_not_called()

    def test_concrete_edit_survives_to_actual_request_and_preserves_old_completed_clips(self):
        record=self.read();record['status']='video_review'
        record['shots'][1]['native_image_repairs']=[dict(issues=[{'evidence':'old failed correction'}])]
        studio.save(self.path,record)
        report=self.report()
        report['review']['issues'][0]['correction']='把乐器放回桌面，人物双手离开乐器；保持房间和其他人物不变。'
        before=copy.deepcopy(record)
        self.start(report);self.run_worker()
        macro=self.renderer.call_args.args[0]
        self.assertIn('【本次必须执行的具体修改】',macro['image_prompt'])
        self.assertIn(report['review']['issues'][0]['correction'],macro['image_prompt'])
        after=self.read()
        self.assertEqual(after['status'],'image_review')
        self.assertEqual(after['shots'][0],before['shots'][0])
        self.assertEqual(after['shots'][2],before['shots'][2])
        self.assertEqual(len(after['shots'][1]['native_image_repairs']),2)
        with self.assertRaisesRegex(ValueError,'已经自动修复过一次'):self.start(self.report())

    def failed_repair_with_receipt(self):
        def fail(macro, pool):
            output = Path(macro['_output_path'])
            self.repair_receipt = output.with_name(output.name + '.openai-image.json')
            self.repair_receipt.write_text(json.dumps({'status': 'unknown', 'request': {
                'source_prompt_sha256': hashlib.sha256(macro['image_prompt'].encode()).hexdigest(),
                'reference_sha256': [hashlib.sha256(Path(p).read_bytes()).hexdigest()
                                     for p in macro['reference_image_paths']]}}))
            raise RuntimeError('Transport ended before complete body')
        self.renderer.side_effect = fail
        self.start()
        self.run_worker()
        self.renderer.side_effect = self.render

    def retry_failed_repair(self, confirmed=True):
        return self.client.post(self.base + '/images/dependent-B/retry-repair', json={
            'revision': self.read()['revision'], 'confirm_new_attempt': confirmed})

    def test_explicit_repair_retry_uses_exact_frozen_inputs_and_keeps_receipt(self):
        self.failed_repair_with_receipt()
        before = self.read()
        receipt_bytes = self.repair_receipt.read_bytes()
        original_macro = copy.deepcopy(self.renderer.call_args.args[0])
        self.assertEqual(self.retry_failed_repair().status_code, 200)
        self.run_worker()
        macro = self.renderer.call_args.args[0]
        self.assertEqual(macro['image_prompt'], original_macro['image_prompt'])
        self.assertEqual(macro['reference_image_paths'], original_macro['reference_image_paths'])
        self.assertNotEqual(macro['_output_path'], original_macro['_output_path'])
        after = self.read()
        shot = after['shots'][1]
        self.assertEqual(shot['image_prompt'], before['shots'][1]['image_prompt'])
        self.assertEqual(shot['image_task']['status'], 'completed')
        self.assertEqual(len(shot['native_image_repairs']), 1)
        self.assertEqual(len(shot['native_image_repairs'][0]['manual_retries']), 1)
        self.assertEqual(self.repair_receipt.read_bytes(), receipt_bytes)
        self.assertEqual(after['shots'][0], before['shots'][0])
        self.assertEqual(after['shots'][2], before['shots'][2])
        self.assertEqual(after['scene_assets'], before['scene_assets'])
        with self.assertRaisesRegex(ValueError, '已经自动修复过一次'):
            self.start()

    def test_still_editor_receives_only_opening_not_later_action_or_prop_result(self):
        record=self.read();shot=record['shots'][1]
        shot['action']='FUTURE_ACTION_SENTINEL'
        shot['continuity_states']=[dict(prop_id='p',name='instrument',
            opening={'description':'Instrument rests on table.'},
            ending={'description':'FUTURE_RESULT_SENTINEL'},
            visibility='opening',visibility_basis='On table.',change_basis='FUTURE_CHANGE_SENTINEL')]
        studio.save(self.path,record)
        self.start();self.run_worker()
        prompt=self.renderer.call_args.args[0]['image_prompt']
        self.assertIn('Instrument rests on table.',prompt)
        for future in ('FUTURE_ACTION_SENTINEL','FUTURE_RESULT_SENTINEL','FUTURE_CHANGE_SENTINEL'):
            self.assertNotIn(future,prompt)
        self.assertTrue(self.read()['shots'][1]['native_image_repairs'][0]['design_sha256'])

    def test_previous_protocol_failed_request_remains_exactly_retryable(self):
        for revision in ('concrete-correction-v2','static-opening-edit-v3'):
            with self.subTest(revision=revision):
                record=self.read();record['shots'][1].pop('native_image_repairs',None)
                studio.save(self.path,record)
                with patch.object(repair,'REPAIR_REVISION',revision):
                    self.failed_repair_with_receipt()
                original_macro=copy.deepcopy(self.renderer.call_args.args[0])
                self.assertEqual(self.retry_failed_repair().status_code,200)
                self.run_worker()
                self.assertEqual(self.renderer.call_args.args[0]['image_prompt'],original_macro['image_prompt'])
                self.assertEqual(self.renderer.call_args.args[0]['reference_image_paths'],original_macro['reference_image_paths'])

    def test_failed_completed_edit_can_rebuild_once_without_wrong_frame_or_other_shots(self):
        report=self.report()
        report['review']['issues'][0]['correction']='Place the instrument on the table, with both hands separated from it.'
        self.start(report);self.run_worker()
        before=self.read()
        corrected=self.report();corrected['review']['issues'][0]['correction']=report['review']['issues'][0]['correction']
        self.start(corrected);self.run_worker()
        after=self.read();shot=after['shots'][1]
        self.assertEqual(shot['native_image_repairs'][-1]['strategy'],repair.REBUILD_STRATEGY)
        self.assertEqual(shot['native_image_repairs'][-1]['references'][0]['file'],after['scene_assets'][0]['image'])
        self.assertEqual(after['shots'][0],before['shots'][0])
        self.assertEqual(after['shots'][2],before['shots'][2])
        self.assertEqual(len(shot['image_history']),2)
        self.assertEqual(after['status'],'image_review')
        # Rebuilding can resolve the first defect but leave a local detail.
        # Recheck actual new pixels, then allow one final edit, never a loop.
        final=self.report();final['review']['issues'][0]['correction']='Correct only the remaining visible contact at the instrument.'
        self.start(final);self.run_worker()
        final_record=self.read();last=final_record['shots'][1]
        self.assertEqual(last['native_image_repairs'][-1]['strategy'],repair.FINAL_EDIT_STRATEGY)
        self.assertEqual(final_record['shots'][0],after['shots'][0])
        self.assertEqual(final_record['shots'][2],after['shots'][2])
        self.assertEqual(len(last['image_history']),3)
        self.assertEqual(final_record['status'],'image_review')
        with self.assertRaisesRegex(ValueError,'三次辅助修复上限'):self.start(self.report())
        self.assertEqual(self.renderer.call_count,3)

    def test_rebuild_rejects_wrong_frame_in_reference_selection_and_stale_scene(self):
        record=self.read();record['shots'][1]['native_image_repairs']=[{'repair_revision':repair.REPAIR_REVISION}]
        studio.save(self.path,record)
        report=self.report();report['review']['issues'][0]['correction']='Restore the approved table opening state.'
        with patch.object(studio,'_image_inputs',return_value=('prompt',[str(self.path/record['shots'][1]['image'])],None)):
            with self.assertRaisesRegex(ValueError,'错误核心图'):self.start(report)
        self.write_image(record['scene_assets'][0]['image'],'orange')
        with self.assertRaisesRegex(ValueError,'场景资产版本'):self.start(report)
        self.renderer.assert_not_called()

    def test_unknown_rebuild_request_requires_explicit_retry_with_frozen_assets(self):
        record=self.read();record['shots'][1]['native_image_repairs']=[{'repair_revision':repair.REPAIR_REVISION}]
        studio.save(self.path,record)
        original_report=self.report
        def concrete_report(*args,**kwargs):
            result=original_report(*args,**kwargs)
            result['review']['issues'][0]['correction']='Place the instrument back on the table.'
            return result
        with patch.object(self,'report',side_effect=concrete_report):self.failed_repair_with_receipt()
        macro=copy.deepcopy(self.renderer.call_args.args[0])
        with self.assertRaisesRegex(ValueError,'上次编辑失败'):self.start()
        self.assertEqual(self.retry_failed_repair().status_code,200)
        self.run_worker()
        self.assertEqual(self.renderer.call_args.args[0]['image_prompt'],macro['image_prompt'])
        self.assertEqual(self.renderer.call_args.args[0]['reference_image_paths'],macro['reference_image_paths'])

    def test_repair_retry_requires_confirmation_without_mutation(self):
        self.failed_repair_with_receipt()
        before = self.files()
        calls = self.renderer.call_count
        self.assertEqual(self.retry_failed_repair(False).status_code, 409)
        self.assertEqual(self.files(), before)
        self.assertEqual(self.renderer.call_count, calls)
        self.assertEqual(self.workers, [])

    def test_repair_retry_rejects_changed_design_and_frozen_reference(self):
        self.failed_repair_with_receipt()
        record = self.read()
        record['shots'][1]['action'] = 'A different unapproved action'
        studio.save(self.path, record)
        before = self.files()
        self.assertEqual(self.retry_failed_repair().status_code, 409)
        self.assertEqual(self.files(), before)
        record['shots'][1]['action'] = 'A enters.'
        reference = record['shots'][1]['native_image_repairs'][0]['references'][0]
        studio.save(self.path, record)
        self.write_image(reference['snapshot'], 'orange')
        before = self.files()
        self.assertEqual(self.retry_failed_repair().status_code, 409)
        self.assertEqual(self.files(), before)
        self.assertEqual(self.workers, [])

    def test_repair_retry_rejects_missing_receipt(self):
        self.failed_repair_with_receipt()
        self.repair_receipt.unlink()
        before = self.files()
        self.assertEqual(self.retry_failed_repair().status_code, 409)
        self.assertEqual(self.files(), before)

    def test_restart_retains_auto_repair_action(self):
        self.failed_repair_with_receipt()
        record = self.read()
        record['shots'][1]['image_task']['status'] = 'running'
        studio.save(self.path, record)
        recovered = studio.read(self.path)['shots'][1]
        self.assertEqual(recovered['image_task']['action'], 'auto_repair')
        self.assertEqual(recovered['image_task']['status'], 'failed')

    def test_uncertain_transport_failure_is_not_reissued_or_hidden(self):
        self.renderer.side_effect = RuntimeError('Provider outcome unknown')
        before = self.read()
        self.start()
        self.run_worker()
        after = self.read()
        self.assertEqual(after['shots'][1]['image_version'],before['shots'][1]['image_version'])
        self.assertEqual(after['shots'][1]['image_task']['status'],'failed')
        self.assertIn('未自动重发',one_click.next_action(after)[1])
        with self.assertRaisesRegex(ValueError,'上次编辑失败'):
            self.start()
        self.assertEqual(self.renderer.call_count,1)
        self.assertEqual(self.workers,[])

    def test_stale_evidence_and_foreign_owner_submit_nothing(self):
        report = self.report()
        shot = self.read()['shots'][1]
        self.write_image(shot['image'],'blue')
        with self.assertRaisesRegex(ValueError,'已变化'):
            self.start(report)
        with self.assertRaises(HTTPException):
            repair.start_repair(self.identity,7,{'id':2},[report])
        self.assertEqual(self.workers,[])
        self.renderer.assert_not_called()

    def test_one_attempt_per_shot_does_not_strand_other_unrepaired_shots(self):
        self.start();self.run_worker()
        with self.assertRaisesRegex(ValueError,'已经自动修复过一次'):
            self.start()
        record = self.read()
        record['shots'][0]['native_image_repairs']=[{},{}]
        studio.save(self.path,record)
        self.start(self.report('independent-C'));self.run_worker()
        self.assertEqual(self.renderer.call_count,2)
        with self.assertRaisesRegex(ValueError,'已经自动修复过一次'):
            self.start(self.report('independent-C'))

    def test_continuity_uses_only_same_scene_predecessor_and_freezes_both(self):
        report = self.report(code='continuity')
        self.start(report)
        record = self.read()
        refs = record['shots'][1]['native_image_repairs'][0]['references']
        self.assertEqual(len(refs),2)
        self.assertEqual(refs[1]['file'],record['shots'][0]['image'])
        self.run_worker()
        # A scene cut may not pull in the previous setting.
        _,_,paths,_=repair.prepare_repair(self.path,self.read(),[self.report('independent-C','continuity')])
        self.assertEqual(len(paths),1)

    def test_user_pause_wins_after_review_before_request(self):
        token = one_click._executing.set(True)
        try:
            with patch.object(one_click,'load',return_value={'state':'paused'}):
                self.start()
            self.assertEqual(self.workers,[])
            self.assertEqual(self.read()['revision'],7)
        finally:
            one_click._executing.reset(token)

    def test_failure_before_dispatch_keeps_specific_reason_in_status_endpoint(self):
        record=self.read();record['native_owner_id']=1
        record['shots'][1]['image_status']='failed'
        studio.save(self.path,record)
        with patch.object(one_click,'ROOT',self.root/'runs'), \
             patch('backend.app.db.get_user_by_id',return_value={'id':1}), \
             patch.object(one_click,'_owned_job',return_value=({'id':1},None)):
            one_click.register_native(1,record)
            one_click.tick(1,self.identity)
            state=one_click.load(1,self.identity)
            self.assertEqual(state['paused_project_revision'],7)
            app=FastAPI();app.include_router(one_click.router)
            with TestClient(app) as client:
                message=client.get('/api/one-click/'+self.identity).json()['message']
            self.assertIn('部分核心图未完成',message)
        self.renderer.assert_not_called()

    def test_normal_dispatch_reports_image_defect_without_starting_repair(self):
        record = self.read()
        record['creation_parameters']['quality_review_policy']='strict'
        # This is first-pass image preparation, before a video was submitted.
        for key in list(record['shots'][1]):
            if key == 'video' or key.startswith('video_'):
                record['shots'][1].pop(key)
        studio.save(self.path,record)
        report = self.report()
        error = quality.CoreImageReviewRequired('Image requires correction',[report])
        before={p.relative_to(self.path):p.read_bytes() for p in self.path.rglob('*') if p.is_file()}
        with patch.object(quality,'check_native_core_images',side_effect=error), \
             patch.object(studio,'confirm_storyboard_images_for_user') as confirm:
            with self.assertRaises(quality.CoreImageReviewRequired):
                one_click.dispatch('confirm_images',{'id':1},record)
            confirm.assert_not_called()
        self.assertEqual(self.workers,[])
        self.renderer.assert_not_called()
        self.assertEqual({p.relative_to(self.path):p.read_bytes() for p in self.path.rglob('*') if p.is_file()},before)

    def test_identity_repair_uses_all_opening_identities_without_label_reuse(self):
        record=self.read()
        characters=[]
        participants=[]
        for index in range(3):
            identity='pc_'+str(index)*32
            reference=f'ref-{index}'
            file=f'assets/identity-{index}.jpg'
            self.write_image(file,'yellow')
            record['references'].append(dict(id=reference,file=file))
            characters.append(dict(project_character_id=identity,reference_id=reference,name=f'Role {index}',constraints='Keeps the approved coat.'))
            participants.append(dict(project_character_id=identity,presence='opening'))
        record['shots'][1]['cast_plan']={'characters':participants}
        studio.save(self.path,record)
        with patch('backend.app.video_cast_planning.saved_planning_cast',return_value={'characters':characters}):
            _,prompt,paths,_=repair.prepare_repair(self.path,record,[self.report(code='cast')])
        self.assertEqual(len(paths),4)
        self.assertIn('图1是需要局部修复',prompt)
        for index in range(3):self.assertIn(f'图{index+2}只约束开场角色“Role {index}”',prompt)


if __name__ == '__main__':unittest.main()
