"""Regression fixtures adapted from upstream 2d26af9 prompt authority tests."""
import unittest

from backend.app import h3_prompt_agent as agent


class UpstreamSectionTests(unittest.TestCase):
    def test_inline_sections_preserve_bodies_for_each_input_mode(self):
        for mode, names in (
            ('composition_reference', agent._SECTIONS),
            ('single_first', agent._KEYFRAME_SECTIONS),
            ('first_last', agent._KEYFRAME_SECTIONS),
        ):
            with self.subTest(mode=mode):
                source = ' '.join(f'{name}: Original content {index}.' for index, name in enumerate(names))
                result = agent._repair_section_structure(source, mode)
                for index, name in enumerate(names):
                    self.assertEqual(agent._section_body(result, name).strip(), f'Original content {index}.')
                self.assertEqual(agent._repair_section_structure(result, mode), result)

    def test_duplicates_fail_instead_of_becoming_a_second_design(self):
        source = ' '.join(f'{name}: Approved content.' for name in agent._SECTIONS)
        with self.assertRaisesRegex(ValueError, '重复'):
            agent._repair_section_structure(source + ' summary: Another design.', 'composition_reference')

    def test_missing_sections_are_not_fabricated(self):
        with self.assertRaisesRegex(ValueError, '段落结构'):
            agent._repair_section_structure('summary: Only a summary.', 'composition_reference')
