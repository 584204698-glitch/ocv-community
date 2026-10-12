"""Exercise actual director requests with a local callback, never a provider.

These verify frame contracts and declared phase structure, not model semantics,
pixel recognition, visual quality or character identity coverage.
"""
import copy
import unittest
from unittest.mock import Mock

from backend.app.video_agents import (
    design_core_images, direct_motion, plan_groups, write_image_prompts, write_video_prompts,
)
from backend.app.video_director_contracts import (
    MOTION_CONTRACT, OPENING_FRAME_GUIDE, SINGLE_REFERENCE_GUIDE, STORY_OPENING_FRAME_CONTRACT,
    STORY_NARRATION_AUDIO_RULE,
    TEXT_CONTRACT, VISUAL_CONTRACT, frame_text_contract, motion_contract,
    opening_frame_plan_issues, reference_guide, story_frame_contract,
    story_uses_opening_frame, visual_contract,
)


def context(status='current', *, text_mode='text_assisted'):
    return {'story_handoff': {'status': status, 'story': '米粒写完信，小鹿才从树后走出来。',
                              'brief': {'idea': '米粒无外耳，穿蓝背心。小鹿不得提前入场。'}},
            'video_direction': {'dynamic_text_mode': text_mode}}


def motion(reference_beat=1):
    return {'version': 2, 'scene_anchor': '窗边到门口', 'participants': ['米粒', '小鹿'],
            'beats': [{'action': '米粒写完信，合上信纸。', 'texts': []},
                      {'action': '米粒走到门边；小鹿此时才从树后出来，接过米粒的信。', 'texts': []}],
            'reference_beat': reference_beat,
            'reference_visual': '无外耳的米粒穿蓝背心，独自在窗边写信。',
            'reference_participants': ['米粒'], 'reference_texts': []}


def scenes():
    return [{'slide_id': 's1', 'text': '米粒在窗边写信。', 'start': 0, 'end': 4},
            {'slide_id': 's2', 'text': '写完以后，小鹿才从树后走出来。', 'start': 4, 'end': 8}]


def shot():
    return {'id': 'shot1', 'kind': 'video', 'slide_ids': ['s1', 's2'], 'duration': 8, 'generation_duration': 8,
            'intent': '写完信之后才相见', 'motion_basis': '从写信到见面需要依次展开',
            'progression_plan': '先独自写信，后相见交信', 'source_subtitles': scenes(),
            'visual_description': '无外耳的米粒穿蓝背心，独自在窗边写信。', 'visual_design': {},
            'reference_ids': [], 'motion_plan': motion(), 'action': '米粒写信，小鹿后入场。'}


def video_result():
    return {'shots': [{'id': 'shot1', 'continuity_prompt': '米粒无外耳，蓝背心；保持窗边场景。',
                       'beat_prompts': [{'beat': 1, 'prompt': '米粒独自在窗边写信。'},
                                        {'beat': 2, 'prompt': '小鹿此时才从树后出来，接过米粒的信。'}],
                       'ending_prompt': '米粒与小鹿站在门口自然停留。'}]}


class Capture:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, system, payload):
        self.calls.append((system, copy.deepcopy(payload)))
        if not self.results:
            raise AssertionError('Unexpected extra model request')
        return copy.deepcopy(self.results.pop(0))


class FrameContractSelectionTest(unittest.TestCase):
    def test_only_current_and_legacy_adopted_sources_use_real_opening_frame(self):
        for status in ('current', 'legacy_body_only'):
            self.assertTrue(story_uses_opening_frame(context(status)))
            self.assertEqual(reference_guide(context(status)), OPENING_FRAME_GUIDE)
        for value in ({}, None, {'story_handoff': None}, context('detached'), context('unknown')):
            self.assertFalse(story_uses_opening_frame(value))
            self.assertEqual(story_frame_contract(value), '')
            self.assertEqual(reference_guide(value), SINGLE_REFERENCE_GUIDE)

    def test_legacy_contracts_are_unchanged_and_story_contracts_are_not_contradictory(self):
        self.assertEqual(visual_contract({}), VISUAL_CONTRACT)
        self.assertEqual(motion_contract({}), MOTION_CONTRACT)
        self.assertEqual(frame_text_contract({}, TEXT_CONTRACT), TEXT_CONTRACT)
        self.assertIn('画出随后才依次出现的必要素材', VISUAL_CONTRACT)
        self.assertNotIn('画出随后才依次出现的必要素材', visual_contract(context()))
        self.assertIn('它只是时间基准，不限制核心图能包含的素材', MOTION_CONTRACT)
        self.assertNotIn('它只是时间基准，不限制核心图能包含的素材', motion_contract(context()))
        self.assertNotIn('核心图可以收纳多个阶段的素材', frame_text_contract(context(), TEXT_CONTRACT))
        self.assertIn('reference_beat 必须为1', motion_contract(context()))
        self.assertIn('不自动证明它们是人物', story_frame_contract(context()))

    def test_phase_fields_are_checked_without_inventing_character_presence(self):
        valid = motion()
        self.assertEqual(opening_frame_plan_issues(context(), valid), [])
        invalid = motion(reference_beat=2)
        self.assertTrue(opening_frame_plan_issues(context(), invalid))
        self.assertEqual(opening_frame_plan_issues({}, invalid), [])
        self.assertEqual(invalid['reference_beat'], 2)
        # A declared list is not proof of when anyone enters. The structural
        # validator must not manufacture an appearance declaration or coverage.
        self.assertNotIn('video_character_review', valid)
        self.assertNotIn('character_coverage', valid)
        self.assertNotIn('presence', valid)

    def test_later_visible_text_cannot_be_packed_into_opening_frame(self):
        plan = motion()
        later = {'text': '到了', 'owner': '小鹿', 'container': '对话气泡'}
        plan['beats'][1]['texts'] = [later]
        plan['reference_texts'] = [later]
        self.assertIn('第一阶段未声明', '；'.join(opening_frame_plan_issues(context(), plan)))


