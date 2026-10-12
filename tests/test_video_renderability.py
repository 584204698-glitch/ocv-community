import copy
import unittest
from unittest.mock import Mock, patch

from backend.app.video_renderability import ensure_renderable, review_designs, staging_contract

CONTEXT = {'story_handoff': {'status': 'current'}}
SHOT = {'id': 'shot1', 'kind': 'video', 'duration': 5, 'slide_ids': ['s1'],
        'intent': '回忆过去', 'visual_description': '两个人影叠在主人公身体上',
        'source_subtitles': [{'text': '她想起了过去。'}], 'reference_ids': []}


class RenderabilityTests(unittest.TestCase):
    def test_native_review_receives_current_listener_transition_without_extra_call(self):
        actor, listener = 'pc_' + '1' * 32, 'pc_' + '2' * 32
        def speaker(identity, position):
            return dict(character_id=identity, name='同名', kind='character',
                        on_screen=True, position=position, presence='opening')
        first = dict(id='current_a', action='发言者坐下，听者仍在门口。',
            native_dialogue=dict(scene='A room.',
                speakers=[speaker(actor, 'At the chair'), speaker(listener, 'At the door')],
                performance_beats=[dict(start=0, end=5,
                    action='The speaker sits. The listener stays at the door.',
                    action_zh='发言者坐下，听者留在门口。')]))
        second = dict(id='current_b', motion_plan=dict(reference_visual='两人已在桌边。'),
            native_dialogue=dict(scene='Both characters at the table.',
                speakers=[speaker(listener, 'At the table'), speaker(actor, 'Seated')],
                performance_beats=[]))
        source = [first, second]; before = copy.deepcopy(source)
        context = {**CONTEXT, 'staging_review_history': [{'shot_sources': [{'id': 'old_a'}]}]}
        ask = Mock(return_value={'reviews': [dict(id=s['id'], problems=[]) for s in source]})
        review_designs(context, source, ask=ask)
        self.assertEqual(ask.call_count, 1)
        edge = ask.call_args.args[1]['native_character_transitions'][0]
        self.assertEqual((edge['from_shot_id'], edge['to_shot_id']), ('current_a', 'current_b'))
        by_id = {c['character_id']: c for c in edge['characters']}
        self.assertEqual(by_id[listener]['previous_start'], 'At the door')
        self.assertEqual(by_id[listener]['next_start'], 'At the table')
        self.assertEqual(by_id[actor]['next_start'], 'Seated')
        submitted_shots = ask.call_args.args[1]['shots']
        self.assertEqual(submitted_shots[0]['native_dialogue']['performance_beats'],
                         first['native_dialogue']['performance_beats'])
        self.assertEqual(edge['next_opening'], '两人已在桌边。')
        submitted_shots[0]['native_dialogue']['performance_beats'][0]['action'] = 'mutated review input'
        self.assertEqual(source, before)

    def test_transition_does_not_treat_narrator_or_reused_name_as_visible_identity(self):
        from backend.app.video_renderability import native_character_transitions
        narrator = dict(character_id='n', name='旁白', kind='narrator', on_screen=False)
        later = dict(character_id='a', name='同名', kind='character', on_screen=True,
                     presence='later', position='Behind the door')
        first = dict(id='a', native_dialogue=dict(speakers=[narrator, later], performance_beats=[]))
        second = dict(id='b', native_dialogue=dict(speakers=[narrator,
            {**later, 'presence': 'opening', 'position': 'Inside'},
            {**later, 'character_id': 'another_project_role'}]))
        edge = native_character_transitions([first, second])[0]
        self.assertEqual([c['character_id'] for c in edge['characters']], ['a'])
        self.assertEqual(edge['characters'][0]['previous_presence'], 'later')
        self.assertEqual(edge['characters'][0]['next_presence'], 'opening')
        self.assertEqual(native_character_transitions([SHOT, SHOT]), [])

    def test_legacy_explainer_does_not_gain_new_paid_review(self):
        ask = Mock()
        self.assertEqual(ensure_renderable({}, [], [SHOT], [], ask=ask), [SHOT])
        ask.assert_not_called()
        self.assertEqual(staging_contract({}), '')

    def test_incomplete_or_unsupported_review_never_passes(self):
        for value in ({}, {'reviews': []}, {'reviews': [{'id': 'wrong', 'problems': []}]},
                      {'reviews': [{'id': 'shot1', 'problems': [{'code': 'looks_bad'}]}]}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                review_designs(CONTEXT, [SHOT], ask=Mock(return_value=value))

    def test_simple_scene_can_pass_without_rewriting_source(self):
        ask = Mock(return_value={'reviews': [{'id': 'shot1', 'problems': []}]})
        before = copy.deepcopy(SHOT)
        result = ensure_renderable(CONTEXT, [], [SHOT], [], ask=ask)
        self.assertEqual(result, [before]); self.assertEqual(SHOT, before)
        self.assertEqual(ask.call_count, 1)

    def test_identity_conflict_stops_before_faking_child_from_adult(self):
        ask = Mock(return_value={'reviews': [{'id': 'shot1', 'problems': [
            {'code': 'identity_conflict', 'evidence': '童年沿用35岁成人婚纱参考', 'repair': '先设计童年版本'}]}]})
        with patch('backend.app.video_agents.design_core_images') as core:
            with self.assertRaisesRegex(ValueError, '出图前停止'):
                ensure_renderable(CONTEXT, [], [SHOT], [], ask=ask)
            core.assert_not_called()

    def test_one_automatic_redesign_then_independent_recheck(self):
        problem = {'id': 'shot1', 'problems': [{'code': 'duplicate_staging',
                   'evidence': '身旁叠加了同一人的过去身影', 'repair': '独立呈现回忆，只有一个实体'}]}
        ask = Mock(side_effect=[{'reviews': [problem]}, {'reviews': [{'id': 'shot1', 'problems': []}]}])
        fixed = {'id': 'shot1', 'visual_description': '她独自坐在窗边', 'reference_ids': []}
        with patch('backend.app.video_agents.design_core_images', return_value=[fixed]) as core, \
             patch('backend.app.video_agents.direct_motion', return_value=[{'id': 'shot1', 'motion_plan': {}, 'action': '她望向窗外'}]):
            result = ensure_renderable(CONTEXT, [], [SHOT], [], ask=ask)
        self.assertEqual(result[0]['visual_description'], fixed['visual_description'])
        self.assertEqual(result[0]['source_subtitles'], SHOT['source_subtitles'])
        self.assertEqual(result[0]['slide_ids'], SHOT['slide_ids'])
        self.assertEqual(result[0]['duration'], 5)
        self.assertIn('renderability_repairs', core.call_args.args[0])

    def test_second_failure_stops_instead_of_repeated_paid_redesign(self):
        failure = {'reviews': [{'id': 'shot1', 'problems': [{'code': 'contact_ambiguity',
                   'evidence': '同一只手同时握手并拿杯', 'repair': '先放杯，再握手'}]}]}
        ask = Mock(return_value=failure)
        with patch('backend.app.video_agents.design_core_images', return_value=[SHOT]) as core, \
             patch('backend.app.video_agents.direct_motion', return_value=[{'id': 'shot1', 'motion_plan': {}, 'action': '放杯'}]):
            with self.assertRaises(ValueError):
                ensure_renderable(CONTEXT, [], [SHOT], [], ask=ask)
        self.assertEqual(core.call_count, 1); self.assertEqual(ask.call_count, 2)


if __name__ == '__main__': unittest.main()
