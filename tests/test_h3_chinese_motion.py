import unittest

from backend.app import h3_prompt_agent as agent


def draft(mode='composition_reference'):
    body = (
        '[Shot 1] 沿用 <Picture 1> 的蜡笔绘本画风、日光、道路和开场位置。'
        '身份取自 <Picture 2> 的 <Subject 1> 是白兔。'
        '固定机位，兔子从画面中部沿路跑到右侧，身体整体越过路边已有的小石头，'
        '每次落脚都有实际前进，身后的树保持原位，与树的距离持续扩大。'
        '随后兔子在新位置减速停下，回头看向后方，再转回并抬起前爪，无声地露出自信的微笑。'
        '嘴部保持自然放松，不做连续说话口型，不恢复开场位置。'
        '保留脚步接触地面的细碎声，脚步声随减速渐慢。'
    )
    if mode != 'composition_reference':
        body = body.replace('身份取自 <Picture 2> 的 <Subject 1> 是白兔。', '图中的白兔保持相同外貌。')
        if mode == 'first_last':
            body += '最终在结束时到达 <Picture 2> 所示的停步状态。'
        return f'integrated_multimodal_description:\n{body}\n\noverall_soundscape:\n脚步接触地面的细碎声。\n\nnon_diegetic_music:\nN/A'
    return (
        'subject_definitions:\n<Picture 1> 是画风与场景参考。\n'
        '<Subject 1> 是兔子，身份依据 <Picture 2>：白色长耳兔，保留原有造型。\n\n'
        'summary:\n兔子沿路前进并停步回头，随后无声地抬爪示意。\n\n'
        'retention_analysis:\n图片提供身份和画风，不是严格首帧，不要求像素锁定。\n\n'
        f'detailed_description:\n{body}\n\noverall_soundscape:\n脚步接触地面的细碎声。\n\nnon_diegetic_music:\nN/A'
    )


