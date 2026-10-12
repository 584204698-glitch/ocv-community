import copy
import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from backend.app import video_native_image_quality as quality, one_click, video_studio as studio


def clear_review(ids):
    return dict(observations=[dict(shot_id=i,description='One character beside a closed door.') for i in ids],
                issues=[],uncertainties=[],continuity_checks=[dict(from_shot_id=a,to_shot_id=b,
                    camera='Same doorway and camera direction.',
                    characters=dict(status='consistent',evidence='Character remains beside the door.'),
                    setting=dict(status='consistent',evidence='The same closed door is visible.'))
                    for a,b in zip(ids,ids[1:])])


class NativeCoreImageTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.path=Path(tmp.name)
        self.shots=[]
        for i in range(6):
            Image.new('RGB',(20,12),(i*20,80,30)).save(self.path/f'{i}.jpg')
            self.shots.append(dict(id=f'shot_{i}',kind='video',image=f'{i}.jpg',image_status='completed',
                action='Open the door.',native_dialogue={'scene':'A opens the door.'}))

    def ask(self,system,evidence,images):
        self.assertEqual(len(images),len(evidence['image_order']))
        self.assertTrue(all(row['data'] for row in images))
        return clear_review(evidence['image_order'])

    def test_provider_syntax_repair_preserves_findings_without_rescreening_images(self):
        review = clear_review(['shot_0'])
        review['issues'] = [dict(shot_id='shot_0', code='cast', evidence='An extra person is visible.',
                                correction='Remove the extra person beside the doorway.')]
        review['uncertainties'] = ['The rear hand is obscured.']
        raw = json.dumps(review).replace('"issues":', '"issues"', 1)
        original = (self.path / '0.jpg').read_bytes()
        with patch('backend.app.video_agents.generate_gemini_text',
                   side_effect=[raw, json.dumps(review)]) as generate:
            result = quality.screen_group(self.path, self.shots[:1])
            self.assertEqual(result['review'], review)
            self.assertEqual(generate.call_count, 2)
            initial, repaired = [call.kwargs for call in generate.call_args_list]
            self.assertEqual(len(initial['image_data']), 1)
            self.assertEqual(initial['temperature'], .1)
            self.assertEqual(initial['max_output_tokens'], 4500)
            self.assertNotIn('image_data', repaired)
            payload = json.loads(repaired['user_prompt'])
            self.assertEqual(payload['raw_output'], raw)
            self.assertEqual(payload['original_request'], {'response_contract': quality.SYSTEM})
            self.assertIn('不删除问题', repaired['system_prompt'])
            self.assertEqual(quality.screen_group(self.path, self.shots[:1]), result)
            self.assertEqual(generate.call_count, 2)
        response = next((self.path / 'assets/core_image_reviews').glob('*.response.json'))
        self.assertEqual(json.loads(response.read_text(encoding='utf8'))['model_outputs'],
                         {'original': raw, 'repaired': json.dumps(review)})
        self.assertEqual((self.path / '0.jpg').read_bytes(), original)

    def test_failed_syntax_repair_or_invalid_shape_retains_diagnostics_without_pass_cache(self):
        raw = '{"observations" []}'
        for repaired in (raw, '{"observations":[],"uncertainties":[]}',
                         '{"observations":[],"issues":[],"uncertainties":[]}'):
            with self.subTest(repaired=repaired), patch('backend.app.video_agents.generate_gemini_text',
                    side_effect=[raw, repaired]) as generate:
                with self.assertRaises(ValueError):
                    quality.screen_group(self.path, self.shots[:1])
                self.assertEqual(generate.call_count, 2)
                files = list((self.path / 'assets/core_image_reviews').glob('*.json'))
                self.assertEqual(len(files), 2)
                self.assertTrue(all(p.name.endswith(('.response.json', '.rejected.json')) for p in files))
                rejected = json.loads(next(p for p in files if p.name.endswith('.rejected.json')).read_text(encoding='utf8'))
                self.assertEqual(rejected['model_outputs'], {'original': raw, 'repaired': repaired})
                if repaired == raw:
                    self.assertIn('repaired_content', rejected['error'])

    def test_valid_json_missing_fields_does_not_trigger_repair(self):
        with patch('backend.app.video_agents.generate_gemini_text',
                   return_value='{"observations":[]}') as generate:
            with self.assertRaises(ValueError):
                quality.screen_group(self.path, self.shots[:1])
            generate.assert_called_once()

    def test_provider_envelope_failure_is_classified_without_body_retention_or_retry(self):
        private = 'PRIVATE_PROVIDER_BODY'
        with patch('backend.app.video_agents.generate_gemini_text',
                   side_effect=json.JSONDecodeError(private, private, 1)) as generate:
            with self.assertRaisesRegex(ValueError, 'provider_response') as caught:
                quality.screen_group(self.path, self.shots[:1])
            generate.assert_called_once()
            self.assertNotIn(private, str(caught.exception))
        files = list((self.path / 'assets/core_image_reviews').glob('*.json'))
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].name.endswith('.rejected.json'))
        self.assertNotIn(private, files[0].read_text(encoding='utf8'))

    def test_actual_images_and_plan_key_cache_without_rewriting_assets(self):
        originals={p.name:p.read_bytes() for p in self.path.glob('*.jpg')}
        first=quality.screen_group(self.path,self.shots[:2],ask=self.ask)
        with patch.object(quality,'Image',side_effect=AssertionError('Cache should be reused')):
            self.assertEqual(quality.screen_group(self.path,self.shots[:2],ask=None),first)
        changed=copy.deepcopy(self.shots[:2]);changed[0]['native_dialogue']['scene']='A closes the door.'
        second=quality.screen_group(self.path,changed,ask=self.ask)
        self.assertNotEqual(first['fingerprint'],second['fingerprint'])
        Image.new('RGB',(20,12),'blue').save(self.path/'0.jpg')
        third=quality.screen_group(self.path,changed,ask=self.ask)
        self.assertNotEqual(second['fingerprint'],third['fingerprint'])
        self.assertFalse(third['audio_checked']);self.assertFalse(third['quality_guaranteed'])
        for name,data in originals.items():
            if name!='0.jpg':self.assertEqual((self.path/name).read_bytes(),data)

    def test_incomplete_pair_checks_or_unreported_conflict_cannot_be_cached_as_pass(self):
        ids = [s['id'] for s in self.shots[:3]]
        cases = []
        missing = clear_review(ids); missing.pop('continuity_checks'); cases.append(missing)
        partial = clear_review(ids); partial['continuity_checks'].pop(); cases.append(partial)
        foreign = clear_review(ids); foreign['continuity_checks'][0]['from_shot_id'] = 'foreign'; cases.append(foreign)
        no_landmark = clear_review(ids); no_landmark['continuity_checks'][0]['camera'] = ''; cases.append(no_landmark)
        no_setting = clear_review(ids); no_setting['continuity_checks'][0].pop('setting'); cases.append(no_setting)
        ignored = clear_review(ids); ignored['continuity_checks'][0]['characters']['status'] = 'conflict'; cases.append(ignored)
        for result in cases:
            with self.subTest(result=result), self.assertRaises(ValueError):
                quality.screen_group(self.path,self.shots[:3],ask=lambda *args: result)
        files = list((self.path/'assets/core_image_reviews').glob('*.json'))
        self.assertTrue(files)
        self.assertTrue(all(p.name.endswith('.rejected.json') for p in files))

    def test_uncertain_pair_is_retained_and_stops_before_next_group(self):
        report = {'review': clear_review([s['id'] for s in self.shots[:4]])}
        report['review']['continuity_checks'][0]['characters'] = dict(
            status='uncertain',evidence='The doorway is visible but actor identity is obscured.')
        with patch('backend.app.language_routing.project_language_scope',return_value=nullcontext()), \
             patch.object(studio,'planning_parameters',return_value={}), \
             patch.object(quality,'screen_group',return_value=report) as screen:
            with self.assertRaises(quality.CoreImageReviewRequired) as caught:
                quality.check_native_core_images(self.path,{'native_story':True,'shots':self.shots},1)
            self.assertEqual(screen.call_count,1)
            self.assertEqual(caught.exception.reports,[report])
            self.assertIn('尚未核实',str(caught.exception))
            self.assertEqual(report['review']['issues'],[])

    def test_explained_camera_change_does_not_force_redraw(self):
        review = clear_review(['shot_0','shot_1'])
        review['continuity_checks'][0].update(camera='The second view reverses the doorway and window.',
            characters=dict(status='explained_change',evidence='Actors exchange screen sides under the reverse camera.'),
            setting=dict(status='not_comparable',evidence='The first wall is outside the reverse shot.'))
        report = quality.screen_group(self.path,self.shots[:2],ask=lambda *args:review)
        with patch('backend.app.language_routing.project_language_scope',return_value=nullcontext()), \
             patch.object(studio,'planning_parameters',return_value={}), \
             patch.object(quality,'screen_group',return_value=report):
            self.assertEqual(quality.check_native_core_images(self.path,
                {'native_story':True,'shots':self.shots[:2]},1),[report])

    def test_planning_feedback_reads_only_current_cached_images_without_model_calls(self):
        shots = copy.deepcopy(self.shots[:2])
        record = {'native_story': True, 'shots': shots}
        review = clear_review([s['id'] for s in shots])
        review['issues'] = [dict(shot_id='shot_1',code='continuity',
            evidence='The same visible wall loses a fixed sign.',
            correction='Retain the original sign on the visible wall.')]
        report = quality.screen_group(self.path,shots,ask=lambda *args:review)
        with patch('backend.app.gemini_client.generate_gemini_text',side_effect=AssertionError('No paid call')):
            self.assertEqual(quality.cached_current_findings(self.path,record),[report])
            shots[0]['image_prompt'] = 'Changed design'
            self.assertEqual(quality.cached_current_findings(self.path,record),[])
            shots[0].pop('image_prompt')
            Image.new('RGB',(20,12),'purple').save(self.path/'0.jpg')
            self.assertEqual(quality.cached_current_findings(self.path,record),[])
            shots[1]['image_status'] = 'pending'
            self.assertEqual(quality.cached_current_findings(self.path,record),[])
            self.assertEqual(quality.cached_current_findings(self.path,{'shots':shots}),[])

    def test_missing_foreign_or_incomplete_observation_fails_closed(self):
        for change in ({'image':'missing.jpg'},{'image':'../outside.jpg'},{'image_status':'running'}):
            with self.subTest(change=change),self.assertRaises(Exception):
                quality.screen_group(self.path,[{**self.shots[0],**change}],ask=self.ask)
        def omitted(*args):return dict(observations=[],issues=[],uncertainties=[])
        with self.assertRaisesRegex(ValueError,'逐张'):
            quality.screen_group(self.path,self.shots[:2],ask=omitted)
        saved=list((self.path/'assets/core_image_reviews').glob('*.json'))
        self.assertTrue(saved)
        self.assertTrue(all(p.name.endswith('.rejected.json') for p in saved))

    def test_opening_instruction_is_not_lost_and_changes_invalidate_review_cache(self):
        shot = self.shots[0]
        shot['visual_description'] = 'A character hides behind a tree.'
        shot['image_prompt'] = 'Only two ears are visible; the face stays behind the trunk.'
        shot['image_applied_prompt'] = shot['image_prompt']
        calls = []
        def inspect(system, evidence, images):
            calls.append(copy.deepcopy(evidence['shots'][0]))
            return clear_review(evidence['image_order'])
        first = quality.screen_group(self.path, [shot], ask=inspect)
        self.assertEqual(calls[0]['image_prompt'], shot['image_prompt'])
        self.assertNotIn('image_applied_prompt', calls[0])
        shot['image_prompt'] = 'The face peeks around the trunk.'
        second = quality.screen_group(self.path, [shot], ask=inspect)
        self.assertNotEqual(first['fingerprint'], second['fingerprint'])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]['image_prompt'], shot['image_prompt'])
        self.assertEqual(calls[1]['image_applied_prompt'], shot['image_applied_prompt'])
        self.assertEqual(calls[1]['image_sha256'], calls[0]['image_sha256'])

    def test_structured_uncertainty_is_preserved_without_hiding_issues(self):
        def uncertain(system,evidence,images):
            review=clear_review(evidence['image_order'])
            review['uncertainties']=[{'shot_id':'shot_0','description':'Feet are obscured.'}]
            review['issues']=[dict(shot_id='shot_0',code='cast',evidence='An extra visible person.',
                                   correction='Remove only the extra person beside the doorway.')]
            return review
        report=quality.screen_group(self.path,self.shots[:1],ask=uncertain)
        self.assertEqual(report['review'],uncertain(None,{'image_order':['shot_0']},None))
        self.assertEqual(quality.screen_group(self.path,self.shots[:1]),report)

    def test_invalid_uncertainty_keeps_raw_response_and_cannot_enter_success_cache(self):
        for item in (None,{'shot_id':'foreign','description':'Cannot see.'},
                     {'shot_id':'shot_0'},{'message':['wrong type']},{'passed':True}):
            def invalid(*args):
                value=clear_review(['shot_0']);value['uncertainties']=[item];return value
            with self.subTest(item=item),self.assertRaisesRegex(ValueError,'不确定项'):
                quality.screen_group(self.path,self.shots[:1],ask=invalid)
        files=list((self.path/'assets/core_image_reviews').glob('*.json'))
        self.assertTrue(all(p.name.endswith('.rejected.json') for p in files))
        rejected=json.loads(files[0].read_text(encoding='utf8'))
        self.assertEqual(rejected['response']['uncertainties'],[{'passed':True}])

    def test_repaired_pixels_are_rechecked_against_prior_findings_and_need_concrete_edits(self):
        self.shots[0]['native_image_repairs']=[dict(issues=[dict(code='opening_state',evidence='The wrong hand holds the cup.')])]
        def check(system,evidence,images):
            self.assertEqual(evidence['shots'][0]['prior_image_findings'][0]['evidence'],'The wrong hand holds the cup.')
            result=clear_review(evidence['image_order'])
            result['issues']=[dict(shot_id='shot_0',code='opening_state',evidence='Still held in the left hand.')]
            return result
        with self.assertRaisesRegex(ValueError,'目标修改指令'):
            quality.screen_group(self.path,self.shots[:1],ask=check)
        def corrected(system,evidence,images):
            result=check(system,evidence,images)
            result['issues'][0]['correction']='Move the cup to the right hand; keep the room unchanged.'
            return result
        result=quality.screen_group(self.path,self.shots[:1],ask=corrected)
        self.assertIn('right hand',result['review']['issues'][0]['correction'])

    def test_cached_malformed_report_cannot_pass(self):
        report=quality.screen_group(self.path,self.shots[:1],ask=self.ask)
        target=self.path/'assets/core_image_reviews'/f"{report['fingerprint']}.json"
        report['review']['observations']=[]
        target.write_text(json.dumps(report))
        with self.assertRaisesRegex(ValueError,'逐张'):
            quality.screen_group(self.path,self.shots[:1])

    def test_groups_cover_all_images_with_boundary_overlap_and_keep_narration_unchanged(self):
        calls=[]
        def screen(path,group):
            calls.append([s['id'] for s in group]);return {'review':clear_review(calls[-1])}
        record={'native_story':{'text':'story'},'shots':self.shots}
        with patch('backend.app.language_routing.project_language_scope',return_value=nullcontext()), \
             patch.object(studio,'planning_parameters',return_value={}),patch.object(quality,'screen_group',side_effect=screen):
            quality.check_native_core_images(self.path,record,1)
            self.assertEqual(calls,[['shot_0','shot_1','shot_2','shot_3'],['shot_3','shot_4','shot_5']])
            calls.clear()
            self.assertEqual(quality.check_native_core_images(self.path,{'shots':self.shots},1),[])
            self.assertEqual(calls,[])
            quality.check_native_core_images(self.path,record,1,cancelled=lambda:True)
            self.assertEqual(calls,[])

    def test_issue_prevents_confirmation_and_no_gpu_submission(self):
        record={'id':'a'*32,'revision':2,'native_story':{'text':'story'},'shots':self.shots}
        record['creation_parameters']={'quality_review_policy':'strict'}
        with patch.object(quality,'check_native_core_images',side_effect=ValueError('shot_1：门的状态相反')), \
             patch.object(studio,'confirm_storyboard_images_for_user') as confirm:
            with self.assertRaisesRegex(ValueError,'shot_1'):
                one_click.dispatch('confirm_images',{'id':1},record)
            confirm.assert_not_called()

    def test_pause_during_screening_does_not_confirm_old_snapshot(self):
        record={'id':'a'*32,'revision':2,'native_story':{'text':'story'},'shots':self.shots}
        record['creation_parameters']={'quality_review_policy':'strict'}
        token=one_click._executing.set(True)
        try:
            with patch.object(quality,'check_native_core_images',return_value=[]), \
                 patch.object(one_click,'load',return_value={'state':'paused'}), \
                 patch.object(studio,'confirm_storyboard_images_for_user') as confirm:
                one_click.dispatch('confirm_images',{'id':1},record)
                confirm.assert_not_called()
        finally:one_click._executing.reset(token)

    def test_issues_report_exact_shot_and_do_not_screen_remaining_groups(self):
        report={'review':{'issues':[dict(shot_id='shot_1',code='opening_state',evidence='A hand holds the bell instead of the stated bill contact.')]}}
        with patch('backend.app.language_routing.project_language_scope',return_value=nullcontext()), \
             patch.object(studio,'planning_parameters',return_value={}),patch.object(quality,'screen_group',return_value=report) as screen:
            with self.assertRaisesRegex(ValueError,'shot_1'):
                quality.check_native_core_images(self.path,{'native_story':True,'shots':self.shots},1)
            self.assertEqual(screen.call_count,1)
