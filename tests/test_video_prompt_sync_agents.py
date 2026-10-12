"""Exercise actual input handoffs locally; no provider, pixels or coverage claims."""
import copy
import unittest
from unittest.mock import Mock, patch

from backend.app import video_agents as agents
from backend.app import video_prompt_refresh as refresh_module
from backend.app.video_director_contracts import (
    COMPOSITION_REFERENCE_GUIDE, FIRST_LAST_GUIDE, OPENING_FRAME_GUIDE,
    REFERENCE_AVAILABILITY_CONTRACT, STORY_OPENING_FRAME_CONTRACT,
)
from backend.app.video_motion_plan import render_motion_action
from tests.test_story_frame_contracts import Capture, motion, video_result
from tests.test_video_cast_director import context, planned_shot, references


STALE = '其身份外观须另行核对已绑定的独立参考，本镜提供的编号参考中没有她的身份图。'


def manifest(mode='composition_reference'):
    rows = [{'id': 'shot1', 'name': '本镜核心图', 'label': '<Picture 1>',
             'role': 'composition' if mode == 'composition_reference' else 'first_frame', 'sha256': 'a' * 64}]
    if mode == 'first_last':
        rows.append({'id': 'tail-1', 'name': '尾帧', 'label': 'last_frame', 'role': 'last_frame', 'sha256': 'b' * 64})
    elif mode == 'composition_reference':
        for index, row in enumerate(context()['project_cast']['characters'], 2):
            rows.append({'id': row['reference_id'], 'name': row['name'], 'label': f'<Picture {index}>',
                         'role': 'identity_reference', 'sha256': row['reference_sha256']})
    return {'input_mode': mode, 'images': rows, 'project_characters': context()['project_cast']['characters']}


def stale_shot():
    value = planned_shot()
    value['motion_plan']['beats'][-1]['action'] += STALE
    value['action'] += STALE
    value['video_prompt'] += STALE
    value['image_prompt'] = '  ' + value['image_prompt'] + '\n'
    return value


