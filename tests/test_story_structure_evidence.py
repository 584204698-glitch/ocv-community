import importlib.util
from pathlib import Path
import unittest

path = Path(__file__).resolve().parents[1]/'plugins/ocv_community/extension_src/backend/app/story_structure_evidence.py'
spec = importlib.util.spec_from_file_location('story_structure_evidence', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class StoryEvidenceTest(unittest.TestCase):
    text = '小禾抬起漏杯。水滴落下。她把杯子移到花盆旁。'
    nodes = [{'id':'start', 'function':'setup', 'quote':'小禾抬起漏杯。'},
             {'id':'drop', 'function':'change', 'quote':'水滴落下。'},
             {'id':'end', 'function':'action', 'quote':'她把杯子移到花盆旁。'}]

    def test_frozen_text_and_unicode_offsets_are_auditable(self):
        result = module.verify(self.text, self.nodes,
            links=[{'from':'start', 'to':'drop', 'why':'杯子漏水'}],
            cycles=[{'setup':'start', 'action':'drop', 'payoff':'end'}])
        for node in result['nodes']:
            self.assertEqual(self.text[node['start_character']:node['end_character']], node['quote'])
        self.assertEqual(result['counts']['cycles'], 1)
        self.assertNotIn('score', result)
        self.assertEqual(module.verify(self.text, self.nodes, expected_sha256=result['text_sha256'])['text_sha256'], result['text_sha256'])
        with self.assertRaisesRegex(ValueError, 'TEXT_CHANGED'):
            module.verify(self.text+' ', self.nodes, expected_sha256=result['text_sha256'])

    def test_invented_and_ambiguous_quotes_are_rejected(self):
        for text, nodes in [(self.text, [dict(self.nodes[0], quote='不存在')]),
                            (self.text*2, self.nodes), (self.text, [self.nodes[0]]*2)]:
            with self.assertRaises(ValueError):
                module.verify(text, nodes)

    def test_backwards_and_missing_references_are_rejected(self):
        for link in [{'from':'end','to':'start','why':'reversed'},
                     {'from':'missing','to':'end','why':'missing'}]:
            with self.assertRaisesRegex(ValueError, 'LINK_INVALID'):
                module.verify(self.text, self.nodes, links=[link])
        with self.assertRaisesRegex(ValueError, 'CYCLE_INVALID'):
            module.verify(self.text, self.nodes, cycles=[{'setup':'start','action':'end','payoff':'drop'}])


if __name__ == '__main__':
    unittest.main()
