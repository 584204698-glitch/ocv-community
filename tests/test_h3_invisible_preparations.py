import unittest
from backend.app.h3_dialogue import _visible_action
from backend.app.h3_dialogue import DialoguePlan,build_prompt,performance_intervals,normalize_dialogue
from tests.test_h3_dialogue import data


class InvisiblePreparationTests(unittest.TestCase):
    def test_compiled_performance_keeps_approved_plan_and_exact_dialogue(self):
        value=data();plan=DialoguePlan.model_validate(value)
        hidden='Outside the frame, the visitor conceals a letter. Neither he nor the letter enters the frame.'
        value['performance_beats']=[dict(start=a,end=b,action='The women remain seated. '+hidden,
                                         action_zh='两人保持坐姿，画外来客藏好信封而不入画。')
                                    for a,b in performance_intervals(plan.duration,plan.turns)]
        plan=DialoguePlan.model_validate(value);before=plan.model_dump()
        prompt=build_prompt(plan,picture_count=3)
        self.assertNotIn('visitor',prompt);self.assertIn('The women remain seated.',prompt)
        for turn in value['turns']:self.assertIn(normalize_dialogue(turn['text']),prompt)
        self.assertEqual(plan.model_dump(),before)

    def test_invisible_silent_preparation_is_not_a_rendered_actor_event(self):
        prefix='The girl looks over her shoulder. '
        hidden='Outside the frame, the visitor puts a letter inside his jacket. Neither he nor the letter enters the frame.'
        suffix=' The girl holds the lantern still.'
        self.assertEqual(_visible_action(prefix+hidden+suffix),prefix+suffix)

    def test_audible_events_dialogue_and_actual_entrance_are_retained(self):
        for action in (
            'Outside the frame, the visitor knocks on the door. Neither he nor the letter enters the frame.',
            'Off-screen the mother says hello. Neither she nor the phone appears in the frame.',
            'Outside the frame, the visitor picks up a letter. He enters the frame carrying it.',
            'The visitor enters from the doorway.',
        ):
            self.assertEqual(_visible_action(action),action)
