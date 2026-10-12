"""The local-edit route must not reopen the old composite-frame loophole."""
import copy
import unittest

from backend.app.video_prompt_refresh import revise_motion


class StoryFrameRefreshTest(unittest.TestCase):
    def run_plan(self, status, reference_beat):
        context = {'story_handoff': {'status': status, 'brief': {'idea': '无外耳，蓝色马甲'}}}
        shot = {'id': 's1', 'duration': 10, 'source_subtitles': [{'text': '米粒写好信，阿宁随后推门进来。'}]}
        plan = {'version': 2, 'scene_anchor': '书房', 'participants': ['米粒', '阿宁'],
                'beats': [{'action': '米粒写信', 'texts': []}, {'action': '阿宁进门', 'texts': []}],
                'reference_beat': reference_beat, 'reference_visual': '米粒坐着写信',
                'reference_participants': ['米粒'], 'reference_texts': []}
        def ask(system, payload):
            self.assertEqual(payload['story_context'], context)
            if status == 'current':
                self.assertIn('真实开场首帧', system)
                self.assertNotIn('不要求回到首帧', system)
                self.assertNotIn('静音', system)
                self.assertIn('自然环境声', system)
            return {'shots': [{'id': 's1', 'motion_plan': copy.deepcopy(plan)}]}
        return revise_motion(context, shot, [], '', '米粒坐着写信', 'image_prompt', ask=ask)

    def test_current_story_refresh_rejects_later_reference_beat(self):
        with self.assertRaisesRegex(ValueError, '首帧方案'):
            self.run_plan('current', 2)

    def test_current_story_refresh_keeps_opening_and_whole_shot_separate(self):
        plan = self.run_plan('current', 1)
        self.assertEqual(plan['participants'], ['米粒', '阿宁'])
        self.assertEqual(plan['reference_participants'], ['米粒'])

    def test_detached_independent_manuscript_keeps_legacy_planner(self):
        self.assertEqual(self.run_plan('detached', 2)['reference_beat'], 2)


if __name__ == '__main__':
    unittest.main()
