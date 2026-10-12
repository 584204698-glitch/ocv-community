"""Project identity reaches actual director requests without claiming pixels.

All provider responses are local doubles. These tests establish ID authority,
reference selection, phase handoff and invalidation, not visual consistency.
"""
import copy
import hashlib
import json
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

import story_agents
from backend.app import video_agents as agents
from backend.app.video_cast_planning import bind_planned_characters, validate_planning_cast
from backend.app.video_director_contracts import (
    PROJECT_CAST_CONTRACT, STORY_OPENING_FRAME_CONTRACT, story_uses_opening_frame,
)
from backend.app.video_plan import edit_structure, normalize_shots, plan_storyboard, planning_fingerprint
from tests.test_story_frame_contracts import Capture, motion, scenes, shot, video_result


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def cast_snapshot(*, changed=False, same_names=False, count=2):
    value = {'version': 1, 'source': 'user_confirmed_project_identity', 'project_id': 'project-1',
             'characters': []}
    for index in range(count):
        value['characters'].append({
            'project_character_id': 'pc_' + f'{index + 1:032x}',
            'name': '米粒' if same_names or index == 0 else '小鹿' + (str(index) if index > 1 else ''),
            'reference_id': f'ref-{index + 1}',
            'constraints': ('无外耳，蓝背心，黄围巾。' if index == 0 else '保留鹿角和绿色衣服。')
                           + ('新衣服要求。' if changed and index == 0 else ''),
            'reference_sha256': digest(['pixels', index]),
            'identity_sha256': digest(['identity', index, changed if index == 0 else False]),
        })
    value['cast_sha256'] = digest(value)
    return validate_planning_cast(value)


def context(cast=None):
    return {'project_cast': cast or cast_snapshot(),
            'video_direction': {'dynamic_text_mode': 'text_assisted', 'max_core_character_references': 3}}


def references(cast=None):
    return [{'id': row['reference_id'], 'name': row['name'], 'file': row['name'] + '.png'}
            for row in (cast or cast_snapshot())['characters']]


def declarations(cast=None):
    rows = (cast or cast_snapshot())['characters']
    return [{'project_character_id': rows[0]['project_character_id'], 'presence': 'opening',
             'basis': '米粒在窗边写信。'},
            {'project_character_id': rows[1]['project_character_id'], 'presence': 'later',
             'basis': '写完以后，小鹿才从树后走出来。'}]


def planned_shot(cast=None):
    cast = cast or cast_snapshot()
    value = shot()
    value['reference_ids'] = ['ref-1']
    value['cast_plan'] = bind_planned_characters(cast, declarations(cast))
    value['image_prompt'] = '米粒无外耳，穿蓝背心，独自在窗边写信。'
    value['video_prompt'] = '图1为真实首帧。米粒写完信，小鹿才入场。'
    return value


def core_result(cast=None):
    return {'shots': [{'id': 'shot1', 'intent': shot()['intent'],
                      'visual_description': shot()['visual_description'], 'visual_design': {},
                      'reference_ids': [], 'planned_characters': declarations(cast)}]}