class StoryDirectorRequestTest(unittest.TestCase):
    def assert_handoff(self, capture, expected_context):
        for system, payload in capture.calls:
            self.assertEqual(payload['story_context']['story_handoff'], expected_context['story_handoff'])
            self.assertIn(STORY_OPENING_FRAME_CONTRACT, system)
            self.assertIn('米粒无外耳', payload['story_context']['story_handoff']['brief']['idea'])

    def test_group_planner_receives_exact_story_handoff(self):
        ctx = context()
        row = {key: shot()[key] for key in ('id', 'kind', 'slide_ids', 'intent', 'motion_basis', 'progression_plan')}
        ask = Capture({'shots': [row]})
        result = plan_groups(ctx, scenes(), {}, ask=ask)
        self.assertEqual(result[0]['slide_ids'], ['s1', 's2'])
        self.assert_handoff(ask, ctx)

    def test_core_design_uses_opening_only_and_drops_composite_teaching_example(self):
        for mode in ('text_assisted', 'visual_first'):
            with self.subTest(mode=mode):
                ctx = context(text_mode=mode)
                original = copy.deepcopy(ctx)
                ask = Capture({'shots': [{'id': 'shot1', 'visual_description': shot()['visual_description'],
                                         'visual_design': {}, 'reference_ids': []}]})
                result = design_core_images(ctx, scenes(), [shot()], [], ask=ask)
                self.assert_handoff(ask, ctx)
                self.assertNotIn('method_example', ask.calls[0][1])
                self.assertNotIn('画出随后才依次出现的必要素材', ask.calls[0][0])
                self.assertNotIn('小鹿', result[0]['visual_description'])
                self.assertEqual(ctx, original)

    def test_motion_director_separates_whole_shot_and_opening_subjects(self):
        ctx = context()
        original = shot()
        before = copy.deepcopy(original)
        ask = Capture({'shots': [{'id': 'shot1', 'motion_plan': motion()}]})
        result = direct_motion(ctx, [original], [], ask=ask)
        self.assert_handoff(ask, ctx)
        self.assertNotIn('method_example', ask.calls[0][1])
        self.assertIn('真实开场首帧', ask.calls[0][1]['shots'][0]['numbered_references'][0]['purpose'])
        self.assertNotIn('图中复合素材不要求在视频里同时存在', ask.calls[0][0])
        self.assertEqual(result[0]['motion_plan']['participants'], ['米粒', '小鹿'])
        self.assertEqual(result[0]['motion_plan']['reference_participants'], ['米粒'])
        self.assertNotIn('video_character_review', result[0])
        self.assertEqual(original, before)

    def test_nonopening_phase_gets_one_explicit_repair_without_silent_relabeling(self):
        wrong = motion(reference_beat=2)
        ask = Capture({'shots': [{'id': 'shot1', 'motion_plan': wrong}]},
                      {'shots': [{'id': 'shot1', 'motion_plan': motion()}]})
        result = direct_motion(context(), [shot()], [], ask=ask)
        self.assertEqual(len(ask.calls), 2)
        self.assertIn('reference_beat 必须为 1', str(ask.calls[1][1]['validation_errors']))
        self.assertEqual(ask.calls[1][1]['previous_result']['shots'][0]['motion_plan']['reference_beat'], 2)
        self.assertEqual(result[0]['motion_plan']['reference_beat'], 1)
        self.assertEqual(wrong['reference_beat'], 2)

    def test_repeated_nonopening_phase_is_rejected(self):
        response = {'shots': [{'id': 'shot1', 'motion_plan': motion(reference_beat=2)}]}
        ask = Capture(response, response)
        with self.assertRaisesRegex(ValueError, 'reference_beat 必须为 1'):
            direct_motion(context(), [shot()], [], ask=ask)
        self.assertEqual(len(ask.calls), 2)

    def test_image_finalizer_prompt_and_payload_use_true_opening(self):
        ctx = context()
        ask = Capture({'shots': [{'id': 'shot1', 'image_sections': {
            'characters_and_style': '米粒无外耳，蓝背心。',
            'scene': '米粒独自在窗边写信。', 'constraints': '开场只出现米粒。',
        }}]})
        result = write_image_prompts(ctx, '水彩', [shot()], [], ask=ask)
        self.assert_handoff(ask, ctx)
        system = ask.calls[0][0]
        for invalid in ('核心图不是强制首帧', '允许有归属的复合参考信息', '核心图可以收纳多个阶段的素材'):
            self.assertNotIn(invalid, system)
        self.assertNotIn('小鹿', result[0]['image_prompt'])

    def test_video_finalizer_uses_correct_guide_and_honest_identity_gap_notice(self):
        ctx = context()
        ask = Capture(video_result())
        result = write_video_prompts(ctx, [shot()], [], ask=ask)
        self.assert_handoff(ask, ctx)
        self.assertNotIn('可能包含跨阶段的素材', ask.calls[0][0])
        self.assertTrue(result[0]['video_prompt'].startswith(OPENING_FRAME_GUIDE))
        self.assertNotIn(SINGLE_REFERENCE_GUIDE, result[0]['video_prompt'])
        notice = '；'.join(result[0]['video_prompt_warnings'])
        self.assertIn('小鹿', notice)
        self.assertIn('不是已验证的后入场人物清单', notice)
        self.assertIn('尚未证明图片身份覆盖', notice)
        self.assertNotIn('video_character_review', result[0])

    def test_uploaded_references_do_not_prove_character_identity_binding(self):
        original = shot()
        original['reference_ids'] = ['actual-asset-1']
        references = [{'id': 'actual-asset-1', 'name': '小鹿', 'file': 'reference.png'}]
        ask = Capture(video_result())
        result = write_video_prompts(context(), [original], references, ask=ask)
        self.assertEqual(ask.calls[0][1]['shots'][0]['numbered_references'][1]['id'], 'actual-asset-1')
        self.assertIn('尚未证明图片身份覆盖', '；'.join(result[0]['video_prompt_warnings']))
        self.assertNotIn('user_confirmed', result[0])

    def test_adopted_story_audio_preserves_environment_and_action_without_generated_music(self):
        for status in ('current', 'legacy_body_only'):
            with self.subTest(status=status):
                ctx = context(status)
                motion_ask = Capture({'shots': [{'id': 'shot1', 'motion_plan': motion()}]})
                direct_motion(ctx, [shot()], [], ask=motion_ask)
                finalizer_ask = Capture(video_result())
                write_video_prompts(ctx, [shot()], [], ask=finalizer_ask)
                for capture in (motion_ask, finalizer_ask):
                    for system, _payload in capture.calls:
                        self.assertIn(STORY_NARRATION_AUDIO_RULE, system)
                        self.assertNotIn('静音', system)

    def test_legacy_and_detached_paths_retain_composite_behavior(self):
        for ctx in ({}, context('detached')):
            with self.subTest(context=ctx):
                ask = Capture({'shots': [{'id': 'shot1', 'motion_plan': motion(reference_beat=2)}]})
                result = direct_motion(ctx, [shot()], [], ask=ask)
                self.assertIn('method_example', ask.calls[0][1])
                self.assertIn('不强制作为首帧', ask.calls[0][1]['shots'][0]['numbered_references'][0]['purpose'])
                self.assertNotIn(STORY_OPENING_FRAME_CONTRACT, ask.calls[0][0])
                self.assertIn('不要求对口型，静音。', ask.calls[0][0])
                self.assertEqual(result[0]['motion_plan']['reference_beat'], 2)
                finalizer = Capture(video_result())
                final = write_video_prompts(ctx, [shot()], [], ask=finalizer)
                self.assertIn('默认静音，不要求逐字对口型。', finalizer.calls[0][0])
                self.assertTrue(final[0]['video_prompt'].startswith(SINGLE_REFERENCE_GUIDE))
                self.assertNotIn('尚未证明图片身份覆盖', '；'.join(final[0]['video_prompt_warnings']))

    def test_invalid_adopted_plan_stops_before_finalizer_model_call(self):
        original = shot()
        original['motion_plan']['reference_beat'] = 2
        ask = Mock(side_effect=AssertionError('Invalid frame plan must stop before a model call'))
        with self.assertRaisesRegex(ValueError, 'reference_beat 必须为 1'):
            write_image_prompts(context(), '水彩', [original], [], ask=ask)
        with self.assertRaisesRegex(ValueError, 'reference_beat 必须为 1'):
            write_video_prompts(context(), [original], [], ask=ask)
        ask.assert_not_called()

    def test_core_reference_ids_must_still_be_real_even_when_names_match(self):
        response = {'shots': [{'id': 'shot1', 'visual_description': shot()['visual_description'],
                               'reference_ids': ['小鹿']}]}
        ask = Capture(response, response)
        with self.assertRaisesRegex(ValueError, '参考素材选择无效'):
            design_core_images(context(), scenes(), [shot()], [], ask=ask)
        self.assertEqual(len(ask.calls), 2)


if __name__ == '__main__':
    unittest.main()
