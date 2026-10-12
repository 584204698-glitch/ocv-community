import copy
import unittest
from unittest.mock import Mock

from backend.app import video_native_story as native, video_renderability as staging
from tests import test_video_native_story as fixtures


class StagingReviewHistoryTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.NativeDirectorTests(); self.f.setUp()
        self.f.raw = fixtures.with_performance(self.f.raw)
        self.params = {'speech_mode': 'dialogue', 'project_cast': self.f.cast}
        self.problem = {'id': 'shot_001', 'problems': [dict(code='opening_conflict',
            evidence='当前与上一镜的持伞位置不同，缺少过渡。', repair='保持雨伞位置，让角色移动到明确的位置。')]}
        self.good = [{'id': 'shot_001', 'problems': []}, {'id': 'shot_002', 'problems': []}]

    def plan(self, ask):
        return native.plan_native_story(self.f.record, self.f.refs, self.params,
            progress=lambda _: None, persist=lambda: None, ask=ask)

    def test_revision_rechecks_saved_candidate_without_new_director_or_story_call(self):
        self.plan(Mock(side_effect=[copy.deepcopy(self.f.raw), fixtures.good_story_review(), {'reviews': self.good}]))
        state = self.f.record['native_planning_state']
        original = copy.deepcopy(state['candidate'])
        state.update(staging_review_revision='old', error='Previous staging failed', attempt=1,
                     review=[self.problem, self.good[1]])
        native.retry_failed_plan(self.f.record)
        self.assertEqual(state['candidate'], original)
        self.assertIn('story_review', state)
        ask = Mock(return_value={'reviews': self.good})
        result = self.plan(ask)
        self.assertEqual(ask.call_count, 1)
        evidence = ask.call_args.args[1]['story_context']['staging_review_history'][0]
        self.assertEqual(evidence['review'][0], self.problem)
        self.assertEqual(evidence['shot_sources'][0]['source_text'], self.f.raw['shots'][0]['source_text'])
        self.assertEqual(state['candidate'], original)
        self.assertEqual(len(result[2]), 2)
        self.assertEqual(state['staging_review_revision'], staging.REVISION)

    def test_same_revision_retry_still_repairs_actual_failed_shots_and_carries_previous_advice(self):
        self.plan(Mock(side_effect=[copy.deepcopy(self.f.raw), fixtures.good_story_review(), {'reviews': self.good}]))
        state = self.f.record['native_planning_state']
        state.update(error='Current staging failed', review=[self.problem, self.good[1]], attempt=1)
        native.retry_failed_plan(self.f.record)
        self.assertNotIn('candidate', state)
        self.assertEqual(state['repair']['shot_ids'], ['shot_001'])
        replacement = copy.deepcopy(self.f.raw['shots'][0]); replacement['action'] = '小兔移到门边，小熊停在原地。'
        ask = Mock(side_effect=[{'replacements': [{'id': 'shot_001', 'shots': [replacement]}]},
                              fixtures.good_story_review(), {'reviews': self.good}])
        self.plan(ask)
        context = ask.call_args.args[1]['story_context']
        self.assertEqual(context['previous_staging_repair']['problems'], 'Current staging failed')
        self.assertEqual(context['staging_review_history'][-1]['review'][0], self.problem)
        self.assertEqual(state['candidate']['shots'][1], self.f.raw['shots'][1])

    def test_history_is_bounded_copied_and_source_anchored_not_assumed_current_ids(self):
        state = {'candidate': copy.deepcopy(self.f.raw), 'review': [self.problem]}
        for index in range(5):
            state['candidate']['shots'][0]['source_text'] = f'旧版片段{index}'
            native._remember_staging_review(state)
        self.assertEqual(len(state['staging_review_history']), 3)
        before = copy.deepcopy(state['staging_review_history'])
        context = native._staging_context({}, state)
        context['staging_review_history'][0]['review'][0]['problems'].clear()
        self.assertEqual(state['staging_review_history'], before)
        self.assertEqual(before[-1]['shot_sources'][0]['source_text'], '旧版片段4')

    def test_setting_conflict_repairs_only_cited_dependencies_and_preserves_dialogue(self):
        previous = copy.deepcopy(self.f.raw)
        unrelated = copy.deepcopy(previous['shots'][0])
        previous['shots'].append(unrelated)
        conflict = {'id': 'shot_002', 'problems': [dict(code='opening_conflict',
            evidence='shot_001的门旁有标牌；shot_002同一取景却禁止一切文字。',
            repair='统一shot_001和shot_002的既有布景，shot_999不存在。')]}
        state = {'candidate': previous, 'review': [conflict], 'error': 'Setting conflict'}
        repair = native._repair_request(state)
        self.assertEqual(repair['shot_ids'], ['shot_001', 'shot_002'])
        fixed = copy.deepcopy(previous['shots'][:2])
        for row in fixed:
            row['image_prompt'] += ' 保留门旁既有标牌，不生成字幕。'
        ask = Mock(return_value={'replacements': [
            {'id': f'shot_{i+1:03d}', 'shots': [row]} for i, row in enumerate(fixed)]})
        merged = native._generate_candidate(ask, native.NATIVE_DIRECTOR, {}, repair)
        self.assertEqual(ask.call_count, 1)
        self.assertEqual(merged['shots'][2], unrelated)
        for before, after in zip(previous['shots'], merged['shots']):
            self.assertEqual(before['source_text'], after['source_text'])
            self.assertEqual(before['native_dialogue']['turns'], after['native_dialogue']['turns'])
        self.assertEqual(state['candidate'], previous)

    def test_current_image_evidence_enters_explicit_retry_without_rewriting_adopted_story(self):
        self.plan(Mock(side_effect=[copy.deepcopy(self.f.raw), fixtures.good_story_review(),
                                   {'reviews': self.good}]))
        original = copy.deepcopy(self.f.record['native_planning_state']['candidate'])
        report = dict(fingerprint='verified-current-report',image_versions={'shot_001':'a','shot_002':'b'},
            review=dict(issues=[dict(shot_id='shot_002',code='continuity',
                evidence='A fixed sign disappears from the visible wall.',
                correction='Retain the same sign on that wall.')],continuity_checks=[
                dict(from_shot_id='shot_001',to_shot_id='shot_002',
                     characters=dict(status='consistent',evidence='Same positions'),
                     setting=dict(status='conflict',evidence='Sign disappears'))]))
        native.retry_failed_plan(self.f.record,[report])
        state = self.f.record['native_planning_state']
        self.assertEqual(state['repair']['shot_ids'],['shot_001','shot_002'])
        self.assertEqual(state['repair']['previous'],original)
        self.assertEqual(state['repair']['image_problems'][0]['image_versions'],report['image_versions'])
        context=native._staging_context({},state)
        self.assertEqual(context['previous_core_image_findings'],state['repair']['image_problems'])
        self.assertNotIn('candidate',state)
        self.assertEqual(state['attempt'],0)
        # One explicit image-driven repair is not permission for another
        # automatic rewrite if independent review still rejects the result.
        answer = {'replacements': [dict(id=f'shot_{i+1:03d}',shots=[copy.deepcopy(row)])
                                  for i,row in enumerate(original['shots'])]}
        ask = Mock(side_effect=[answer,fixtures.good_story_review(),
                               {'reviews':[self.problem,self.good[1]]}])
        with self.assertRaisesRegex(ValueError,'对话镜头设计未通过'):
            self.plan(ask)
        self.assertEqual(ask.call_count,3)
        self.assertEqual(state['candidate'],original)
        cached = Mock(side_effect=AssertionError('Unexpected repeated review'))
        with self.assertRaisesRegex(ValueError,'对话镜头设计未通过'):
            self.plan(cached)
        cached.assert_not_called()

    def test_old_advice_does_not_override_new_failure_or_expand_retry_budget(self):
        ask = Mock(side_effect=[copy.deepcopy(self.f.raw), fixtures.good_story_review(),
            {'reviews': [self.problem, self.good[1]]},
            {'replacements': [{'id': 'shot_001', 'shots': [copy.deepcopy(self.f.raw['shots'][0])]}]},
            fixtures.good_story_review(), {'reviews': [self.problem, self.good[1]]}])
        with self.assertRaisesRegex(ValueError, '对话镜头设计未通过'):
            self.plan(ask)
        self.assertEqual(ask.call_count, 6)
        self.assertEqual(self.f.record['native_planning_state']['attempt'], 1)

    def test_new_reviewer_failure_preserves_candidate_until_explicit_local_retry(self):
        self.plan(Mock(side_effect=[copy.deepcopy(self.f.raw), fixtures.good_story_review(),
                                   {'reviews': self.good}]))
        state = self.f.record['native_planning_state']
        original = copy.deepcopy(state['candidate'])
        state.update(staging_review_revision='story-staging-v7-contact-release-and-held-props',
                     error='Previous review failed', attempt=1,
                     review=[self.problem, self.good[1]])
        native.retry_failed_plan(self.f.record)
        latest = {'id': 'shot_002', 'problems': [dict(code='opening_conflict',
            evidence='听者上一镜在门口，此镜在桌旁，缺少过渡。',
            repair='在当前镜头中保持门口开场，补充走到桌旁的动作。')]}
        ask = Mock(return_value={'reviews': [self.good[0], latest]})
        with self.assertRaisesRegex(ValueError, '对话镜头设计未通过'):
            self.plan(ask)
        self.assertEqual(ask.call_count, 1)
        self.assertEqual(state['candidate'], original)
        self.assertTrue(state['version_revalidation'])
        # Re-entering planning is not permission to pay for another candidate.
        no_call = Mock(side_effect=AssertionError('Unexpected generation'))
        with self.assertRaisesRegex(ValueError, '对话镜头设计未通过'):
            self.plan(no_call)
        no_call.assert_not_called()
        native.retry_failed_plan(self.f.record)
        self.assertEqual(state['repair']['shot_ids'], ['shot_002'])
        self.assertEqual(state['repair']['previous'], original)
        self.assertNotIn('version_revalidation', state)