class AgentZeroCastTest(unittest.TestCase):
    def test_exact_cast_survives_initial_and_identity_repair_provider_calls(self):
        cast = cast_snapshot(same_names=True)
        ambiguous = {'characters': [{'character_id': 'm1', 'name': '人物', 'role': '第一人'},
                                    {'character_id': 'm2', 'name': '人物', 'role': '第二人'}],
                     'locations': [], 'continuity_rules': [],
                     'project_cast': {'source': 'model invented'}}
        repaired = copy.deepcopy(ambiguous)
        repaired['characters'][0]['name'] = '第一人'
        repaired['characters'][1]['name'] = '第二人'
        with patch.object(story_agents, 'gemini_configured', return_value=True), patch.object(
                story_agents, 'generate_gemini_text', side_effect=[
                    json.dumps(ambiguous), json.dumps(repaired)]) as provider:
            result = story_agents.create_story_context('米粒写信，小鹿后来入场。', project_cast=cast,
                                                       require_ai_success=True)
        self.assertEqual(provider.call_count, 2)
        for call in provider.call_args_list:
            data = json.loads(call.kwargs['user_prompt'])
            self.assertEqual(data['project_cast'], cast)
            self.assertIn('不能按同名或文件名猜测', call.kwargs['system_prompt'])
        self.assertEqual(result['project_cast'], cast)
        self.assertIsNot(result['project_cast'], cast)
        self.assertEqual([row['character_id'] for row in result['characters']], ['m1', 'm2'])
        self.assertEqual([row['name'] for row in result['project_cast']['characters']], ['米粒', '米粒'])

    def test_invalid_snapshot_stops_before_provider(self):
        value = cast_snapshot()
        value['characters'][0]['constraints'] = '私自改造型'
        with patch.object(story_agents, 'generate_gemini_text') as provider:
            with self.assertRaisesRegex(ValueError, '快照已变化'):
                story_agents.create_story_context('米粒写信。', project_cast=value)
        provider.assert_not_called()

    def test_cast_changes_context_and_plan_fingerprints_without_changing_legacy_basis(self):
        text = '米粒写信。'
        legacy = {'content_mode': 'urban_suspense', 'full_text': text, 'global_character_prompt': '',
                  'world_prompt': '', 'agent0_prompt': hashlib.sha1(b'').hexdigest(),
                  'version': story_agents.STORY_CONTEXT_VERSION}
        # Preserve historical insertion order and JSON serialization exactly.
        expected = hashlib.sha256(json.dumps(legacy, ensure_ascii=False,
                                            separators=(',', ':')).encode('utf-8')).hexdigest()
        self.assertEqual(story_agents.story_context_fingerprint(text, 'story'), expected)
        a, b = cast_snapshot(), cast_snapshot(changed=True)
        self.assertNotEqual(story_agents.story_context_fingerprint(text, project_cast=a),
                            story_agents.story_context_fingerprint(text, project_cast=b))
        self.assertNotEqual(planning_fingerprint(scenes(), '', '', '', references(), {'project_cast': a}),
                            planning_fingerprint(scenes(), '', '', '', references(), {'project_cast': b}))


