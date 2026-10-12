"""Real H3 negative prose must not hide a neighboring affirmative instruction."""
import unittest

from backend.app import h3_prompt_agent as agent


class H3PromptNegationsTest(unittest.TestCase):
    def test_real_picture_book_negative_text_list_does_not_request_audio(self):
        negative = '画面不生成对白、旁白文字、字幕、标题或水印'
        agent._assert_no_generated_audio(negative + '。保留面团接触桌面的细小闷声。')
        for suffix in ('，但添加背景音乐。', '、旁白开始。', '，小鸭说出你好。'):
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                agent._assert_no_generated_audio(negative + suffix)
        with self.assertRaises(ValueError):
            agent._assert_no_generated_audio('画面不是不生成对白。')

    def test_without_treating_a_reference_as_a_locked_frame_has_local_scope(self):
        for negative in ('without treating it as a pixel-locked first frame',
                         'without using <Picture 1> as an exact opening frame',
                         'without regarding this as the strict first-frame'):
            with self.subTest(negative=negative):
                agent._assert_no_strict_reference_frame('Use the composition, ' + negative + '.')
                for suffix in (', but the opening frame must exactly match the reference.',
                               ' and it must be pixel-locked.'):
                    with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                        agent._assert_no_strict_reference_frame(negative + suffix)
        with self.assertRaises(ValueError):
            agent._assert_no_strict_reference_frame(
                'Without moving the camera, use an exact opening frame.')

    def test_real_repair_scene_excludes_singing_and_visible_words(self):
        negative = 'Generate no spoken dialogue, narration, singing, captions, or other visible words.'
        agent._assert_no_generated_audio(negative)
        for suffix in (' Background music plays.', ' The repairman sings.',
                       ' Add background music.'):
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                agent._assert_no_generated_audio(negative + suffix)
        with self.assertRaises(ValueError):
            agent._assert_no_generated_audio('No speech, visible words and the neighbor sings.')

    def test_real_clay_cart_negative_list_with_bubbles(self):
        negative = ('No dialogue, narration, singing, captions, bubbles, titles, '
                    'watermarks, or generated visible text.')
        agent._assert_no_generated_audio(negative)
        for noun in ('bubbles', 'speech bubbles', 'thought bubbles', 'dialogue bubbles',
                     'title cards', 'subtitle bars'):
            with self.subTest(noun=noun):
                agent._assert_no_generated_audio(f'No speech, singing, {noun}, or background music.')
                for tail in (', background music plays.', ' and the narrator speaks.',
                             ', singing begins.', ' and footsteps accompany background music.'):
                    with self.subTest(tail=tail), self.assertRaises(ValueError):
                        agent._assert_no_generated_audio(f'No speech, singing, {noun}' + tail)
        with self.assertRaises(ValueError):
            agent._assert_no_generated_audio(negative + ' Background music plays.')

    def test_reference_rather_than_strict_frame_is_not_a_lock(self):
        agent._assert_no_strict_reference_frame(
            'Maintain the composition; use it as a reference rather than a strict first frame.')

    def test_real_pixel_frame_negations_have_local_scope(self):
        for negative in ('not a pixel-locked first frame', 'not as a pixel-locked opening frame',
                         'rather than an exact first-frame', 'not the pixel-perfect opening frame'):
            with self.subTest(negative=negative):
                agent._assert_no_strict_reference_frame('Use the reference, ' + negative + '.')
                for suffix in (' and the first frame must exactly match the reference.',
                               ', but it must be pixel-locked.'):
                    with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                        agent._assert_no_strict_reference_frame('Use the reference, ' + negative + suffix)

    def test_reference_comparison_cannot_hide_an_added_lock(self):
        for suffix in (' and the first frame must exactly match the reference.',
                       ', but it must be pixel-locked.'):
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                agent._assert_no_strict_reference_frame(
                    'Use it as a reference rather than a strict first frame' + suffix)

    def test_real_generated_text_and_passive_audio_exclusions_are_accepted(self):
        for sentence in (
            'No dialogue, narration, singing, captions, other newly generated text, or music is produced.',
            'No spoken dialogue, narration, singing or generated visible text; '
            'narration is added separately in postproduction.',
            'No speech, singing, or background music is generated.',
            'No speech or background music are produced.',
        ):
            with self.subTest(sentence=sentence):
                agent._assert_no_generated_audio(sentence)

    def test_passive_exclusion_cannot_hide_other_affirmative_audio(self):
        negative = 'No dialogue, narration, singing, captions, other newly generated text, or music is produced'
        for suffix in ('. Background music plays.', '; The woman sings.',
                       ', but the narrator speaks.', ' and background music begins.'):
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                agent._assert_no_generated_audio(negative + suffix)
        for sentence in ('Without speech, singing is generated.',
                         'No speech, singing is encouraged.',
                         'No speech, generated visible text and the narrator speaks.'):
            with self.subTest(sentence=sentence), self.assertRaises(ValueError):
                agent._assert_no_generated_audio(sentence)

    def test_real_passive_exclusion_keeps_cart_sounds_and_authored_prose(self):
        negative = 'No dialogue, narration, singing, captions, other newly generated text, or music is produced.'
        source = ('integrated_multimodal_description:\n[Shot 1] The cart stops. ' + negative
                  + '\n\noverall_soundscape:\nCart wheels roll over the pavement. Quiet nighttime ambience.'
                  + '\n\nnon_diegetic_music:\nN/A')
        prepared = agent.prepare_h3_video_prompt(source, effects_only=True)
        self.assertIn(negative, prepared)
        self.assertEqual(agent._section_body(prepared, 'overall_soundscape').strip(),
                         'Cart wheels roll over the pavement. Quiet nighttime ambience.')

    def test_real_hyphenated_opening_frame_negation_is_accepted(self):
        for wording in ('opening-frame', 'opening frame', 'OPENING-FRAME'):
            with self.subTest(wording=wording):
                agent._assert_no_strict_reference_frame(
                    '<Picture 1> supplies the study composition and visual treatment throughout, '
                    f'not an exact {wording} pixel match.')

    def test_affirmative_hyphenated_opening_frame_promises_still_fail(self):
        for direction in (
            '<Picture 1> is an exact opening-frame.',
            'The opening-frame must exactly match <Picture 1>.',
            'Preserve the exact opening-frame and all its pixels.',
        ):
            with self.subTest(direction=direction), self.assertRaises(ValueError):
                agent._assert_no_strict_reference_frame(direction)

    def test_negated_opening_frame_does_not_hide_following_affirmative_clause(self):
        negative = '<Picture 1> is not an exact opening-frame pixel match'
        for separator in ('. ', '; ', ', but ', ', yet ', ', however ', ', nevertheless ', ' and '):
            with self.subTest(separator=separator), self.assertRaises(ValueError):
                agent._assert_no_strict_reference_frame(
                    negative + separator + 'the opening-frame must exactly match the reference.')

    def test_unrelated_negation_does_not_excuse_a_pixel_lock(self):
        with self.assertRaises(ValueError):
            agent._assert_no_strict_reference_frame(
                'Do not change the clothing; preserve the exact opening-frame.')

    def test_real_mixed_audio_and_narration_text_negative_list_is_accepted(self):
        for sentence in (
            'Generate no speech, singing or visible narration text.',
            'Generate no speech, singing, or visible narration text.',
            'Without speech, singing or visible narration text.',
            'No visible narration text, speech or singing.',
        ):
            with self.subTest(sentence=sentence):
                agent._assert_no_generated_audio(sentence)

    def test_negative_list_does_not_hide_adjacent_positive_speech_or_music(self):
        negative = 'Generate no speech, singing or visible narration text'
        for suffix in (
            '. The woman sings.',
            '; The woman sings.',
            ', the woman sings.',
            ', but the narrator says hello.',
            ', however background music plays.',
            ', yet a narrator speaks.',
            ' and the woman sings.',
            ' while background music plays.',
        ):
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                agent._assert_no_generated_audio(negative + suffix)

    def test_positive_sentence_before_the_negative_list_still_fails(self):
        with self.assertRaises(ValueError):
            agent._assert_no_generated_audio(
                'Background music plays. Generate no speech, singing or visible narration text.')

    def test_real_long_negative_list_is_accepted(self):
        for sentence in (
            'Generate no speech, narration, singing, music, captions, titles, overlays, or watermarks.',
            'Without speech or singing. Retain the door creak.',
        ):
            with self.subTest(sentence=sentence):
                agent._assert_no_generated_audio(sentence)

    def test_negative_list_never_swallows_a_predicate_or_positive_tail(self):
        for sentence in (
            'No speech, singing is encouraged.',
            'No speech, background music plays.',
            'No speech, singing, background music plays.',
            'No speech and singing, but music plays.',
            'No speech, singing, decorative borders and the woman sings.',
            'No speech, singing, music and background music plays.',
            'No speech, singing, a soundtrack plays.',
            'No speech, singingbird speaks.',
            'Without speech, background music and footsteps fill the room.',
            'Without speech, singing and dancing convey the emotion.',
            'Without speech, singing, dancing and footsteps fill the room.',
        ):
            with self.subTest(sentence=sentence), self.assertRaises(ValueError):
                agent._assert_no_generated_audio(sentence)

    def test_negative_production_commands_scope_over_noun_lists(self):
        nouns = 'speech, narration, singing, captions, titles, watermarks, or background music.'
        for command in ('Do not generate', "Don't add", 'Don’t include', 'Never produce',
                        'Must not synthesize', 'Avoid', 'Exclude', 'Omit', 'Do not generate any'):
            with self.subTest(command=command):
                agent._assert_no_generated_audio(command + ' ' + nouns)

    def test_negative_commands_do_not_hide_positive_predicates(self):
        for command in ('Do not generate', "Don't add", 'Never produce', 'Must not synthesize',
                        'Avoid', 'Exclude', 'Omit'):
            for tail in (', singing begins.', ', background music plays.',
                         ', singing, but background music plays.',
                         ', singing, titles and the narrator speaks.',
                         ', singing, decorative frames; the woman sings.'):
                with self.subTest(command=command, tail=tail), self.assertRaises(ValueError):
                    agent._assert_no_generated_audio(command + ' speech' + tail)

    def test_unrelated_negative_command_does_not_mask_generated_audio(self):
        for sentence in ('Do not generate a spotlight, background music plays.',
                         'Do not add singing faces, the narrator speaks.',
                         'Do not generate speechless scenes, background music plays.'):
            with self.subTest(sentence=sentence), self.assertRaises(ValueError):
                agent._assert_no_generated_audio(sentence)

    def test_extended_negative_list_preparation_preserves_sound_and_wording(self):
        negative = 'Generate no speech, narration, singing, music, captions, titles, overlays, or watermarks.'
        source = ('integrated_multimodal_description:\n[Shot 1] The child writes. ' + negative
                  + '\n\noverall_soundscape:\nQuiet indoor ambience and pen friction.\n\nnon_diegetic_music:\nN/A')
        prepared = agent.prepare_h3_video_prompt(source, effects_only=True)
        self.assertIn(negative, prepared)
        self.assertEqual(agent._section_body(prepared, 'overall_soundscape').strip(),
                         'Quiet indoor ambience and pen friction.')

    def test_effects_only_preparation_preserves_authored_natural_sounds(self):
        source = (
            'integrated_multimodal_description:\n'
            '[Shot 1] A child writes a letter. Generate no speech, singing or visible narration text.\n\n'
            'overall_soundscape:\n'
            'Retain quiet indoor ambience and pen friction against paper. '
            'The door creaks open, followed by light footsteps.\n\n'
            'non_diegetic_music:\nN/A'
        )
        prepared = agent.prepare_h3_video_prompt(source, effects_only=True)
        self.assertIn('Generate no speech, singing or visible narration text.', prepared)
        self.assertIn('Retain quiet indoor ambience and pen friction against paper.', prepared)
        self.assertIn('The door creaks open, followed by light footsteps.', prepared)
        self.assertEqual(agent._section_body(prepared, 'non_diegetic_music').strip(), 'N/A')


if __name__ == '__main__':
    unittest.main()