class ChineseMotionTests(unittest.TestCase):
    def test_continuous_mouth_action_in_negative_output_list(self):
        for good in ('不生成人声、连续说话口型或背景音乐。',
                     '全程无对白、连续说话口型。'):
            agent._assert_no_generated_audio(good)
        for bad in ('不生成人声、连续说出介绍词。',
                    '不生成人声、姐姐连续说话。',
                    '不生成人声、连续说话口型，但姐姐开口回答。',
                    '不能不生成人声、连续说话口型。'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                agent._assert_no_generated_audio(bad)

    def test_all_input_modes_preserve_chinese_actions_and_sound(self):
        for mode in ('composition_reference', 'single_first', 'first_last'):
            with self.subTest(mode=mode):
                assets = [{'picture_label': '<Picture 1>'}]
                if mode != 'single_first':
                    assets.append({'picture_label': '<Picture 2>', 'subject_label': '<Subject 1>'})
                result = agent._validate(draft(mode), 11, input_mode=mode, image_assets=assets)
                self.assertIn('身体整体越过', result)
                self.assertIn(agent._ZH_EFFECTS_ONLY_POLICY, result)
                self.assertNotIn(agent._EFFECTS_ONLY_POLICY, result)
                self.assertEqual(agent._section_body(result, 'overall_soundscape').strip(), '脚步接触地面的细碎声。')
                self.assertEqual(result, agent._validate(result, 11, input_mode=mode, image_assets=assets))

    def test_chinese_audio_conflicts_are_rejected_even_after_prohibitions(self):
        for bad in ('兔子说出自己的判断。', '兔子说：“我能赢。”', '生成英语对白。',
                    '不生成对白，但兔子说道你好。', '禁止背景音乐，背景音乐响起。',
                    '兔子无声地抬爪，同时开口回答。', '旁白由后期合成，添加背景音乐。'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                agent._assert_no_generated_audio(bad)

    def test_natural_sounds_and_local_prohibitions_are_preserved(self):
        for good in ('不生成任何语言的人声、对白、旁白、歌声或背景音乐。',
                     '角色不说话、不对口型，嘴部自然放松。',
                     '角色不说话、不唱歌。',
                     '角色动作自然连贯，不作说话口型。',
                     '不生成对白、旁白、字幕、标题卡或水印。',
                     '不生成对白、旁白或其他人声。',
                     '小鸟鸣叫，树叶沙沙作响，兔子呼吸声自然。',
                     '旁白由后期合成。角色抬起前爪，无声地微笑，不做连续说话口型。',
                     agent._ZH_EFFECTS_ONLY_POLICY):
            with self.subTest(good=good):
                agent._assert_no_generated_audio(good)

    def test_negative_verbs_do_not_hide_spoken_directions_or_double_negations(self):
        for bad in ('角色不说话、背景音乐响起', '角色不说话、唱出歌词',
                    '角色不说话但小鸭开口回答', '角色不能不说话', '角色不是不说话',
                    '不生成对白、字幕或水印，同时旁白响起',
                    '不作说话口型，但画外传来人声'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                agent._assert_no_generated_audio(bad)

    def test_chinese_reference_mapping_cannot_swap_or_duplicate_identities(self):
        assets = [{'picture_label': '<Picture 1>'}, {'picture_label': '<Picture 2>', 'subject_label': '<Subject 1>'}]
        for bad in (draft().replace('身份依据 <Picture 2>', '身份依据 <Picture 1>'),
                    draft().replace('summary:', '<Subject 1> 是另一个角色，身份依据 <Picture 2>。\n\nsummary:')):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                agent._validate(bad, 11, input_mode='composition_reference', image_assets=assets)

    def test_real_negative_list_with_lip_sync_preserves_sound_effects(self):
        prohibition = '不生成对白、旁白、歌声及对口型动作，嘴部自然保持静止。'
        prompt = draft().replace('不做连续说话口型', prohibition.rstrip('。'))
        assets = [{'picture_label': '<Picture 1>'}, {'picture_label': '<Picture 2>', 'subject_label': '<Subject 1>'}]
        result = agent._validate(prompt, 11, input_mode='composition_reference', image_assets=assets)
        self.assertIn('脚步接触地面的细碎声', result)
        self.assertIn('不生成对白、旁白、歌声及对口型动作', result)
        for conjunction in ('及', '以及', '和', '或', '、'):
            agent._assert_no_generated_audio('不生成对白' + conjunction + '对口型动作。')
            for positive in ('背景音乐响起', '小熊开口说话', '添加旁白'):
                for separator in ('，', '但', '；', '及', '以及'):
                    with self.subTest(positive=positive, separator=separator), self.assertRaises(ValueError):
                        agent._assert_no_generated_audio(prohibition + separator + positive)

    def test_chinese_tail_state_is_required(self):
        with self.assertRaises(ValueError):
            agent._validate(draft('first_last').replace('最终在结束时到达', '开场参照'), 11,
                            input_mode='first_last', image_assets=[{'picture_label': '<Picture 1>'}, {'picture_label': '<Picture 2>'}])

    def test_prohibitions_apply_to_predicates_not_a_fixed_subject_or_noun_list(self):
        for prefix in ('也', '本镜始终', '画外与画内均', '小熊和小鸭', ''):
            for negative in ('不生成对白、旁白、歌声或背景音乐', '不得开口说话', '避免连续说话口型'):
                with self.subTest(prefix=prefix,negative=negative):
                    agent._assert_no_generated_audio(prefix + negative + '；保留触盘声。')
                    for positive in ('背景音乐响起', '小鸭说出你好', '添加语音'):
                        with self.subTest(positive=positive), self.assertRaises(ValueError):
                            agent._assert_no_generated_audio(prefix + negative + '及' + positive)
        for bad in ('不能不生成对白', '并非不生成对白', '不禁止生成对白',
                    '不要避免说话', '不是不作说话口型', '不允许不说话'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                agent._assert_no_generated_audio(bad)

    def test_actions_used_as_nouns_in_prohibited_output_lists(self):
        for command in ('不生成', '不得生成', '也不要添加', '本镜禁止产生'):
            for noun in ('说话口型', '说话口型动作', '唱歌', '开口说话'):
                negative=command+'对白、'+noun+'、新增可见文字、字幕、标题卡或水印'
                agent._assert_no_generated_audio(negative)
                for positive in ('、背景音乐响起', '、小鸭说出你好', '、添加旁白'):
                    with self.subTest(command=command,noun=noun,positive=positive), self.assertRaises(ValueError):
                        agent._assert_no_generated_audio(negative+positive)
        for bad in ('不是不生成对白、说话口型', '不得生成对白、说话口型要真实',
                    '不得生成对白、开口说话表达想法', '不生成对白、唱歌给小熊听'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                agent._assert_no_generated_audio(bad)

    def test_local_pixel_lock_prohibition_cannot_hide_a_promise(self):
        agent._assert_no_strict_reference_frame('不是严格首帧，不要求像素锁定。')
        with self.assertRaises(ValueError):
            agent._assert_no_strict_reference_frame('不是严格首帧，但首帧必须逐像素一致。')

    def test_explicit_absence_of_speech_is_not_a_request_to_speak(self):
        prohibition = '无说话口型、对白、旁白、字幕或其他新文字。'
        agent._assert_no_generated_audio(prohibition)
        source = draft().replace('不做连续说话口型', '无说话口型')
        prepared = agent.prepare_h3_video_prompt(source, effects_only=True)
        self.assertIn('无说话口型', prepared)
        self.assertEqual(agent._section_body(prepared, 'overall_soundscape').strip(), '脚步接触地面的细碎声。')
        for bad in ('无说话口型、背景音乐响起', '无说话口型但角色开口说话',
                    '并非无说话口型', '不是无说话口型', '不能无说话口型'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                agent._assert_no_generated_audio(bad)

    def test_absence_list_does_not_request_singing(self):
        for prefix in ('全程无', '本镜没有'):
            good=prefix+'新文字、对白、旁白、歌唱或背景音乐'
            agent._assert_no_generated_audio(good)
            for tail in ('，角色开口说话', '、旁白响起', '但添加背景音乐'):
                with self.subTest(tail=tail),self.assertRaises(ValueError):
                    agent._assert_no_generated_audio(good+tail)
        for bad in ('并非无对白、歌唱', '不是没有对白、歌唱', '无对白、歌唱给姐姐听'):
            with self.subTest(bad=bad),self.assertRaises(ValueError):
                agent._assert_no_generated_audio(bad)


if __name__ == '__main__':
    unittest.main()
