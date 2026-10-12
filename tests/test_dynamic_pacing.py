import copy
import unittest
from unittest.mock import Mock, patch

from backend.app.video_agents import plan_groups
from backend.app.video_pacing import pacing_policy, enforce_pacing, pacing_partitions, exceeds
from backend.app.video_group_repair import _fallback_row, validate_group_structure


def captions(count=10, seconds=3):
    return [dict(slide_id=f's{i}', start=i*seconds, end=(i+1)*seconds,
                 text=f'动作{i}完成。') for i in range(count)]


def group(scenes, identity='parent', dynamic=True):
    return dict(_fallback_row(scenes, {}, dynamic), id=identity)


class DynamicPacingTests(unittest.TestCase):
    def test_legacy_and_auto_keep_semantic_behavior(self):
        for config in ({}, {'visual_pacing_preset':'auto'}, {'visual_pacing_preset':'unknown'}):
            self.assertIsNone(pacing_policy(config))
        policy=pacing_policy({'visual_pacing_preset':'fast','visual_target_duration':30})
        self.assertEqual((policy['min_duration'],policy['target_duration'],policy['max_duration']), (4,6,8))

    def test_custom_normalizes_nonfinite_and_conflicting_bounds(self):
        p=pacing_policy(dict(visual_pacing_preset='custom', visual_min_duration=20,
                            visual_target_duration=float('nan'), visual_max_duration=6, visual_max_slides=99))
        self.assertEqual((p['min_duration'],p['target_duration'],p['max_duration'],p['max_slides']), (15,15,15,12))

    def test_initial_director_receives_selected_pacing(self):
        scenes=captions(4)
        calls=[]
        def ask(system, payload):
            calls.append(payload)
            return {'shots':[group(scenes[:2],'a'),group(scenes[2:],'b')]}
        result=plan_groups({}, scenes, {'visual_pacing_preset':'fast'}, ask=ask)
        self.assertEqual(calls[0]['visual_pacing']['target_duration'],6)
        self.assertEqual(len(calls),1)
        self.assertEqual(len(result),2)

    def test_storyboard_forwards_saved_creation_settings(self):
        import sys
        from types import SimpleNamespace
        from backend.app.video_plan import plan_storyboard
        class ReachedGroups(Exception): pass
        parameters=dict(visual_pacing_preset='custom',visual_min_duration=4,
                        visual_target_duration=6,visual_max_duration=8,visual_max_slides=3)
        reader=SimpleNamespace(create_story_context=Mock(return_value={'characters':[]}))
        with patch.dict(sys.modules,{'story_agents':reader}), patch('backend.app.video_agents.plan_groups',side_effect=ReachedGroups) as planner:
            with self.assertRaises(ReachedGroups):
                plan_storyboard(captions(4),'水彩','','',[],lambda _:None,parameters)
        forwarded=planner.call_args.args[2]
        self.assertEqual({k:forwarded[k] for k in parameters},parameters)

    def test_ignored_pacing_gets_one_repair_then_safe_fallback(self):
        scenes=captions(20)
        parents=[group(scenes[i:i+4],str(i)) for i in range(0,20,4)]
        before=copy.deepcopy((scenes,parents))
        ask=Mock(return_value={'groups':[]})
        result=enforce_pacing({},scenes,parents,pacing_policy({'visual_pacing_preset':'fast'}),ask)
        self.assertEqual(len(result),10)
        self.assertEqual(ask.call_count,1)
        self.assertEqual((scenes,parents),before)
        validate_group_structure(result,scenes)
        self.assertTrue(all(row['kind']=='video' for row in result))
        self.assertTrue(all(row['duration_repair']['method']=='boundary_fallback' for row in result))
        self.assertTrue(all(row['semantic']['source_basis']==''.join(s['text'] for s in scenes if s['slide_id'] in row['slide_ids']) for row in result))

    def test_semantic_repair_keeps_its_valid_child_intents(self):
        scenes=captions(4)
        children=[group(scenes[:2],'a'),group(scenes[2:],'b')]
        children[0]['intent']='观察动作'
        ask=Mock(return_value={'groups':[{'parent_id':'parent','shots':children}]})
        result=enforce_pacing({},scenes,[group(scenes)],pacing_policy({'visual_pacing_preset':'fast'}),ask)
        self.assertEqual(result[0]['intent'],'观察动作')
        self.assertEqual(result[0]['duration_repair']['method'],'semantic')

    def test_boundary_review_cannot_silently_undo_pacing(self):
        scenes=captions(4)
        def ask(system,payload):
            if 'groups' in payload:return {'groups':[]}
            return {'shots':[group(scenes[:2],'a'),group(scenes[2:],'b')]}
        with patch('backend.app.video_boundary_review.review_boundaries',return_value=[group(scenes)]):
            result=plan_groups({},scenes,{'visual_pacing_preset':'fast'},ask=ask,boundary_review=True)
        self.assertEqual(len(result),2)

    def test_single_caption_exception_and_manual_groups_are_preserved(self):
        scenes=captions(2,10)
        rows=[group(scenes[:1],'a'),group(scenes[1:],'b')]
        ask=Mock(side_effect=AssertionError('No call needed'))
        policy=pacing_policy({'visual_pacing_preset':'fast'})
        self.assertEqual(enforce_pacing({},scenes,rows,policy,ask),rows)
        manual=dict(group(scenes),manual_locked=True)
        self.assertEqual(enforce_pacing({},scenes,[manual],policy,ask),[manual])

    def test_fallback_preserves_static_and_overlong_single_caption(self):
        scenes=captions(5,3)
        policy=pacing_policy({'visual_pacing_preset':'fast'})
        rows=enforce_pacing({},scenes,[group(scenes,dynamic=False)],policy,Mock(return_value={}))
        self.assertTrue(all(row['kind']=='static' for row in rows))
        long=captions(1,17)+[dict(slide_id='tail',start=17,end=20,text='后续。')]
        parts=pacing_partitions(long,policy)
        self.assertEqual([len(p) for p in parts],[1,1])

    def test_fallback_varied_timelines_cover_exactly_once(self):
        import random
        rng=random.Random(41)
        for _ in range(50):
            scenes=[];start=0
            for i in range(rng.randint(1,35)):
                end=start+rng.uniform(.4,18)
                scenes.append(dict(slide_id=str(i),start=start,end=end,text='内容。'))
                start=end+rng.uniform(0,.4)
            policy=pacing_policy({'visual_pacing_preset':'fast'})
            parts=pacing_partitions(scenes,policy)
            self.assertEqual([s for part in parts for s in part],scenes)
            self.assertTrue(all(not exceeds(part,policy) for part in parts))


if __name__=='__main__':unittest.main()