class VideoInputPromptAgentsTest(unittest.TestCase):
    def test_actual_numbering_includes_later_identity_without_changing_core_reference_ids(self):
        original = stale_shot()
        snapshot = copy.deepcopy(original)
        actual = manifest()
        rows = agents._numbered_dynamic_shots([original], references(), context(), video_inputs=actual)
        numbered = rows[0]['numbered_references']
        self.assertEqual([row['id'] for row in numbered], ['shot1', 'ref-1', 'ref-2'])
        self.assertEqual([row['number'] for row in numbered], ['图1', '图2', '图3'])
        self.assertEqual([row['label'] for row in numbered], ['<Picture 1>', '<Picture 2>', '<Picture 3>'])
        self.assertEqual(rows[0]['reference_ids'], ['ref-1'])
        self.assertNotIn('video_prompt', rows[0])
        self.assertEqual(original, snapshot)
        self.assertEqual(actual, manifest())

    def test_actual_order_is_authoritative_and_tail_never_becomes_an_identity(self):
        actual = manifest()
        actual['images'][1], actual['images'][2] = actual['images'][2], actual['images'][1]
        for index, row in enumerate(actual['images'], 1):
            row['label'] = f'<Picture {index}>'
        rows = agents._numbered_dynamic_shots([planned_shot()], references(), context(), video_inputs=actual)
        self.assertEqual([row['id'] for row in rows[0]['numbered_references']], ['shot1', 'ref-2', 'ref-1'])
        rows = agents._numbered_dynamic_shots([planned_shot()], references(), context(), video_inputs=manifest('first_last'))
        self.assertEqual(rows[0]['numbered_references'][1]['role'], 'last_frame')
        self.assertEqual(rows[0]['numbered_references'][1]['label'], 'last_frame')
        self.assertIn('不是独立身份图', rows[0]['numbered_references'][1]['purpose'])

    def test_motion_sync_receives_exact_inputs_constraints_and_keeps_reference_design(self):
        original, actual = stale_shot(), manifest()
        generated = motion()
        # A paraphrased reference description is irrelevant to input sync and
        # must never overwrite the existing design or visible-subject claims.
        generated['reference_visual'] = '另一份无意义的模型改写'
        generated['reference_participants'] = ['米粒', '小鹿']
        provider = Capture({'shots': [{'id': 'shot1', 'motion_plan': generated}]})
        result = refresh_module.revise_motion(context(), original, references(), original['action'],
            original['image_prompt'], 'image_prompt', ask=provider, refresh_basis='video_inputs', video_inputs=actual)
        system, payload = provider.calls[0]
        self.assertEqual(payload['video_inputs'], actual)
        self.assertEqual(payload['shot']['reference_ids'], ['ref-1'])
        self.assertEqual([row['id'] for row in payload['shot']['numbered_references']], ['shot1', 'ref-1', 'ref-2'])
        self.assertEqual(payload['story_context']['project_cast'], context()['project_cast'])
        self.assertEqual(payload['existing_action'], original['action'])
        self.assertNotIn('manual_action', payload)
        self.assertNotIn(STORY_OPENING_FRAME_CONTRACT, system)
        self.assertIn(COMPOSITION_REFERENCE_GUIDE, system)
        self.assertIn('只整理每阶段 action', system)
        self.assertIn(REFERENCE_AVAILABILITY_CONTRACT, system)
        self.assertNotIn(STALE, result['beats'][-1]['action'])
        for key in ('scene_anchor', 'participants', 'reference_beat', 'reference_visual',
                    'reference_participants', 'reference_texts'):
            self.assertEqual(result[key], original['motion_plan'][key])
        self.assertNotIn('video_character_review', result)
        self.assertNotIn('user_confirmed', result)

    def test_finalizer_guide_follows_actual_mode_instead_of_story_policy(self):
        for mode, guide in (('composition_reference', COMPOSITION_REFERENCE_GUIDE),
                            ('first_last', FIRST_LAST_GUIDE), ('single_first', OPENING_FRAME_GUIDE)):
            with self.subTest(mode=mode):
                provider = Capture(video_result())
                actual = manifest(mode)
                result = agents.write_video_prompts(context(), [planned_shot()], references(), ask=provider, video_inputs=actual)
                system, payload = provider.calls[0]
                self.assertEqual(payload['video_inputs'], actual)
                self.assertEqual(len(payload['shots'][0]['numbered_references']), len(actual['images']))
                self.assertTrue(result[0]['video_prompt'].startswith(guide))
                self.assertNotIn(STORY_OPENING_FRAME_CONTRACT, system)
                self.assertIn('自然环境/动作声音', system)
                self.assertIn('不生成额外对白或背景音乐', system)
                self.assertNotIn('默认静音', system)
                self.assertNotIn('user_confirmed', result[0])
                self.assertIn('尚未证明图片身份覆盖', '；'.join(result[0]['video_prompt_warnings']))

    def test_refresh_rebuilds_action_and_keeps_images_timeline_and_existing_review_unchanged(self):
        original, actual = stale_shot(), manifest()
        original['video_character_review'] = {'source': 'assistant_review', 'old_basis': 'old'}
        snapshot = copy.deepcopy(original)
        motion_provider = Capture({'shots': [{'id': 'shot1', 'motion_plan': motion()}]})
        final_provider = Capture(video_result())
        real_revise, real_finalize = refresh_module.revise_motion, agents.write_video_prompts
        def revise(*args, **kwargs):
            return real_revise(*args, **kwargs, ask=motion_provider)
        def finalize(*args, **kwargs):
            return real_finalize(*args, **kwargs, ask=final_provider)
        with patch.object(refresh_module, 'revise_motion', side_effect=revise), \
                patch.object(refresh_module, 'write_video_prompts', side_effect=finalize), \
                patch.object(refresh_module, 'analyze_image') as vision, \
                patch.object(refresh_module, 'write_image_prompts') as image_finalizer:
            updated, analysis = refresh_module.refresh(context(), '新画风不生效', original, references(),
                basis='video_inputs', action=original['action'], image_prompt='不能覆盖原图像提示词', video_inputs=actual)
        self.assertIsNone(analysis)
        self.assertEqual(updated['action'], render_motion_action(updated['motion_plan']))
        for text in (updated['action'], updated['video_prompt'], updated['motion_plan']['beats'][-1]['action']):
            self.assertNotIn(STALE, text)
        for key in set(original) - {'motion_plan', 'action', 'video_prompt', 'video_prompt_warnings'}:
            self.assertEqual(updated[key], original[key], key)
        self.assertEqual(original, snapshot)
        self.assertEqual(updated['video_character_review'], snapshot['video_character_review'])
        vision.assert_not_called()
        image_finalizer.assert_not_called()
        self.assertEqual(len(motion_provider.calls), 1)
        self.assertEqual(len(final_provider.calls), 1)
        self.assertNotIn(STALE, final_provider.calls[0][1]['shots'][0]['action'])

    def test_failure_after_motion_keeps_original_complete(self):
        original = stale_shot()
        snapshot = copy.deepcopy(original)
        with patch.object(refresh_module, 'revise_motion', return_value=motion()), \
                patch.object(refresh_module, 'write_video_prompts', side_effect=RuntimeError('provider failed')):
            with self.assertRaisesRegex(RuntimeError, 'provider failed'):
                refresh_module.refresh(context(), '', original, references(), basis='video_inputs',
                    action=original['action'], image_prompt=original['image_prompt'], video_inputs=manifest())
        self.assertEqual(original, snapshot)

    def test_invalid_manifest_stops_before_provider_in_both_agents(self):
        cases = []
        for mutate in (
            lambda data: data.update(input_mode='made_up'),
            lambda data: data['images'][0].update(id='another-shot'),
            lambda data: data['images'][1].update(role='last_frame'),
            lambda data: data['images'][1].update(label='<Picture 3>'),
            lambda data: data['images'][1].update(sha256='unknown'),
            lambda data: data['images'][1].update(id='shot1'),
            lambda data: data.update(project_characters=None),
            lambda data: data.update(images=[]),
        ):
            actual = manifest()
            mutate(actual)
            cases.append(actual)
        for actual in cases:
            provider = Mock(side_effect=AssertionError('invalid input must stop before provider'))
            with self.subTest(actual=actual), self.assertRaises(ValueError):
                agents.write_video_prompts(context(), [planned_shot()], references(), ask=provider, video_inputs=actual)
            with self.subTest(motion=actual), self.assertRaises(ValueError):
                refresh_module.revise_motion(context(), planned_shot(), references(), '原动作', '原图',
                    'image_prompt', ask=provider, refresh_basis='video_inputs', video_inputs=actual)
            provider.assert_not_called()

    def test_sync_rejects_missing_plan_new_phases_changed_characters_or_visible_text(self):
        original = planned_shot()
        original.pop('motion_plan')
        provider = Mock()
        with self.assertRaisesRegex(ValueError, '已有完整阶段方案'):
            refresh_module.revise_motion(context(), original, references(), '原动作', '原图',
                'image_prompt', ask=provider, refresh_basis='video_inputs', video_inputs=manifest())
        provider.assert_not_called()
        for change in ('phase', 'participant', 'text'):
            generated = motion()
            if change == 'phase':
                generated['beats'].append({'action': '新加的事件', 'texts': []})
            elif change == 'participant':
                generated['participants'].append('新角色')
            else:
                generated['beats'][0]['texts'] = [{'text': '新字', 'owner': '米粒', 'container': '气泡'}]
            provider = Capture({'shots': [{'id': 'shot1', 'motion_plan': generated}]})
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, '改变了原有阶段'):
                refresh_module.revise_motion(context(), planned_shot(), references(), '原动作', '原图',
                    'image_prompt', ask=provider, refresh_basis='video_inputs', video_inputs=manifest())

    def test_missing_manifest_and_vision_flags_never_reach_provider(self):
        for kwargs in ({}, {'video_inputs': manifest(), 'force_vision': True},
                       {'video_inputs': manifest(), 'action_only': True}):
            with self.subTest(kwargs=kwargs), patch.object(refresh_module, 'ask_json') as provider, \
                    patch.object(refresh_module, 'analyze_image') as vision, self.assertRaises(ValueError):
                refresh_module.refresh(context(), '', planned_shot(), references(), basis='video_inputs',
                    action='原动作', image_prompt='原图', **kwargs)
            provider.assert_not_called()
            vision.assert_not_called()

    def test_original_planning_numbering_and_contract_are_retained(self):
        original = planned_shot()
        provider = Capture(video_result())
        result = agents.write_video_prompts(context(), [original], references(), ask=provider)
        system, payload = provider.calls[0]
        self.assertNotIn('video_inputs', payload)
        self.assertEqual([row.get('id') for row in payload['shots'][0]['numbered_references']], [None, 'ref-1'])
        self.assertIn(STORY_OPENING_FRAME_CONTRACT, system)
        self.assertIn(REFERENCE_AVAILABILITY_CONTRACT, system)
        self.assertTrue(result[0]['video_prompt'].startswith(OPENING_FRAME_GUIDE))
        self.assertEqual(original['reference_ids'], ['ref-1'])


if __name__ == '__main__':
    unittest.main()
