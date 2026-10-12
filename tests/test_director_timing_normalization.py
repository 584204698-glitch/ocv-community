import copy
import unittest

from backend.app.h3_dialogue import DialoguePlan, Turn, validate_dialogue_draft
from backend.app.video_native_story import normalize_director_timing
from tests.test_h3_dialogue import data


class DirectorTimingTests(unittest.TestCase):
    def draft(self, duration=8.5):
        value = data()
        value['duration'] = duration
        value['performance_beats'] = [
            dict(start=0, end=.2, action='The friends remain beside the table.', action_zh='两位朋友留在桌旁。'),
            dict(start=.2, end=2.7, action='The right friend addresses the left friend.', action_zh='右侧朋友面向左侧朋友。'),
            dict(start=2.7, end=3, action='The left friend looks toward the right friend.', action_zh='左侧朋友看向右侧朋友。'),
            dict(start=3, end=5.5, action='The left friend addresses the right friend.', action_zh='左侧朋友面向右侧朋友。'),
            dict(start=5.5, end=duration, action='The friends keep their places beside the table.', action_zh='两位朋友保持桌旁位置。'),
        ]
        return value

    def test_fractional_duration_only_pads_tail_without_rewriting_speech_or_action(self):
        value = self.draft()
        before = copy.deepcopy(value)
        fixed = normalize_director_timing(value)
        self.assertEqual(value, before)
        self.assertEqual(fixed['duration'], 9)
        self.assertEqual(fixed['turns'], [Turn.model_validate(t).model_dump() for t in value['turns']])
        self.assertEqual(fixed['performance_beats'][:-1], value['performance_beats'])
        self.assertEqual((fixed['performance_beats'][-1]['start'], fixed['performance_beats'][-1]['end']), (8.5, 9))
        validate_dialogue_draft(fixed)
        with self.assertRaises(ValueError):
            DialoguePlan.model_validate(value, context={'unbound_draft': True})

    def test_whole_float_normalizes_without_a_new_beat(self):
        value = self.draft(8.0)
        fixed = normalize_director_timing(value)
        self.assertEqual(fixed['duration'], 8)
        self.assertIs(type(fixed['duration']), int)
        self.assertEqual(fixed['performance_beats'], value['performance_beats'])

    def test_padding_cannot_hide_invalid_original_timing(self):
        for mutate in (
            lambda v: v['performance_beats'][-1].update(end=8),
            lambda v: v['performance_beats'][-1].update(start=5.7),
            lambda v: v['turns'][-1].update(end=8.7),
            lambda v: v.update(duration=15.1),
            lambda v: v.update(duration=float('nan')),
        ):
            value = self.draft()
            mutate(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_director_timing(value)

    def test_integer_drafts_keep_the_existing_contract(self):
        value = self.draft(8)
        self.assertIs(normalize_director_timing(value), value)


if __name__ == '__main__':
    unittest.main()
