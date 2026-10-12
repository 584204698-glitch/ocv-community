import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from PIL import Image
from backend.app.video_core_references import resolve_reference, dependency_error, reference_prompt
from backend.app.video_core_reference_plan import reference_signature, plan_core_reference_groups
from tests import test_video_core_reference_plan as fixtures


class CoreReferenceRuntimeTests(unittest.TestCase):
    def test_reviewed_invisible_state_rebase_retains_source_pixels_but_rejects_independent_changes(self):
        from backend.app.video_dynamic_review import Advice,source_state_repairs,reconcile_state_references
        from tests.test_video_native_staging import NativeStagingTest
        fixture=NativeStagingTest();fixture.setUp()
        self.record['native_story']['text']='底部已经涂胶。'
        for shot in self.record['shots']:
            shot['native_dialogue']=copy.deepcopy(fixture.shot['native_dialogue'])
            row=shot['continuity_states'][0]
            row.update(name='台灯',ending=copy.deepcopy(row['opening']),visibility_basis='灯在画面中。',change_basis='')
        self.adopt()
        before=copy.deepcopy(self.record)
        advice=Advice.model_validate(dict(summary='只修正不可见底面条件。',warnings=[],
            shots=[dict(id=s['id'],reason='动作不变。',proposal=None) for s in before['shots']],
            state_corrections=[dict(prop_id='prop_lamp',state_id='table',source_quote='底部已经涂胶',
                reason='原文已有准备条件。',description='在桌面中央，底面已涂胶。',
                description_en='At the center of the table, with glue already on its underside.',
                visible_change=False,visual_reason='底面在桌面上不可见，颜色形状及位置不变。')]))
        targets=[s['id'] for s in before['shots']]
        current=copy.deepcopy(before)
        current['shots']=list(source_state_repairs(before,advice,targets,True).values())
        folder=self.path/'assets/dynamic_reviews';folder.mkdir(parents=True)
        (folder/'review.json').write_text(json.dumps(dict(basis=dict(record=before,targets=targets,
            repair_source_states=True),response=advice.model_dump())),encoding='utf8')
        self.assertTrue(dependency_error(self.path,current,current['shots'][1]))
        for mutate in (lambda r:r['shots'][1]['cast_plan']['characters'][0].update(identity_sha256='changed'),
                       lambda r:r['shots'][1].update(scene_reference_id='different')):
            bad=copy.deepcopy(current);mutate(bad)
            self.assertEqual(reconcile_state_references(self.path,bad),[])
            self.assertTrue(dependency_error(self.path,bad,bad['shots'][1]))
        updated=reconcile_state_references(self.path,current)
        self.assertEqual(updated,['1'])
        self.assertEqual(dependency_error(self.path,current,current['shots'][1]),'')
        self.assertEqual(current['shots'][1]['core_reference_used']['source_image_sha256'],
                         before['shots'][1]['core_reference_used']['source_image_sha256'])
        changed_pixels=copy.deepcopy(current)
        Image.new('RGB',(32,24),'red').save(self.path/'source.png')
        self.assertTrue(dependency_error(self.path,changed_pixels,changed_pixels['shots'][1]))
        self.assertEqual(reconcile_state_references(self.path,changed_pixels),[])

    def setUp(self):
        f=fixtures.CoreReferencePlanTests();f.setUp();self.record=f.record
        self.record['core_reference_policy']=1
        self.record.setdefault('creation_parameters', {})['quality_review_policy']='strict'
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.path=Path(temp.name)
        Image.new('RGB',(32,24),'blue').save(self.path/'source.png')
        self.source=self.record['shots'][0];self.target=self.record['shots'][1]
        self.source.update(image='source.png',image_status='completed',
            image_version=hashlib.sha256((self.path/'source.png').read_bytes()).hexdigest())
        self.review=dict(observations=[dict(shot_id='0',description='The expected blue lamp and cast are visible.')],
                         continuity_checks=[],issues=[],uncertainties=[])

    def adopt(self):
        result=resolve_reference(self.path,self.record,self.target,review=True,ask=Mock(return_value=self.review))
        self.target['core_reference_used']=result['binding']
        return result

    def test_resolve_requires_current_review_and_reuses_cache_without_model_call(self):
        with self.assertRaisesRegex(ValueError,'实图检查'):
            resolve_reference(self.path,self.record,self.target)
        first=self.adopt()
        ask=Mock(side_effect=AssertionError('No further model calls'))
        self.assertEqual(first,resolve_reference(self.path,self.record,self.target,review=True,ask=ask))
        ask.assert_not_called()
        restored=json.loads(json.dumps(self.record))
        self.assertEqual(dependency_error(self.path,restored,restored['shots'][1]),'')

    def test_source_pixel_version_change_blocks_only_bound_target(self):
        self.adopt()
        Image.new('RGB',(32,24),'red').save(self.path/'source.png')
        self.assertTrue(dependency_error(self.path,self.record,self.target))
        self.assertEqual(dependency_error(self.path,self.record,self.record['shots'][2]),'')
        self.source['image_version']=hashlib.sha256((self.path/'source.png').read_bytes()).hexdigest()
        self.assertIn('换版',dependency_error(self.path,self.record,self.target))
        with self.assertRaisesRegex(ValueError,'实图检查'):
            resolve_reference(self.path,self.record,self.target)

    def test_failed_or_uncertain_source_never_becomes_reference_and_never_retries(self):
        self.review['uncertainties']=['The lamp is obscured.']
        ask=Mock(return_value=self.review)
        for _ in range(2):
            with self.assertRaisesRegex(ValueError,'待核实'):
                resolve_reference(self.path,self.record,self.target,review=True,ask=ask)
        ask.assert_called_once()

    def test_stale_scene_or_unresolved_source_edit_prevents_even_a_review_request(self):
        self.source.update(scene_reference_used_version='old')
        ask=Mock(side_effect=AssertionError('No call'))
        with self.assertRaisesRegex(ValueError,'旧场景'):
            resolve_reference(self.path,self.record,self.target,review=True,ask=ask)
        self.source.pop('scene_reference_used_version')
        self.source['image_task']={'status':'failed'}
        with self.assertRaisesRegex(ValueError,'未解决'):
            resolve_reference(self.path,self.record,self.target,review=True,ask=ask)
        ask.assert_not_called()

    def test_changed_design_and_deleted_source_keep_lineage_and_block_acceptance(self):
        self.adopt();binding=copy.deepcopy(self.target['core_reference_used'])
        self.target['continuity_states'][0]['opening']['description']='different opening'
        self.assertTrue(dependency_error(self.path,self.record,self.target))
        self.assertEqual(binding,self.target['core_reference_used'])
        (self.path/'source.png').unlink()
        with self.assertRaises(Exception):resolve_reference(self.path,self.record,self.record['shots'][2])

    def test_policy_is_not_activated_by_opening_an_old_record(self):
        self.record.pop('core_reference_policy')
        with patch('backend.app.video_core_references.screen_group',side_effect=AssertionError('No check')):
            self.assertIsNone(resolve_reference(self.path,self.record,self.target,review=True))

    def test_appearance_binding_keeps_source_review_and_detects_pixel_replacement(self):
        self.target['continuity_states'][0]['opening']['description']='修补完成，灯仍未亮'
        self.target['cast_plan']['characters']=self.target['cast_plan']['characters'][:1]
        with self.assertRaisesRegex(ValueError,'实图检查'):
            resolve_reference(self.path,self.record,self.target)
        result=self.adopt()
        self.assertEqual(result['binding']['mode'],'appearance_only')
        restored=json.loads(json.dumps(self.record))
        self.assertEqual(dependency_error(self.path,restored,restored['shots'][1]),'')
        self.target['continuity_states'][0]['opening']['description']='本镜批准的新开场状态'
        self.assertEqual(dependency_error(self.path,self.record,self.target),'')
        Image.new('RGB',(32,24),'orange').save(self.path/'source.png')
        self.source['image_version']=hashlib.sha256((self.path/'source.png').read_bytes()).hexdigest()
        self.assertIn('换版',dependency_error(self.path,self.record,self.target))

    def test_appearance_mode_rejects_bad_review_and_design_scope_changes(self):
        self.target['continuity_states'][0]['opening']['description']='灯已修补'
        self.review['uncertainties']=['The prop appearance is obscured.']
        with self.assertRaisesRegex(ValueError,'待核实'):
            self.adopt()
        self.review['uncertainties']=[]
        # A changed report is a new review, rather than overwriting the failed cache.
        with patch('backend.app.video_core_references.screen_group',return_value=dict(
            review=self.review,image_versions={'0':self.source['image_version']},fingerprint='checked')):
            self.adopt()
        for change in ('style','ratio','scene','appearance','mode'):
            r=copy.deepcopy(self.record);target=r['shots'][1]
            if change in {'style','ratio'}:r.setdefault('settings',{})[change]='changed'
            if change=='scene':target['scene_reference_id']='another'
            if change=='appearance':target['continuity_props'][0]['appearance_en']='A patterned lamp.'
            if change=='mode':target['core_reference_used']['mode']='opening'
            with self.subTest(change=change):self.assertTrue(dependency_error(self.path,r,target))

    def test_mode_less_legacy_opening_source_remains_valid_after_new_fallback_changes_plan(self):
        source=self.record['shots'][1];target=self.record['shots'][2]
        for shot in (source,target):shot['continuity_states'][0]['opening']['description']='修补后的灯'
        source.update(image=self.source['image'],image_status='completed',image_version=self.source['image_version'])
        target['core_reference_used']=dict(version=1,source_shot_id=source['id'],
            source_image_sha256=source['image_version'],signature=reference_signature(self.record,target),
            review_fingerprint='legacy-check')
        self.assertEqual(plan_core_reference_groups(self.record)[2]['source_shot_id'],'0')
        self.assertEqual(dependency_error(self.path,self.record,target),'')
        source['continuity_states'][0]['opening']['description']='different'
        self.assertTrue(dependency_error(self.path,self.record,target))

    def test_appearance_prompt_keeps_original_action_and_voice_and_has_explicit_limits(self):
        prompt='批准动作：修补纸灯。原台词：我来试试。'
        result=reference_prompt(prompt,3,mode='appearance_only')
        self.assertTrue(result.startswith(prompt))
        for word in ('appearance_only','旧损坏','开关','占用','演员','机位','姿态','当前批准'):
            self.assertIn(word,result)
        self.assertEqual(reference_prompt(result,3,mode='appearance_only'),result)
        self.assertIn('同开场状态',reference_prompt(prompt,3))