class DirectorCastRequestTest(unittest.TestCase):
    def assert_requests(self, capture, ctx):
        for system, payload in capture.calls:
            self.assertIn(PROJECT_CAST_CONTRACT, system)
            self.assertEqual(payload['story_context']['project_cast'], ctx['project_cast'])

    def test_cast_without_adoption_uses_true_opening_and_no_composite_example(self):
        ctx = context()
        ask = Capture(core_result())
        result = agents.design_core_images(ctx, scenes(), [shot()], references(), ask=ask)
        self.assertTrue(story_uses_opening_frame(ctx))
        self.assert_requests(ask, ctx)
        self.assertIn(STORY_OPENING_FRAME_CONTRACT, ask.calls[0][0])
        self.assertNotIn('画出随后才依次出现的必要素材', ask.calls[0][0])
        self.assertNotIn('method_example', ask.calls[0][1])
        self.assertEqual(result[0]['reference_ids'], ['ref-1'])
        self.assertEqual(result[0]['cast_plan'], planned_shot()['cast_plan'])
        self.assertEqual(result[0]['cast_plan']['source'], 'director_plan')
        self.assertNotIn('video_character_review', result[0])
        self.assertNotIn('user_confirmed', result[0])

    def test_actual_group_and_boundary_review_requests_receive_same_cast(self):
        rows = [{'id': 'first', 'slide_ids': ['s1'], 'kind': 'video', 'intent': '先写信'},
                {'id': 'second', 'slide_ids': ['s2'], 'kind': 'video', 'intent': '后见面'}]
        for row in rows:
            row.update(motion_basis='原文动作依次发生', progression_plan=row['intent'])
        ask = Capture({'shots': rows}, {'moves': [], 'repartitions': []})
        result = agents.plan_groups(context(), scenes(), {}, ask=ask, boundary_review=True)
        self.assertEqual(len(result), 2)
        self.assertEqual(len(ask.calls), 2)
        self.assert_requests(ask, context())

    def test_actual_group_repair_request_keeps_cast_authority(self):
        initial = {'id': 'shot1', 'slide_ids': ['s1', 's2'], 'kind': 'video', 'intent': '先写信后见面'}
        repaired = {**initial, 'motion_basis': '写信后相见需要过程', 'progression_plan': '先写信后见面',
                    'semantic': {'message': '先写信后见面', 'source_basis': '米粒在窗边写信。', 'fact_status': 'fact'}}
        ask = Capture({'shots': [initial]}, {'shots': [repaired]})
        agents.plan_groups(context(), scenes(), {}, ask=ask)
        self.assertEqual(len(ask.calls), 2)
        self.assertIn('Agent 1B', ask.calls[1][0])
        self.assert_requests(ask, context())

    def test_later_or_undeclared_reference_has_one_bounded_correction(self):
        for declared in (declarations(), declarations()[:1]):
            wrong = core_result()
            wrong['shots'][0].update(reference_ids=['ref-2'], planned_characters=declared)
            ask = Capture(wrong, core_result())
            result = agents.design_core_images(context(), scenes(), [shot()], references(), ask=ask)
            self.assertEqual(len(ask.calls), 2)
            self.assertIn('不能作为开场核心图', str(ask.calls[1][1]['validation_errors']))
            self.assertEqual(result[0]['reference_ids'], ['ref-1'])
            self.assert_requests(ask, context())

    def test_persistently_invalid_character_plans_fail_after_one_correction(self):
        bad_lists = [None, {}, [dict(declarations()[0], project_character_id='pc_' + 'f' * 32)],
                     [dict(declarations()[0], presence='user_confirmed')],
                     [dict(declarations()[0], in_last_frame=True)],
                     [declarations()[0], declarations()[0]]]
        for value in bad_lists:
            with self.subTest(value=value):
                response = core_result()
                response['shots'][0]['planned_characters'] = value
                ask = Capture(response, response)
                with self.assertRaises(ValueError):
                    agents.design_core_images(context(), scenes(), [shot()], references(), ask=ask)
                self.assertEqual(len(ask.calls), 2)

    def test_missing_plan_is_not_an_explicit_empty_cast(self):
        missing = core_result()
        missing['shots'][0].pop('planned_characters')
        empty = copy.deepcopy(missing)
        empty['shots'][0]['planned_characters'] = []
        ask = Capture(missing, empty)
        result = agents.design_core_images(context(), scenes(), [shot()], references(), ask=ask)
        self.assertEqual(len(ask.calls), 2)
        self.assertEqual(result[0]['cast_plan']['characters'], [])
        self.assertEqual(result[0]['reference_ids'], [])

    def test_model_supplied_cast_authority_is_rebuilt_and_legacy_forgery_removed(self):
        response = core_result()
        response['shots'][0]['cast_plan'] = {'source': 'user_confirmed', 'in_last_frame': True}
        result = agents.design_core_images(context(), scenes(), [shot()], references(), ask=Capture(response))
        self.assertEqual(result[0]['cast_plan'], planned_shot()['cast_plan'])
        legacy = agents.design_core_images({}, scenes(), [shot()], [], ask=Capture(response))
        self.assertNotIn('cast_plan', legacy[0])

    def test_same_names_and_filenames_cannot_select_another_binding(self):
        cast = cast_snapshot(same_names=True)
        response = core_result(cast)
        response['shots'][0]['planned_characters'] = [declarations(cast)[0]]
        result = agents.design_core_images(context(cast), scenes(), [shot()], references(cast), ask=Capture(response))
        self.assertEqual(result[0]['reference_ids'], ['ref-1'])
        wrong = copy.deepcopy(response)
        wrong['shots'][0]['reference_ids'] = ['ref-2']
        with self.assertRaisesRegex(ValueError, '不能作为开场核心图'):
            agents.design_core_images(context(cast), scenes(), [shot()], references(cast), ask=Capture(wrong, wrong))

    def test_opening_asset_must_exist_in_actual_reference_catalog(self):
        ask = Capture(core_result(), core_result())
        with self.assertRaisesRegex(ValueError, '参考素材选择无效'):
            agents.design_core_images(context(), scenes(), [shot()], [], ask=ask)
        self.assertEqual(len(ask.calls), 2)

    def test_reserved_scene_capacity_is_checked_after_adding_opening_assets(self):
        cast = cast_snapshot(count=4)
        response = core_result(cast)
        response['shots'][0]['planned_characters'] = [
            {'project_character_id': row['project_character_id'], 'presence': 'opening', 'basis': '四人同时在场。'}
            for row in cast['characters']]
        ask = Capture(response, response)
        with self.assertRaisesRegex(ValueError, '3 张上限'):
            agents.design_core_images(context(cast), scenes(), [shot()], references(cast), ask=ask)
        self.assertEqual(len(ask.calls), 2)
        self.assertEqual(len(ask.calls[1][1]['previous_result']['shots'][0]['planned_characters']), 4)
        ctx = context(cast)
        ctx['video_direction']['max_core_character_references'] = 4
        result = agents.design_core_images(ctx, scenes(), [shot()], references(cast), ask=Capture(response))
        self.assertEqual(result[0]['reference_ids'], ['ref-1', 'ref-2', 'ref-3', 'ref-4'])

    def test_motion_and_finalizers_receive_canonical_plan_without_inventing_later_picture_numbers(self):
        ctx, original = context(), planned_shot()
        responses = [Capture({'shots': [{'id': 'shot1', 'motion_plan': motion()}]}),
                     Capture({'shots': [{'id': 'shot1', 'image_sections': {
                         'characters_and_style': '米粒无外耳，蓝背心。', 'scene': '米粒独自写信。', 'constraints': ''}}]}),
                     Capture(video_result())]
        agents.direct_motion(ctx, [original], references(), ask=responses[0])
        agents.write_image_prompts(ctx, '水彩', [original], references(), ask=responses[1])
        agents.write_video_prompts(ctx, [original], references(), ask=responses[2])
        for capture in responses:
            self.assert_requests(capture, ctx)
            sent = capture.calls[0][1]['shots'][0]
            self.assertEqual(sent['cast_plan'], original['cast_plan'])
            if 'numbered_references' in sent:
                self.assertEqual([row.get('id') for row in sent['numbered_references']], [None, 'ref-1'])
            self.assertNotIn('video_character_review', sent)

    def test_stale_missing_or_tampered_plans_stop_all_downstream_provider_calls(self):
        for problem in ('old_identity', 'missing_plan', 'later_reference', 'missing_reference', 'missing_cast'):
            with self.subTest(problem=problem):
                ctx, original = context(), planned_shot()
                if problem == 'old_identity':
                    ctx = context(cast_snapshot(changed=True))
                elif problem == 'missing_plan':
                    original.pop('cast_plan')
                elif problem == 'later_reference':
                    original['reference_ids'] = ['ref-1', 'ref-2']
                elif problem == 'missing_reference':
                    original['reference_ids'] = []
                else:
                    ctx = {}
                provider = Mock(side_effect=AssertionError('Must stop before any provider call'))
                for run in (lambda: agents.direct_motion(ctx, [original], references(), ask=provider),
                            lambda: agents.write_image_prompts(ctx, '', [original], references(), ask=provider),
                            lambda: agents.write_video_prompts(ctx, [original], references(), ask=provider)):
                    with self.assertRaises(ValueError):
                        run()
                provider.assert_not_called()


