import unittest

from backend.app.story_genre_profiles import PROFILES, production_profile, supplement
from backend.app.story_writer import STORY_PRESETS, STORY_WRITER_SYSTEM_PROMPT, story_writer_system_prompt
from tests.test_story_development import QueueCall, brief, draft, plan, review
from backend.app.story_development import develop_story


class GenreProfileTests(unittest.TestCase):
    def test_every_builtin_preset_has_a_fixed_profile(self):
        self.assertEqual(set(PROFILES), set(STORY_PRESETS))
        for identity in STORY_PRESETS:
            if identity != 'general':
                for stage in ('writer', 'director', 'review'):
                    self.assertIn(identity, supplement(identity, stage))

    def test_unknown_and_art_style_use_unchanged_general_fallback(self):
        self.assertEqual(story_writer_system_prompt({'preset': 'unknown'}), STORY_WRITER_SYSTEM_PROMPT)
        for parameters in ({}, {'style': '日系动画'}, {'story_snapshot': {'brief': {'preset': 'unknown'}}}):
            self.assertEqual(production_profile(parameters), 'general')
        self.assertEqual(supplement(None, 'director'), '')

    def test_adopted_brief_selects_profile_without_modification(self):
        parameters = {'story_snapshot': {'brief': {'preset': 'fantasy_rule'}}}
        self.assertEqual(production_profile(parameters), 'fantasy_rule')
        self.assertEqual(parameters, {'story_snapshot': {'brief': {'preset': 'fantasy_rule'}}})

    def test_non_picturebook_development_uses_profile_without_extra_call(self):
        call = QueueCall(draft(), review())
        develop_story(brief(preset='everyday_comedy'), plan=plan(), call=call)
        self.assertEqual(len(call.requests), 2)
        self.assertIn('固定题材规则（everyday_comedy', call.requests[0]['system_prompt'])


if __name__ == '__main__':
    unittest.main()
