import copy
import unittest

from backend.app.video_timing_policy import storyboard_pacing, story_speech_budget


class StoryPacingBudgetTests(unittest.TestCase):
    def test_measures_gap_across_silent_action_shots_without_rejecting_them(self):
        shots = [
            dict(id='request', native_dialogue=dict(duration=4, turns=[
                dict(character_id='a', text='帮帮我。', start=.4, end=2)])),
            dict(id='handoff', native_dialogue=dict(duration=6, turns=[])),
            dict(id='answer', native_dialogue=dict(duration=4, turns=[
                dict(character_id='b', text='当然了！', start=.8, end=2)])),
        ]
        before = copy.deepcopy(shots)
        report = storyboard_pacing(shots)
        self.assertEqual(report['planned_seconds'], 14)
        self.assertEqual(report['speech_handoffs'], [
            dict(after_shot='request', before_shot='answer', seconds=8.8)])
        self.assertEqual(report['shots'][1]['non_speech_seconds'], 6)
        self.assertEqual(shots, before)
        self.assertNotIn('blocking', report)

    def test_source_counts_are_only_reference_and_do_not_become_generated_lines(self):
        report = story_speech_budget('她说：“帮帮我。”他点头：「当然了！」')
        self.assertEqual(report['quoted_chinese_characters'], 6)
        self.assertEqual(report['quoted_natural_seconds_min'], 1.5)
        self.assertEqual(story_speech_budget('独自走完这条长路。')['quoted_chinese_characters'], 0)
        self.assertNotIn('turns', report)

    def test_review_receives_whole_film_metrics_in_existing_request(self):
        from backend.app.video_dialogue_story_review import review_story
        from unittest.mock import Mock
        shots = [dict(id='one', native_dialogue=dict(duration=4, turns=[]))]
        ask = Mock(return_value=dict(reviewed_shot_ids=['one'],
                   viewer_question='人物能否完成眼前的任务。',
                   motivation_and_payoff='通过无声行动表达既定人物选择。', problems=[]))
        review_story(dict(production_text='无声行动', native_speech_mode='dialogue',
                         project_cast={}), shots, ask=ask)
        self.assertEqual(ask.call_count, 1)
        self.assertEqual(ask.call_args.args[1]['storyboard_pacing']['planned_seconds'], 4)

    def test_explicit_replan_of_completed_candidate_preserves_assets_and_history(self):
        from backend.app.video_native_story import retry_failed_plan
        state = dict(candidate={'shots': [{'source_text': '采用稿'}]},
                     story_review={'problems': []}, review=[{'problems': []}])
        record = dict(status='storyboard_review', native_planning_state=copy.deepcopy(state),
                      shots=[{'id': 'one', 'image': 'existing.jpg'}], project_cast={'characters': []})
        assets = copy.deepcopy(record['shots'])
        retry_failed_plan(record)
        self.assertNotIn('native_planning_state', record)
        self.assertEqual(record['native_planning_history'][-1], state)
        self.assertEqual(record['shots'], assets)

    def test_automatic_or_unfinished_plan_is_not_discarded(self):
        from backend.app.video_native_story import retry_failed_plan
        state = dict(candidate={'shots': []}, story_review={'problems': []}, review=[])
        record = dict(status='planning', native_planning_state=copy.deepcopy(state))
        retry_failed_plan(record)
        self.assertEqual(record['native_planning_state'], state)

    def test_authorial_request_for_more_dialogue_is_advisory_even_when_strict(self):
        from backend.app.video_dialogue_story_review import partition_problems
        issue = dict(code='unnatural_dialogue', origin='adopted_story', shot_ids=['one'])
        for strict in (False, True):
            blocking, advisory = partition_problems(dict(problems=[issue]), strict=strict)
            self.assertEqual(blocking, [])
            self.assertEqual(advisory, [issue])


if __name__ == '__main__':
    unittest.main()
