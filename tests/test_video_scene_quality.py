import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image
from backend.app.video_scene_quality import check_native_scene_images, prepare_scene_repair, blocking_scene_uncertainties, refresh_review_notices


class SceneQualityTests(unittest.TestCase):
    def setUp(self):
        observer=patch('backend.app.video_scene_quality.observe_scene',return_value={
            'image_sha256':'observed','observation':{'objects':[],'appearance':'white image','uncertainties':[]}})
        self.observer=observer.start();self.addCleanup(observer.stop)
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.path=Path(temp.name);Image.new('RGB',(32,24),'white').save(self.path/'scene.jpg')
        self.record={'native_story':{'text':'Visitors enter a library.'},'settings':{'style':'水彩'},
            'scene_assets':[{'id':'room','asset_kind':'scene_reference','name':'Library','image':'scene.jpg','image_status':'completed',
                             'image_prompt':'原有图书馆门牌。','used_by':['one','two']}],
            'shots':[{'id':identity,'scene_reference_id':'room','image_prompt':'门牌写有“图书馆”。'}
                     for identity in ['one','two']]}
        self.review={'observations':[{'shot_id':'room','description':'门牌是英文 Library。'}],
            'continuity_checks':[], 'issues':[{'shot_id':'room','code':'continuity',
                'evidence':'批准要求图书馆，实际是英文 Library。','correction':'仅将门牌文字改为图书馆。'}],
            'uncertainties':[]}

    def test_failed_real_asset_review_is_cached_and_original_requirements_are_forwarded(self):
        asks=Mock(return_value=self.review);original=(self.path/'scene.jpg').read_bytes()
        for _ in range(2):
            with self.assertRaisesRegex(ValueError,'Library'):
                check_native_scene_images(self.path,self.record,ask=asks)
        self.assertEqual(asks.call_count,1)
        system,evidence,images=asks.call_args.args
        self.assertIn('图书馆',evidence['context']['approved_members'][0]['image_prompt'])
        self.assertEqual(evidence['context']['style'],'水彩')
        self.assertEqual(len(images),1)
        self.assertEqual((self.path/'scene.jpg').read_bytes(),original)
        self.assertFalse(self.record['scene_assets'][0]['quality_review']['quality_guaranteed'])

    def test_changed_approved_text_invalidates_cache_but_quality_metadata_does_not(self):
        review=copy.deepcopy(self.review);review['issues']=[]
        asks=Mock(return_value=review)
        check_native_scene_images(self.path,self.record,ask=asks)
        check_native_scene_images(self.path,self.record,ask=asks)
        self.assertEqual(asks.call_count,1)
        self.record['shots'][0]['image_prompt']='门牌写有“阅览室”。'
        check_native_scene_images(self.path,self.record,ask=asks)
        self.assertEqual(asks.call_count,2)

    def test_design_changes_make_ui_report_stale_without_model_call_or_erasing_evidence(self):
        review=copy.deepcopy(self.review);review['issues']=[]
        asks=Mock(return_value=review)
        check_native_scene_images(self.path,self.record,ask=asks)
        asset=self.record['scene_assets'][0]
        original=copy.deepcopy(asset['quality_review'])
        self.record['revision']=900
        self.record['shots'][0]['image_version']='unrelated core pixels'
        refresh_review_notices(self.record)
        self.assertEqual(asset['review_design_fingerprint'],original['design_fingerprint'])
        self.record['shots'][0]['image_prompt']='门牌写有“阅览室”。'
        refresh_review_notices(self.record)
        self.assertNotEqual(asset['review_design_fingerprint'],original['design_fingerprint'])
        self.assertEqual(asset['quality_review'],original)
        asks.assert_called_once()

    def test_uncertain_observation_stops_and_legacy_project_does_not_gain_paid_check(self):
        review=copy.deepcopy(self.review);review.update(issues=[],uncertainties=['门牌看不清。'])
        asks=Mock(return_value=review)
        with self.assertRaisesRegex(ValueError,'看不清'):
            check_native_scene_images(self.path,self.record,ask=asks)
        self.record.pop('native_story');asks.reset_mock()
        check_native_scene_images(self.path,self.record,ask=asks)
        asks.assert_not_called()

    def test_single_scene_notes_preserve_a_failure_but_cannot_become_a_pass(self):
        self.review['continuity_checks']=['固定通路可用，没有相邻镜头可比。']
        with self.assertRaisesRegex(ValueError,'Library'):
            check_native_scene_images(self.path,self.record,ask=Mock(return_value=self.review))
        prompt,display=prepare_scene_repair(self.path,self.record,self.record['scene_assets'][0])
        self.assertIn('仅将门牌文字改为图书馆',prompt)
        self.assertIn('图1',prompt)
        self.assertIn(self.record['settings']['style'],prompt)
        self.assertIn('图书馆',display)
        self.record['shots'][0]['image_prompt']='门牌写有“阅览室”。'
        with self.assertRaisesRegex(ValueError,'旧报告'):
            prepare_scene_repair(self.path,self.record,self.record['scene_assets'][0])
        self.review['issues']=[]
        self.review['observations']=[]
        with self.assertRaisesRegex(ValueError,'逐张实际观察'):
            check_native_scene_images(self.path,self.record,ask=Mock(return_value=self.review))

    def test_previous_failed_repair_is_not_automatically_resubmitted(self):
        with self.assertRaises(ValueError):
            check_native_scene_images(self.path,self.record,ask=Mock(return_value=self.review))
        self.record['scene_assets'][0]['image_task']={'status':'failed'}
        with self.assertRaisesRegex(ValueError,'原请求'):
            prepare_scene_repair(self.path,self.record,self.record['scene_assets'][0])

    def test_valid_observation_with_no_issues_does_not_fail_only_for_extra_notes(self):
        self.review.update(issues=[],uncertainties=[],continuity_checks=['可见文字与批准要求一致。'],
                           observations=[dict(shot_id='room',description='门牌为批准的中文图书馆。')])
        check_native_scene_images(self.path,self.record,ask=Mock(return_value=self.review))
        self.assertFalse(self.record['scene_assets'][0]['quality_review']['quality_guaranteed'])

    def test_single_scene_notes_preserve_uncertainty_without_enabling_repair_or_pass(self):
        self.review.update(issues=[],uncertainties=['画面边缘家具用途无法确认。'],
                           continuity_checks=['已核对文字与可见通路。'])
        asks=Mock(return_value=self.review)
        for _ in range(2):
            with self.assertRaisesRegex(ValueError,'家具用途无法确认'):
                check_native_scene_images(self.path,self.record,ask=asks)
        asks.assert_called_once()
        self.assertEqual(self.record['scene_assets'][0]['quality_review']['uncertainties'],
                         self.review['uncertainties'])
        with self.assertRaisesRegex(ValueError,'尚无明确'):
            prepare_scene_repair(self.path,self.record,self.record['scene_assets'][0])

    def test_incidental_note_is_preserved_with_scope_evidence_and_does_not_trigger_repair(self):
        self.review.update(issues=[],uncertainties=['画外摆件完整形状未知。'],continuity_checks=[],
            uncertainty_assessments=[dict(index=0,scope='incidental',requirement='',
                evidence='右缘仅可见局部装饰家具。',impact='批准设计不要求完整展示该家具，门槛通路已可见且未受遮挡。')])
        check_native_scene_images(self.path,self.record,ask=Mock(return_value=self.review))
        report=self.record['scene_assets'][0]['quality_review']
        self.assertEqual(report['uncertainties'],self.review['uncertainties'])
        self.assertEqual(report['blocking_uncertainties'],[])
        self.assertFalse(report['quality_guaranteed'])
        with self.assertRaisesRegex(ValueError,'尚无明确'):
            prepare_scene_repair(self.path,self.record,self.record['scene_assets'][0])

    def test_requirement_uncertainty_blocks_even_when_another_note_is_incidental(self):
        self.review.update(issues=[],uncertainties=['局部装饰未知。','门槛被遮挡。'],
            uncertainty_assessments=[dict(index=0,scope='incidental',requirement='',
                evidence='墙角摆件仅有局部可见。',impact='该装饰未被指定，不影响可见文字。'),
                dict(index=1,scope='requirement',requirement='门槛至座位的连续通路',
                     evidence='门前大面积地面被遮挡。',impact='无法确认角色能按批准路径入场。')])
        with self.assertRaisesRegex(ValueError,'门槛被遮挡'):
            check_native_scene_images(self.path,self.record,ask=Mock(return_value=self.review))
        self.assertEqual(self.record['scene_assets'][0]['quality_review']['blocking_uncertainties'],['门槛被遮挡。'])

    def test_incomplete_or_contradictory_scope_evidence_cannot_remove_a_blocker(self):
        base=dict(index=0,scope='incidental',requirement='',evidence='画外完整形状无法观察。',impact='未影响批准的窗门与通路。')
        for rows in ([],[dict(base,index=1)],[dict(base,index=False)],[dict(base,impact='')],
                     [dict(base,requirement='关键物体')],[dict(base,scope='requirement')],
                     [dict(base,scope='ignore')]):
            with self.subTest(rows=rows),self.assertRaises(ValueError):
                blocking_scene_uncertainties(dict(uncertainties=['未知'],uncertainty_assessments=rows))
        self.assertEqual(blocking_scene_uncertainties(dict(uncertainties=['旧报告未知'])),['旧报告未知'])
