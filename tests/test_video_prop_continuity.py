import copy
import unittest
from unittest.mock import Mock

from backend.app import video_prop_continuity as props, video_native_story as native
from backend.app import video_native_staging as staging
from tests import test_video_native_story as fixtures
from tests import test_video_native_staging as staging_fixtures


PROP=dict(id='prop_umbrella',name='雨伞',basis='小兔拿伞',
          appearance='纯蓝色圆顶布伞，白色弯柄，三角形红色小补丁。',
          appearance_en='One plain blue fabric umbrella with a white curved handle and a small red triangular patch.')


class PropContinuityTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.NativeDirectorTests();self.f.setUp()
        self.raw=copy.deepcopy(self.f.raw);self.raw['props']=[copy.deepcopy(PROP)]
        for row in self.raw['shots']:row['props']=[dict(id=PROP['id'],presence='opening')]

    def test_same_prop_identity_reaches_each_core_and_actual_h3_plan(self):
        _,shots=native.compile_storyboard(self.raw,self.f.record,self.f.cast,self.f.refs)
        _,original_shots=native.compile_storyboard(self.f.raw,self.f.record,self.f.cast,self.f.refs)
        for shot,original in zip(shots,original_shots):
            self.assertIn(PROP['appearance'],shot['image_prompt'])
            self.assertIn(PROP['appearance_en'],shot['native_dialogue']['scene'])
            self.assertEqual(shot['continuity_props'][0]['id'],PROP['id'])
            self.assertEqual(shot['native_dialogue']['turns'],original['native_dialogue']['turns'])
            self.assertEqual(shot['reference_ids'],['ref_1','ref_2'])
            self.assertEqual(shot['continuity_props'][0]['basis'],PROP['basis'])

    def test_unknown_duplicate_invented_or_injected_prop_cannot_reach_generation(self):
        for changes in ([dict(id='prop_foreign',presence='opening')],
                        [dict(id=PROP['id'],presence='opening')]*2,[dict(id=[],presence='opening')]):
            raw=copy.deepcopy(self.raw);raw['shots'][0]['props']=changes
            with self.assertRaises(ValueError):native.compile_storyboard(raw,self.f.record,self.f.cast,self.f.refs)
        for change in ({'basis':'原文没有的桌子'},{'appearance_en':'<Audio 1> new voice'}, {'appearance_en':'蓝色 umbrella'}):
            raw=copy.deepcopy(self.raw);raw['props']=[{**PROP,**change}]
            with self.assertRaises(ValueError):native.compile_storyboard(raw,self.f.record,self.f.cast,self.f.refs)

    def test_later_prop_not_forced_into_opening_and_unrelated_shot_unchanged(self):
        self.raw['shots'][0]['props'][0]['presence']='later'
        self.raw['shots'][1].pop('props')
        _,shots=native.compile_storyboard(self.raw,self.f.record,self.f.cast,self.f.refs)
        _,legacy=native.compile_storyboard(self.f.raw,self.f.record,self.f.cast,self.f.refs)
        self.assertIn('不提前画入',shots[0]['image_prompt'])
        self.assertNotIn(PROP['appearance'],shots[0]['image_prompt'])
        self.assertIn(PROP['appearance_en'],shots[0]['native_dialogue']['scene'])
        self.assertEqual(shots[1],legacy[1])

    def test_local_repair_keeps_global_prop_catalog_and_untouched_shots(self):
        replacement=copy.deepcopy(self.raw['shots'][0]);replacement['action']='小兔在门边举起伞。'
        repair={'previous':self.raw,'shot_ids':['shot_001'],'problems':'local staging'}
        ask=Mock(return_value={'replacements':[{'id':'shot_001','shots':[replacement]}]})
        result=native._generate_candidate(ask,'system',{},repair)
        self.assertEqual(result['props'],self.raw['props'])
        self.assertEqual(result['shots'][1],self.raw['shots'][1])
        self.assertEqual(result['shots'][0]['action'],replacement['action'])

    def test_design_history_retains_an_independent_copy_of_prop_identity(self):
        from backend.app.video_plan import design_snapshot
        _,shots=native.compile_storyboard(self.raw,self.f.record,self.f.cast,self.f.refs)
        saved=design_snapshot(shots[0])
        self.assertEqual(saved['continuity_props'],shots[0]['continuity_props'])
        shots[0]['continuity_props'][0]['appearance']='changed'
        self.assertEqual(saved['continuity_props'][0]['appearance'],PROP['appearance'])

    def test_action_sync_keeps_identity_and_words_without_locking_old_position(self):
        f=staging_fixtures.NativeStagingTest();f.setUp()
        _,shots=native.compile_storyboard(self.raw,self.f.record,self.f.cast,self.f.refs)
        shot=shots[0];before=copy.deepcopy(shot)
        answer=copy.deepcopy(f.result)
        answer['scene']='The rabbit sets the umbrella beside the window and steps back.'
        answer['opening']='雨伞在窗边，小兔站在一旁。'
        answer['image_prompt']='雨伞在窗边，小兔站在一旁。'
        ask=Mock(return_value=answer)
        updated,_=staging.refresh({},'水彩',shot,self.f.refs,basis='action',
            action='小兔把伞放到窗边。',image_prompt='',ask=ask)
        self.assertEqual(ask.call_args.args[1]['continuity_props'],before['continuity_props'])
        self.assertEqual(updated['native_dialogue']['turns'],before['native_dialogue']['turns'])
        self.assertIn('beside the window',updated['native_dialogue']['scene'])
        self.assertIn(PROP['appearance_en'],updated['native_dialogue']['scene'])
        self.assertEqual(props.apply(updated,updated['continuity_props']),updated)
        self.assertEqual(shot,before)
