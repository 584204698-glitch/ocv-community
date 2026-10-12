"""Base identity sheets must not replace approved opening costumes."""
import copy
import unittest
from backend.app.h3_dialogue import DialoguePlan, build_prompt
from tests.test_h3_dialogue import data


class CostumeReferenceRolesTests(unittest.TestCase):
    def test_opening_costume_and_hair_use_core_identity_uses_character_sheet(self):
        draft = data()
        draft['speakers'][0]['appearance'] = 'A long braid and a gray historical gown.'
        before = copy.deepcopy(draft)
        prompt = build_prompt(DialoguePlan.model_validate(draft), picture_count=3)
        self.assertIn('facial identity comes from <Picture 2>', prompt)
        self.assertIn('hairstyle and clothing come from its existing appearance in <Picture 1>', prompt)
        self.assertIn('identity image does not replace the opening costume or hairstyle', prompt)
        self.assertNotIn('preserve identity, clothing and anatomy from <Picture 2>', prompt)
        self.assertEqual(draft, before)
        self.assertIn('A long braid and a gray historical gown.', prompt)

    def test_later_actor_keeps_identity_sheet_costume_without_core_insertion(self):
        draft = data()
        draft['speakers'][0]['presence'] = 'later'
        prompt = build_prompt(DialoguePlan.model_validate(draft), picture_count=3)
        self.assertIn('<Subject 1> is the character whose identity and clothing come from <Picture 2>', prompt)
        self.assertIn('preserve identity, clothing and anatomy from <Picture 2>', prompt)
        self.assertIn('facial identity comes from <Picture 3>', prompt)
