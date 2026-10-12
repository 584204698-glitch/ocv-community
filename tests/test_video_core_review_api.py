"""Normal report/repair routes using real caches/files and local render doubles."""
import copy,json,unittest
from unittest.mock import Mock,patch
from backend.app import video_studio as studio,video_native_image_quality as quality
from tests.test_video_core_reference_workflow import CoreReferenceWorkflowTests


class CoreReviewApiTests(unittest.TestCase):
    setUp=CoreReferenceWorkflowTests.setUp
    generate=CoreReferenceWorkflowTests.generate

    def completed(self):
        self.generate()
        # The recovery fixture seeds an unrelated unresolved legacy request;
        # this suite starts from an idle project and tests unknowns separately.
        self.f.old_receipt.write_text(json.dumps({'status':'completed','request_id':'fixture-resolved'}))
        real=quality.screen_group
        def answer(system,evidence,media):
            ids=evidence['image_order']
            return dict(observations=[dict(shot_id=i,description='Actual rendered frame inspected.') for i in ids],
                continuity_checks=[dict(from_shot_id=a,to_shot_id=b,camera='Same room, closer view.',
                    characters=dict(status='consistent',evidence='The same characters remain.'),
                    setting=dict(status='consistent',evidence='The same table remains.')) for a,b in zip(ids,ids[1:])],
                issues=[dict(shot_id='core1',code='opening_state',evidence='The lamp is missing.',
                    correction='Restore the approved blue lamp on the table.')] if 'core1' in ids else [],uncertainties=[])
        self.review_ask=Mock(side_effect=answer)
        self.f.stack.enter_context(patch.object(quality,'screen_group',side_effect=lambda p,shots,**kw:
            real(p,shots,ask=self.review_ask,**kw)))

    def check(self):
        return self.f.client.post(self.f.base+'/images/check',json={'revision':self.f.read()['revision']})

    def repair(self,report,**extra):
        return self.f.client.post(self.f.base+'/images/repair-from-review',json={
            'revision':self.f.read()['revision'],'shot_id':'core1','fingerprints':report['fingerprints'],**extra})

    def test_check_reads_pixels_without_mutation_and_reuses_actual_cache(self):
        self.completed();before=self.f.files()
        first=self.check();self.assertEqual(first.status_code,200,first.text)
        self.assertEqual(first.json()['reviewed_ids'],['core0','core1','core2'])
        second=self.check();self.assertEqual(first.json(),second.json())
        self.review_ask.assert_called_once()
        self.assertEqual((self.f.path/'record.json').read_bytes(),before['record.json'])
        for file,data in before.items():self.assertEqual((self.f.path/file).read_bytes(),data)
        self.assertEqual(self.f.renderer.call_count,3)

    def test_explicit_repair_cannot_adapt_story_and_changes_only_selected_image(self):
        self.completed();report=self.check().json();before=self.f.read()
        with patch('backend.app.video_native_handedness.adapt',side_effect=AssertionError('No story adaptation')):
            response=self.repair(report)
        self.assertEqual(response.status_code,200,response.text)
        duplicate=self.repair(report);self.assertEqual(duplicate.status_code,409)
        self.f.run_worker();after=self.f.read()
        for index in (0,2):self.assertEqual(after['shots'][index],before['shots'][index])
        target=after['shots'][1];old=before['shots'][1]
        for key in ('action','visual_description','native_dialogue','cast_plan','continuity_states'):
            self.assertEqual(target.get(key),old.get(key))
        self.assertEqual(len(target['image_history']),len(old.get('image_history',[]))+1)
        self.assertEqual(self.f.renderer.call_count,4)

    def test_stale_pixels_and_untrusted_fingerprints_never_repair(self):
        self.completed();report=self.check().json()
        self.assertEqual(self.repair(report,fingerprints=['a'*64]).status_code,409)
        record=self.f.read();self.f.write_image(record['shots'][1]['image'],'orange')
        self.assertEqual(self.repair(report).status_code,409)
        self.assertEqual(self.f.renderer.call_count,3)
        self.review_ask.assert_called_once()

    def test_unknown_receipt_blocks_new_check_and_repair(self):
        self.completed();report=self.check().json();record=self.f.read()
        p=self.f.path/'assets/unresolved.openai-image.json'
        p.write_text(json.dumps(dict(status='unknown',attempt_id='existing-paid-request')))
        before=p.read_bytes()
        self.assertEqual(self.check().status_code,409)
        self.assertEqual(self.repair(report).status_code,409)
        self.assertEqual(p.read_bytes(),before)
        self.assertEqual(self.f.read(),record)
        self.review_ask.assert_called_once()

    def test_partial_check_does_not_invent_adjacency_across_missing_image(self):
        self.completed();r=self.f.read();r['shots'][1]['image_status']='pending';studio.save(self.f.path,r)
        response=self.check();self.assertEqual(response.status_code,200,response.text)
        data=response.json()
        self.assertEqual([r['image_order'] for r in data['reports']],[['core0'],['core2']])
        self.assertEqual(data['unchecked_ids'],['core1'])
        self.assertFalse(data['quality_guaranteed'])

    def test_changed_design_with_identical_pixels_rejects_cached_repair(self):
        self.completed();report=self.check().json();r=self.f.read()
        r['shots'][1]['image_prompt']='A different approved opening.';studio.save(self.f.path,r)
        self.assertEqual(self.repair(report).status_code,409)
        self.assertEqual(self.f.renderer.call_count,3)
        self.review_ask.assert_called_once()

    def test_concurrent_check_is_rejected_and_failed_check_releases_claim(self):
        self.completed()
        def fail(*args):
            self.assertEqual(self.check().status_code,409)
            raise ValueError('Provider result invalid')
        self.review_ask.side_effect=fail
        response=self.check();self.assertEqual(response.status_code,400)
        self.assertFalse(studio.project_has_image_edits(self.f.identity))
        self.review_ask.assert_called_once()

    def test_provider_failure_returns_actionable_error_without_retry_or_mutation(self):
        self.completed();before=self.f.files()
        self.review_ask.side_effect=RuntimeError('private provider diagnostics')
        response=self.check()
        self.assertEqual(response.status_code,502)
        self.assertIn('未提交修图',response.json()['detail'])
        self.assertNotIn('private provider diagnostics',response.text)
        self.assertFalse(studio.project_has_image_edits(self.f.identity))
        self.review_ask.assert_called_once()
        self.assertEqual(self.f.files(),before)
        self.assertEqual(self.f.renderer.call_count,3)

    def test_normal_get_refreshes_scene_report_scope_without_writes_or_model_calls(self):
        self.completed()
        from backend.app.video_scene_quality import review_design_fingerprint
        record=self.f.read();asset=record['scene_assets'][0]
        asset['used_by']=[s['id'] for s in record['shots']]
        asset['quality_review']={'image_version':asset.get('image_version'),
            'design_fingerprint':review_design_fingerprint(record,asset),'issues':[],'uncertainties':[]}
        studio.save(self.f.path,record)
        raw=(self.f.path/'record.json').read_bytes()
        response=self.f.client.get(self.f.base)
        self.assertEqual(response.status_code,200,response.text)
        current=response.json()['scene_assets'][0]
        self.assertEqual(current['review_design_fingerprint'],current['quality_review']['design_fingerprint'])
        self.assertEqual((self.f.path/'record.json').read_bytes(),raw)
        record['shots'][0]['action']='Walk through a new doorway on the other wall.'
        studio.save(self.f.path,record);raw=(self.f.path/'record.json').read_bytes()
        current=self.f.client.get(self.f.base).json()['scene_assets'][0]
        self.assertNotEqual(current['review_design_fingerprint'],current['quality_review']['design_fingerprint'])
        self.assertEqual(current['quality_review'],asset['quality_review'])
        self.assertEqual((self.f.path/'record.json').read_bytes(),raw)
        self.review_ask.assert_not_called()