class CastPlanLifecycleTest(unittest.TestCase):
    def test_normalize_preserves_cast_and_structure_edits_invalidate_affected_plans(self):
        original = planned_shot()
        normalized = normalize_shots([original], scenes(), ['ref-1', 'ref-2'])
        self.assertEqual(normalized[0]['cast_plan'], original['cast_plan'])
        split = edit_structure(normalized, scenes(), 'split', 0, boundary=1, reference_ids=['ref-1', 'ref-2'])
        for row in split:
            self.assertNotIn('cast_plan', row)
            self.assertTrue(row['design_needs_review'])
        for row in split:
            row['cast_plan'] = copy.deepcopy(original['cast_plan'])
        merged = edit_structure(split, scenes(), 'merge', 0, reference_ids=['ref-1', 'ref-2'])
        self.assertNotIn('cast_plan', merged[0])
        three = [*scenes(), {'slide_id': 's3', 'start': 8, 'end': 10, 'text': '两人站在门口。'}]
        first, second = copy.deepcopy(original), copy.deepcopy(original)
        second.update(id='shot2', slide_ids=['s3'])
        moved = edit_structure([first, second], three, 'boundary', 0, boundary=1,
                               reference_ids=['ref-1', 'ref-2'])
        self.assertTrue(all('cast_plan' not in row for row in moved))

    def run_planning(self, parameters, *, fixed=None, resume=None):
        cast = parameters.get('project_cast')
        core = planned_shot(cast)
        if cast is None:
            core.pop('cast_plan')
            core['reference_ids'] = []
        drafts = []
        with ExitStack() as stack:
            stack.enter_context(patch('backend.app.video_renderability.review_designs',
                side_effect=lambda context, rows, **kwargs: [{'id': row['id'], 'problems': []} for row in rows]))
            agent0 = stack.enter_context(patch.object(story_agents, 'create_story_context',
                return_value={'generation_source': 'gemini', 'project_cast': {'model': 'forged'}}))
            functions = {}
            for name, result in (
                    ('plan_groups', [shot()]), ('design_core_images', [core]),
                    ('direct_motion', [{'id': 'shot1', 'motion_plan': motion(), 'action': '米粒先写信，小鹿后入场。'}]),
                    ('write_image_prompts', [{'id': 'shot1', 'image_prompt': '米粒独自写信。'}]),
                    ('write_video_prompts', [{'id': 'shot1', 'video_prompt': '图1是真实开场。小鹿后入场。'}])):
                functions[name] = stack.enter_context(patch.object(agents, name, return_value=result))
            result = plan_storyboard(scenes(), '', '', '', references(), lambda _: None,
                parameters=parameters, fixed_shots=fixed, repair_only=fixed is not None,
                resume_state=resume, save_state=drafts.append)
        return result, agent0, functions, drafts

    def test_changed_cast_cannot_skip_design_with_fixed_shots_repair_only(self):
        old = planned_shot()
        before = copy.deepcopy(old)
        new = cast_snapshot(changed=True)
        (ctx, result), zero, calls, _ = self.run_planning({'project_cast': new}, fixed=[old])
        self.assertEqual(old, before)
        calls['plan_groups'].assert_not_called()
        for name in ('design_core_images', 'direct_motion', 'write_image_prompts', 'write_video_prompts'):
            calls[name].assert_called_once()
        self.assertEqual(zero.call_args.kwargs['project_cast'], new)
        self.assertEqual(ctx['project_cast'], new)
        self.assertEqual(result[0]['cast_plan'], planned_shot(new)['cast_plan'])

    def test_removed_cast_also_invalidates_old_plan_without_rebinding(self):
        (ctx, rows), zero, calls, _ = self.run_planning({}, fixed=[planned_shot()])
        self.assertNotIn('project_cast', ctx)
        self.assertNotIn('project_cast', zero.call_args.kwargs)
        self.assertNotIn('cast_plan', rows[0])
        calls['design_core_images'].assert_called_once()

    def test_resumed_context_restores_or_removes_only_authoritative_cast(self):
        for parameters in ({}, {'project_cast': cast_snapshot()}):
            state = {'fingerprint': planning_fingerprint(scenes(), '', '', '', references(), parameters),
                     'context': {'generation_source': 'gemini', 'project_cast': {'forged': True}}}
            (ctx, _), zero, _calls, _ = self.run_planning(parameters, resume=state)
            zero.assert_not_called()
            if parameters:
                self.assertEqual(ctx['project_cast'], parameters['project_cast'])
                self.assertIn('真实开场首帧', ctx['video_direction']['core_image_role'])
            else:
                self.assertNotIn('project_cast', ctx)
            self.assertEqual(state['context']['project_cast'], {'forged': True})

    def test_scene_enabled_capacity_and_narration_lipsync_are_recorded(self):
        for scene_enabled, expected in ((True, 3), (False, 4)):
            (ctx, rows), _zero, _calls, _ = self.run_planning(
                {'project_cast': cast_snapshot(), 'scene_references_enabled': scene_enabled})
            self.assertEqual(ctx['video_direction']['max_core_character_references'], expected)
            self.assertIs(rows[0]['reference_audio_lipsync'], False)


if __name__ == '__main__':
    unittest.main()
