import unittest
from backend.app.story_source_alignment import restore_repeated_source

class SourceAlignmentTests(unittest.TestCase):
    def test_recovers_citation_only(self):
        c={'shots':[{'source_text':'她数着数，笑了。','native_dialogue':{'turns':[{'text':'别动。'}]}}]}
        self.assertEqual(len(restore_repeated_source(c,'她数着数着，笑了。')),1)
        self.assertEqual(c['shots'][0]['source_text'],'她数着数着，笑了。')
        self.assertEqual(c['shots'][0]['native_dialogue']['turns'][0]['text'],'别动。')
        self.assertFalse(restore_repeated_source(c,'她数着数着，笑了。'))

    def test_never_repairs_missing_event_reorder_or_negation(self):
        for copied,original in [('她走了。','她没有走了。'),('奶奶笑了。','爸爸回来。奶奶笑了。'),('乙。甲。','甲。乙。'),('她笑了。','她哭了。')]:
            c={'shots':[{'source_text':copied}]}
            self.assertFalse(restore_repeated_source(c,original))
            self.assertEqual(c['shots'][0]['source_text'],copied)

    def test_does_not_move_shot_boundaries(self):
        c={'shots':[{'source_text':'数着数'},{'source_text':'，笑了。'}]}
        self.assertFalse(restore_repeated_source(c,'数着数着，笑了。'))
