import hashlib
import base64
import io
import json
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PIL import Image, ImageOps

from backend.app import h3_prompt_agent as agent


def keyframe_prompt(mode="single_first", duration=8):
    ending = (
        "<Picture 2> supplies the final state: the same child has placed the letter on the table."
        if mode == "first_last" else
        "The child finishes placing the letter on the table, retaining the clothing and room from <Picture 1>."
    )
    return (
        "integrated_multimodal_description:\n"
        "[Shot 1] Illustrated animation. <Picture 1> supplies the opening state: the child holds a letter. "
        "Keep the illustrated clothing, furniture and daylight continuous. "
        "The child lowers the letter onto the table with one small arm movement. "
        f"By the end of the {duration}-second video, {ending} The camera remains fixed.\n\n"
        "overall_soundscape:\nA quiet paper rustle.\n\n"
        "non_diegetic_music:\nSoft background music.\n"
    )


def reference_prompt(count=2, duration=8):
    definitions = ["<Picture 1> is the composition, drawing-style and room-lighting anchor."]
    actions = [
        "The target video keeps the illustrated drawing style and even daylight of <Picture 1>.\n"
        "[Shot 1] Preserve <Picture 1>'s room, palette and clothing continuity. "
        f"The child lowers the letter onto the table during the {duration}-second video. The camera stays fixed."
    ]
    for index in range(1, count + 1):
        definitions.append(
            f"<Subject {index}> is character {index}, with identity and clothing from <Picture {index + 1}>."
        )
        actions.append(
            f"<Subject {index}> uses only the identity and clothing from <Picture {index + 1}>, "
            "appearing at the approved entrance time without changing the room."
        )
    return (
        "subject_definitions:\n" + "\n".join(definitions)
        + "\n\nsummary:\nComplete the approved letter-placement action in the illustrated room."
        + "\n\nretention_analysis:\n<Picture 1> guides composition and style; it is not a strict first frame."
        + "\n\ndetailed_description:\n" + "\n".join(actions)
        + "\n\noverall_soundscape:\nQuiet paper and clothing rustles."
        + "\n\nnon_diegetic_music:\nN/A."
    )


class H3PromptInputsTest(unittest.TestCase):
    def test_final_video_prompt_is_the_only_creative_brief_in_every_mode(self):
        self.shot.update(intent='Abandoned story', action='The camera cuts to a doorway.',
                         image_prompt='Abandoned image design')
        for options in ({}, {'input_mode': 'first_last', 'last_image_path': self.last},
                        {'input_mode': 'composition_reference', 'reference_images': self.references}):
            with self.subTest(options=options):
                self.convert(**options)
                payload = json.loads(self.model.call_args.kwargs['user_prompt'])
                self.assertFalse(payload['allow_cuts'])
                self.assertEqual(payload['model_neutral_video_prompt'], self.shot['video_prompt'])
                for key in ('storyboard_intent', 'approved_dynamic_expression', 'core_image_prompt_for_context'):
                    self.assertNotIn(key, payload)

    def test_discarded_storyboard_text_does_not_invalidate_final_prompt_cache(self):
        expected = self.cache()
        self.shot.update(intent='Discarded intent', action='The camera cuts to a doorway.',
                         image_prompt='Discarded design')
        self.shot['motion_plan']['beats'][0]['action'] = 'Discarded motion prose'
        self.assertEqual(self.convert(), expected)
        self.model.assert_not_called()

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.first = self.make_image("first.png", "red")
        self.last = self.make_image("last.png", "blue")
        self.person = self.make_image("person.png", "green")
        self.references = [
            {"path": self.last, "id": "person-a", "label": "Character A", "role": "character_identity"},
            {"path": self.person, "id": "person-b", "label": "Character B", "role": "character_identity"},
        ]
        self.shot = {
            "intent": "The child places the letter on the table.",
            "action": "Lower the letter once. Keep the camera fixed.",
            "image_prompt": "An illustrated child in a jacket holds a letter beside a table.",
            "video_prompt": "The child lowers the letter to the table. Preserve the room and clothing.",
            "motion_plan": {"beats": [{"texts": []}]},
        }
        self.model = patch.object(agent, "generate_gemini_text", side_effect=self.model_response).start()
        self.addCleanup(patch.stopall)

    def make_image(self, name, color):
        path = self.directory / name
        Image.new("RGB", (12, 10), color).save(path)
        return path

    @staticmethod
    def model_response(**kwargs):
        payload = json.loads(kwargs["user_prompt"])
        mode = payload["input_mode"]
        prompt = (
            reference_prompt(payload["attached_image_count"] - 1, payload["requested_duration_seconds"])
            if mode == "composition_reference" else
            keyframe_prompt(mode, payload["requested_duration_seconds"])
        )
        return json.dumps({"h3_prompt": prompt})

    def convert(self, **kwargs):
        return agent.convert_for_h3(self.shot, 8, image_path=self.first, **kwargs)

    def cache(self, **kwargs):
        prompt, fingerprint = self.convert(**kwargs)
        self.shot.update(h3_prompt=prompt, h3_prompt_source=fingerprint)
        self.model.reset_mock()
        return prompt, fingerprint

    def test_retry_revalidates_unchanged_diagnostic_without_another_model_call(self):
        prompt, fingerprint = self.convert()
        diagnostic = agent.H3PromptValidationError(
            'Previously rejected wording', source_fingerprint=fingerprint,
            input_mode='single_first', draft_prompt=prompt).diagnostic
        self.model.reset_mock()
        result, current = self.convert(previous_diagnostic=diagnostic)
        self.assertEqual(result, prompt)
        self.assertEqual(current, fingerprint)
        self.assertTrue(self.shot['_h3_diagnostic_reused'])
        self.model.assert_not_called()

    def test_retry_cannot_reuse_stale_truncated_or_invalid_diagnostic(self):
        prompt, fingerprint = self.convert()
        original = agent.H3PromptValidationError(
            'Previous error', source_fingerprint=fingerprint,
            input_mode='single_first', draft_prompt=prompt).diagnostic
        for change in (
            {'source_fingerprint': 'changed'}, {'input_mode': 'first_last'},
            {'draft_truncated': True}, {'version': 2}, {'draft_prompt': None},
            {'draft_prompt': prompt.replace('The camera remains fixed.', 'The woman sings.')},
            {'draft_prompt': prompt.replace('<Picture 1>', '<Picture 9>')},
        ):
            with self.subTest(change=list(change)):
                self.model.reset_mock()
                self.shot['_h3_diagnostic_reused'] = True
                result, _ = self.convert(previous_diagnostic={**original, **change})
                self.assertEqual(result, prompt)
                self.model.assert_called_once()
                self.assertNotIn('_h3_diagnostic_reused', self.shot)

    def test_real_changed_image_invalidates_a_previous_diagnostic(self):
        prompt, fingerprint = self.convert()
        diagnostic = agent.H3PromptValidationError(
            'Previous error', source_fingerprint=fingerprint,
            input_mode='single_first', draft_prompt=prompt).diagnostic
        Image.new('RGB', (12, 10), 'yellow').save(self.first)
        self.model.reset_mock()
        _, current = self.convert(previous_diagnostic=diagnostic)
        self.assertNotEqual(current, fingerprint)
        self.model.assert_called_once()
        self.assertNotIn('_h3_diagnostic_reused', self.shot)

    def test_invalid_subject_draft_is_retained_without_repair_or_second_model_call(self):
        draft = reference_prompt().replace('<Subject 2> is', '<Subject 9> is')
        raw = json.dumps({'h3_prompt': draft})
        self.model.side_effect = None
        self.model.return_value = raw
        with self.assertRaises(agent.H3PromptValidationError) as caught:
            self.convert(input_mode='composition_reference', reference_images=self.references)
        diagnostic = caught.exception.diagnostic
        self.assertEqual(diagnostic['raw_response'], raw)
        self.assertEqual(diagnostic['draft_prompt'], draft)
        self.assertEqual(len(diagnostic['source_fingerprint']), 64)
        self.assertEqual(diagnostic['input_mode'], 'composition_reference')
        self.assertEqual(self.model.call_count, 1)
        self.assertNotIn('h3_prompt', self.shot)
        self.assertNotIn('image_data', diagnostic)

    def test_malformed_json_keeps_raw_draft_for_owner_diagnosis(self):
        self.model.side_effect = None
        self.model.return_value = '{"h3_prompt": '
        with self.assertRaises(agent.H3PromptValidationError) as caught:
            self.convert()
        self.assertEqual(caught.exception.diagnostic['raw_response'], '{"h3_prompt": ')
        self.assertEqual(caught.exception.diagnostic['draft_prompt'], '')
        self.assertEqual(self.model.call_count, 1)

    @staticmethod
    def named_reference_draft():
        return (reference_prompt()
            .replace('<Picture 1> is the composition, drawing-style and room-lighting anchor.\n', '')
            .replace('<Subject 1> is character 1, with identity and clothing from <Picture 2>.',
                     '<Subject 1> 米粒 (mili), identified by <Picture 2>: no visible outer ears; blue overalls.')
            .replace('<Subject 2> is character 2, with identity and clothing from <Picture 3>.',
                     '<Subject 2> 白耳 (baier), identified by <Picture 3>: long ears and a green pinafore.'))

    def test_named_subject_draft_normalizes_without_another_model_call(self):
        draft = self.named_reference_draft()
        self.model.side_effect = None
        self.model.return_value = json.dumps({'h3_prompt': draft})
        result, _ = self.convert(input_mode='composition_reference', reference_images=self.references)
        definitions = agent._section_body(result, 'subject_definitions')
        self.assertIn('<Subject 1> is 米粒 (mili), identified by <Picture 2>: no visible outer ears; blue overalls.', definitions)
        self.assertIn('<Subject 2> is 白耳 (baier), identified by <Picture 3>: long ears and a green pinafore.', definitions)
        self.assertIn('<Picture 1> is the composition, style, material, lighting and palette anchor.', definitions)
        self.assertEqual(agent._section_body(result, 'summary'), agent._section_body(draft, 'summary'))
        self.assertTrue(agent._section_body(result, 'detailed_description').startswith(
            agent._section_body(draft, 'detailed_description').rstrip()))
        self.assertEqual(self.model.call_count, 1)

    def test_named_declaration_normalization_is_idempotent(self):
        normalized = agent._normalize_reference_definitions(self.named_reference_draft())
        self.assertEqual(agent._normalize_reference_definitions(normalized), normalized)
        self.assertEqual(agent._normalize_reference_definitions(reference_prompt()), reference_prompt())

    def test_named_declarations_keep_wrong_picture_and_unknown_subject_guards(self):
        base = self.named_reference_draft()
        self.model.side_effect = None
        for draft in (
            base.replace('identified by <Picture 2>:', 'identified by <Picture 3>:'),
            base.replace('identified by <Picture 3>:', 'identified by <Picture 4>:'),
            base.replace('The camera stays fixed.', 'The camera stays fixed. <Subject 9> enters.'),
            base.replace('<Subject 2> 白耳 (baier), identified by <Picture 3>:', '<Subject 2> enters using <Picture 3>.'),
        ):
            with self.subTest(draft=draft):
                self.model.return_value = json.dumps({'h3_prompt': draft})
                with self.assertRaises(agent.H3PromptValidationError):
                    self.convert(input_mode='composition_reference', reference_images=self.references)
        self.assertEqual(self.model.call_count, 4)

    def test_duplicate_named_declarations_are_rejected(self):
        draft = self.named_reference_draft().replace('summary:',
            '<Subject 1> 米粒 (mili), identified by <Picture 2>: same character.\n\nsummary:')
        self.model.side_effect = None
        self.model.return_value = json.dumps({'h3_prompt': draft})
        with self.assertRaisesRegex(agent.H3PromptValidationError, '重复定义'):
            self.convert(input_mode='composition_reference', reference_images=self.references)

    def test_adding_known_anchor_metadata_does_not_supply_missing_action_reference(self):
        draft = self.named_reference_draft()
        start, end = agent._section_bounds(draft)['detailed_description']
        draft = draft[:start] + draft[start:end].replace('<Picture 1>', 'the room') + draft[end:]
        self.model.side_effect = None
        self.model.return_value = json.dumps({'h3_prompt': draft})
        with self.assertRaisesRegex(agent.H3PromptValidationError, '构图锚点'):
            self.convert(input_mode='composition_reference', reference_images=self.references)

    def test_named_declaration_does_not_excuse_affirmative_pixel_lock(self):
        draft = self.named_reference_draft().replace('The camera stays fixed.',
            'The camera stays fixed. The opening frame must exactly match <Picture 1>.')
        self.model.side_effect = None
        self.model.return_value = json.dumps({'h3_prompt': draft})
        with self.assertRaisesRegex(agent.H3PromptValidationError, '严格首帧'):
            self.convert(input_mode='composition_reference', reference_images=self.references)

    def test_diagnostic_is_bounded_and_does_not_capture_runtime_context(self):
        error = agent.H3PromptValidationError('bad' * 2000, source_fingerprint='a' * 64,
            input_mode='composition_reference', raw_response='r' * 70000, draft_prompt='d' * 20000)
        self.assertEqual(len(str(error)), 2000)
        self.assertEqual(len(error.diagnostic['raw_response']), 64000)
        self.assertEqual(len(error.diagnostic['draft_prompt']), 16000)
        self.assertTrue(error.diagnostic['raw_truncated'])
        self.assertTrue(error.diagnostic['draft_truncated'])

    def test_provider_exception_is_not_mislabeled_as_a_rejected_model_draft(self):
        self.model.side_effect = RuntimeError('provider unavailable')
        with self.assertRaises(RuntimeError) as caught:
            self.convert()
        self.assertNotIsInstance(caught.exception, agent.H3PromptValidationError)

    def test_rejected_cached_prompt_is_diagnosed_without_calling_model(self):
        self.cache(input_mode='composition_reference', reference_images=self.references)
        self.shot['h3_prompt'] = self.shot['h3_prompt'].replace('<Subject 2> is', '<Subject 9> is')
        with self.assertRaises(agent.H3PromptValidationError) as caught:
            self.convert(input_mode='composition_reference', reference_images=self.references)
        self.assertEqual(caught.exception.diagnostic['source'], 'cache')
        self.assertEqual(caught.exception.diagnostic['draft_prompt'], self.shot['h3_prompt'])
        self.model.assert_not_called()

    def test_confirmed_character_bindings_reach_agent_and_invalidate_cached_prompt(self):
        self.shot['_confirmed_character_inputs'] = {'status': 'covered', 'characters': [
            {'id': 'person-a', 'name': 'Later visitor', 'presence': 'later',
             'picture_labels': ['<Picture 2>']},
        ]}
        self.cache(input_mode='composition_reference', reference_images=self.references)
        self.shot['_confirmed_character_inputs']['characters'][0]['presence'] = 'opening'
        self.convert(input_mode='composition_reference', reference_images=self.references)
        self.assertEqual(self.model.call_count, 1)
        payload = json.loads(self.model.call_args.kwargs['user_prompt'])
        self.assertEqual(payload['confirmed_character_inputs'], self.shot['_confirmed_character_inputs'])
        self.assertEqual(payload['reference_assets'][1]['picture_label'], '<Picture 2>')

    def test_prepare_keyframe_prompt_moves_legacy_subtitle_tail_into_visual_section(self):
        for mode in ("single_first", "first_last"):
            original = agent._alignment(mode, 8) + "\n\n" + keyframe_prompt(mode).strip()
            contaminated = agent.enforce_no_auto_subtitles(original)
            with self.subTest(mode=mode):
                result = agent.prepare_h3_video_prompt(contaminated)
                self.assertTrue(result.startswith(agent._alignment(mode, 8) + "\n\n"))
                self.assertEqual(list(agent._section_bounds(result)), list(agent._KEYFRAME_SECTIONS))
                self.assertTrue(agent._section_body(result, "integrated_multimodal_description").startswith("[Shot 1]"))
                self.assertIn(agent._SUBTITLE_POLICY, agent._section_body(result, "integrated_multimodal_description"))
                self.assertNotIn(agent.NO_AUTO_SUBTITLES, result)
                self.assertEqual(result[result.index("overall_soundscape:"):],
                                 original[original.index("overall_soundscape:"):])
        self.model.assert_not_called()

    def test_prepare_reference_prompt_preserves_all_sections_and_authored_audio(self):
        original = reference_prompt().replace("N/A.", "An authored instrumental cue with precise timing.")
        contaminated = original + ("\n" + agent.NO_AUTO_SUBTITLES) * 2
        result = agent.prepare_h3_video_prompt(contaminated)
        self.assertEqual(list(agent._section_bounds(result)), list(agent._SECTIONS))
        for section in ("subject_definitions", "summary", "retention_analysis", "overall_soundscape", "non_diegetic_music"):
            self.assertEqual(agent._section_body(result, section), agent._section_body(original, section))
        self.assertTrue(agent._section_body(result, "detailed_description").startswith(
            agent._section_body(original, "detailed_description").rstrip()))
        self.assertIn(agent._SUBTITLE_POLICY, agent._section_body(result, "detailed_description"))
        self.assertNotIn(agent.NO_AUTO_SUBTITLES, result)
        self.model.assert_not_called()

    def test_prepare_clean_and_converted_prompts_is_idempotent(self):
        for options, authored in (
            ({}, keyframe_prompt()),
            ({"input_mode": "first_last", "last_image_path": self.last}, keyframe_prompt("first_last")),
            ({"input_mode": "composition_reference", "reference_images": self.references}, reference_prompt()),
        ):
            converted, _ = self.convert(**options)
            for prompt in (authored, converted, agent.enforce_no_auto_subtitles(converted)):
                with self.subTest(options=options, converted=prompt == converted):
                    prepared = agent.prepare_h3_video_prompt(prompt)
                    self.assertEqual(agent.prepare_h3_video_prompt(prepared), prepared)
                    self.assertEqual(prepared.count(agent._SUBTITLE_POLICY), 1)
            self.assertEqual(agent.prepare_h3_video_prompt(converted), converted)

    def test_prepare_generic_or_incomplete_fields_keeps_original_subtitle_policy(self):
        for prompt in (
            "The child slowly lowers the letter.",
            "An explanation mentioning detailed_description: without a complete H3 prompt.",
            "integrated_multimodal_description: [Shot 1] A child waves.\noverall_soundscape: Wind.",
            keyframe_prompt().replace("non_diegetic_music:\nSoft background music.", "non_diegetic_music:"),
            keyframe_prompt() + "\noverall_soundscape: Duplicate field.",
            "overall_soundscape: Wind.\nintegrated_multimodal_description: [Shot 1] A child waves.\nnon_diegetic_music: N/A",
        ):
            with self.subTest(prompt=prompt):
                result = agent.prepare_h3_video_prompt(prompt)
                self.assertEqual(result, agent.enforce_no_auto_subtitles(prompt))
                self.assertEqual(agent.prepare_h3_video_prompt(result), result)
                self.assertNotIn(agent._SUBTITLE_POLICY, result)
        self.model.assert_not_called()

    def test_prepare_only_removes_the_exact_known_trailing_paragraph(self):
        for suffix in (
            "\n" + agent.NO_AUTO_SUBTITLES + "\nKeep this authored afterword.",
            " Quoted inline: " + agent.NO_AUTO_SUBTITLES,
            "\n" + agent.NO_AUTO_SUBTITLES[:-1],
        ):
            original = keyframe_prompt().strip() + suffix
            with self.subTest(suffix=suffix):
                result = agent.prepare_h3_video_prompt(original)
                self.assertEqual(agent._section_body(result, "non_diegetic_music"),
                                 agent._section_body(original, "non_diegetic_music"))
                self.assertEqual(result.count(agent._SUBTITLE_POLICY), 1)
        self.model.assert_not_called()

    def test_default_single_first_uses_i2va_and_one_real_image(self):
        prompt, fingerprint = self.convert()
        call = self.model.call_args.kwargs
        payload = json.loads(call["user_prompt"])
        self.assertIsInstance(call["image_data"], dict)
        self.assertEqual(call["image_data"], agent._image_data(self.first)[0])
        self.assertEqual(payload["input_mode"], "single_first")
        self.assertEqual(payload["h3_prompt_format"], "h3_i2va")
        self.assertEqual(payload["reference_assets"][0]["role"], "first_frame")
        self.assertEqual(payload["reference_assets"][0]["sha256"], hashlib.sha256(self.first.read_bytes()).hexdigest())
        self.assertIn("Selected input mode: single_first", call["system_prompt"])
        self.assertTrue(prompt.startswith(agent._alignment("single_first", 8)))
        self.assertEqual(list(agent._section_bounds(prompt)), list(agent._KEYFRAME_SECTIONS))
        self.assertNotIn("<Picture 2>", prompt)
        self.assertNotIn("subject_definitions:", prompt)
        self.assertEqual(len(fingerprint), 64)

    def test_first_last_uses_two_real_images_and_fl2va_endpoints(self):
        prompt, _ = self.convert(input_mode="first_last", last_image_path=self.last)
        call = self.model.call_args.kwargs
        payload = json.loads(call["user_prompt"])
        self.assertEqual(call["image_data"], [agent._image_data(self.first)[0], agent._image_data(self.last)[0]])
        self.assertEqual(payload["h3_prompt_format"], "h3_fl2va")
        self.assertEqual([a["role"] for a in payload["reference_assets"]], ["first_frame", "last_frame"])
        self.assertTrue(prompt.startswith(agent._alignment("first_last", 8)))
        self.assertIn("<Picture 2> supplies the final state", prompt)
        self.assertIn("progressively converge to <Picture 2>", prompt)
        self.assertNotIn("subject_definitions:", prompt)
        self.assertNotIn("not a strict first frame", prompt)
        self.assertIn("two temporal endpoints", call["system_prompt"])

    def test_composition_references_preserve_order_pixels_and_metadata(self):
        prompt, _ = self.convert(input_mode="composition_reference", reference_images=self.references)
        call = self.model.call_args.kwargs
        payload = json.loads(call["user_prompt"])
        self.assertEqual(call["image_data"], [agent._image_data(p)[0] for p in (self.first, self.last, self.person)])
        assets = payload["reference_assets"]
        self.assertEqual([a["picture_label"] for a in assets], ["<Picture 1>", "<Picture 2>", "<Picture 3>"])
        self.assertEqual([a["sha256"] for a in assets], [
            hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.first, self.last, self.person)
        ])
        self.assertEqual([a["id"] for a in assets[1:]], ["person-a", "person-b"])
        self.assertEqual([a["label"] for a in assets[1:]], ["Character A", "Character B"])
        self.assertEqual(assets[1]["subject_label"], "<Subject 1>")
        self.assertEqual(list(agent._section_bounds(prompt)), list(agent._SECTIONS))
        self.assertIn("not a strict first frame", prompt)
        self.assertIn("<Subject 2>", agent._section_body(prompt, "detailed_description"))
        self.assertNotIn("0.00 seconds into the target video", prompt)
        self.assertIn("Selected input mode: composition_reference", call["system_prompt"])
        self.assertNotIn(str(self.directory), call["user_prompt"])

    def test_composition_anchor_can_be_used_without_extra_identity_pictures(self):
        prompt, _ = self.convert(input_mode="composition_reference", reference_images=[])
        self.assertIn("not a strict first frame", prompt)
        self.assertIsInstance(self.model.call_args.kwargs["image_data"], dict)

    def test_reference_names_reach_model_alongside_ids_roles_and_picture_labels(self):
        reference = {**self.references[0], "name": "Actor A costume", "label": "<Picture 2>"}
        prompt, fingerprint = self.convert(input_mode="composition_reference", reference_images=[reference])
        metadata = json.loads(self.model.call_args.kwargs["user_prompt"])["reference_assets"][1]
        self.assertEqual(
            {field: metadata[field] for field in ("id", "name", "label", "role", "picture_label")},
            {"id": "person-a", "name": "Actor A costume", "label": "<Picture 2>",
             "role": "character_identity", "picture_label": "<Picture 2>"},
        )
        self.shot.update(h3_prompt=prompt, h3_prompt_source=fingerprint)
        self.model.reset_mock()
        _, renamed = self.convert(
            input_mode="composition_reference",
            reference_images=[{**reference, "name": "Actor B costume"}],
        )
        self.assertNotEqual(fingerprint, renamed)
        self.model.assert_called_once()

    def test_transparent_cutouts_use_same_white_background_as_frozen_video_images(self):
        from backend.app import video_studio

        for mode in ("RGBA", "LA", "P"):
            with self.subTest(mode=mode):
                path = self.directory / f"cutout-{mode}.png"
                if mode == "P":
                    cutout = Image.new("P", (32, 32), 0)
                    cutout.putpalette([0, 0, 0, 255, 0, 0] + [0] * 762)
                    cutout.info["transparency"] = 0
                    cutout.paste(1, (16, 0, 32, 32))
                elif mode == "LA":
                    cutout = Image.new("LA", (32, 32), (0, 0))
                    cutout.paste((80, 255), (16, 0, 32, 32))
                else:
                    cutout = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
                    cutout.paste((200, 30, 30, 255), (16, 0, 32, 32))
                cutout.save(path)
                encoded, _ = agent._image_data(path)
                with Image.open(io.BytesIO(base64.b64decode(encoded["data"]))) as understood:
                    vision_background = understood.getpixel((3, 15))
                    vision_subject = understood.getpixel((27, 15))
                video_studio._normalize_rgb_image(path, strict=True)
                with Image.open(path) as frozen:
                    self.assertEqual(vision_background, (255, 255, 255))
                    self.assertEqual(vision_background, frozen.getpixel((3, 15)))
                    # Independent JPEG encodings can round an opaque color by
                    # a few units, but alpha/background semantics must match.
                    self.assertLessEqual(
                        max(abs(a - b) for a, b in zip(vision_subject, frozen.getpixel((27, 15)))), 3
                    )

    def test_exif_orientation_is_applied_before_image_understanding(self):
        path = self.directory / "oriented.jpg"
        source = Image.new("RGB", (24, 12), "white")
        source.paste((200, 30, 30), (0, 0, 12, 12))
        exif = Image.Exif()
        exif[274] = 6
        source.save(path, exif=exif, quality=95)
        encoded, _ = agent._image_data(path)
        with Image.open(io.BytesIO(base64.b64decode(encoded["data"]))) as understood, Image.open(path) as raw:
            expected = ImageOps.exif_transpose(raw)
            self.assertEqual(understood.size, (12, 24))
            self.assertEqual(understood.size, expected.size)
            for position in ((6, 3), (6, 20)):
                self.assertLessEqual(
                    max(abs(a - b) for a, b in zip(understood.getpixel(position), expected.getpixel(position))), 3
                )

    def test_audio_policy_is_enforced_in_all_modes(self):
        modes = [
            {},
            {"input_mode": "first_last", "last_image_path": self.last},
            {"input_mode": "composition_reference", "reference_images": self.references},
        ]
        for options in modes:
            with self.subTest(options=options):
                prompt, _ = self.convert(**options)
                self.assertEqual(agent._section_body(prompt, "non_diegetic_music").strip(), "N/A")
                expected_sound = ("Quiet paper and clothing rustles." if options.get("input_mode") == "composition_reference"
                                  else "A quiet paper rustle.")
                self.assertEqual(agent._section_body(prompt, "overall_soundscape").strip(), expected_sound)
                detail = "detailed_description" if options.get("input_mode") == "composition_reference" else "integrated_multimodal_description"
                self.assertEqual(agent._section_body(prompt, detail).count(agent._EFFECTS_ONLY_POLICY), 1)
                self.assertNotIn("Soft background music", prompt)

    def test_effects_only_manual_preparation_preserves_concrete_sounds_in_all_modes(self):
        soundscape = ("Soft forest breeze, one faint twig-on-leaf scrape and light footsteps on dry leaves, "
                      "matching the visible action. No speech, narration, vocalization or musical tones.")
        for mode in ("single_first", "first_last", "composition_reference"):
            source = reference_prompt() if mode == "composition_reference" else agent._alignment(mode, 8) + "\n\n" + keyframe_prompt(mode)
            start, end = agent._section_bounds(source)["overall_soundscape"]
            source = source[:start] + soundscape + "\n\n" + source[end:]
            with self.subTest(mode=mode):
                prepared = agent.prepare_h3_video_prompt(source, effects_only=True)
                self.assertEqual(agent._section_body(prepared, "overall_soundscape").strip(), soundscape)
                self.assertEqual(agent._section_body(prepared, "non_diegetic_music").strip(), "N/A")
                self.assertEqual(prepared.count(agent._EFFECTS_ONLY_POLICY), 1)
                self.assertEqual(prepared.count(agent._SUBTITLE_POLICY), 1)
                self.assertEqual(agent.prepare_h3_video_prompt(prepared, effects_only=True), prepared)
                self.assertIn("lowers the letter", prepared)
                if mode != "composition_reference":
                    self.assertTrue(prepared.startswith(agent._alignment(mode, 8)))
        self.model.assert_not_called()

    def test_effects_only_manual_rejects_incomplete_or_conflicting_structure(self):
        for source in (
            "The child lowers a letter; keep the paper rustle.",
            keyframe_prompt().replace("overall_soundscape:\nA quiet paper rustle.", "overall_soundscape:"),
            keyframe_prompt().replace("non_diegetic_music:", "music:"),
            keyframe_prompt() + "\noverall_soundscape: Duplicate sound.",
        ):
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, "请启用 H3 提示词转换，或按官方三段/六段格式补全"):
                agent.prepare_h3_video_prompt(source, effects_only=True)
        self.model.assert_not_called()

    def test_effects_only_preserves_requested_complete_silence_without_new_ambience(self):
        for mode, options in (
            ("single_first", {}),
            ("first_last", {"input_mode": "first_last", "last_image_path": self.last}),
            ("composition_reference", {"input_mode": "composition_reference", "reference_images": self.references}),
        ):
            source = reference_prompt() if mode == "composition_reference" else keyframe_prompt(mode)
            start, end = agent._section_bounds(source)["overall_soundscape"]
            source = source[:start] + "N/A\n\n" + source[end:]
            self.model.side_effect = None
            self.model.return_value = json.dumps({"h3_prompt": source})
            with self.subTest(mode=mode):
                converted, _ = self.convert(**options)
                prepared = agent.prepare_h3_video_prompt(source, effects_only=True)
                for result in (converted, prepared):
                    self.assertEqual(agent._section_body(result, "overall_soundscape").strip(), "N/A")
                    self.assertEqual(agent._section_body(result, "non_diegetic_music").strip(), "N/A")
                    self.assertNotIn("Generate only scene-appropriate", result)
                    self.assertNotIn("environmental ambience", result)
                    self.assertEqual(agent.prepare_h3_video_prompt(result, effects_only=True), result)

    def test_effects_only_keeps_natural_nonverbal_sounds_and_machine_hum(self):
        sounds = (
            "The fan hums softly. The child breathes steadily and laughs once as the paper rustles.",
            "Only faint leaf rustling and a soft outdoor breeze. No voice, narration, dialogue, singing or musical tones.",
        )
        for sound in sounds:
            source = keyframe_prompt().replace("A quiet paper rustle.", sound)
            self.model.side_effect = None
            self.model.return_value = json.dumps({"h3_prompt": source})
            with self.subTest(sound=sound):
                result, _ = self.convert()
                self.assertEqual(agent._section_body(result, "overall_soundscape").strip(), sound)
                manual = agent.prepare_h3_video_prompt(source, effects_only=True)
                self.assertEqual(agent._section_body(manual, "overall_soundscape").strip(), sound)

    def test_effects_only_rejects_positive_speech_or_music_in_soundscape(self):
        for sound in ("A narrator says hello.", "Soft background music.", "No speech, but background music plays."):
            source = keyframe_prompt().replace("A quiet paper rustle.", sound)
            self.model.side_effect = None
            self.model.return_value = json.dumps({"h3_prompt": source})
            with self.subTest(sound=sound):
                with self.assertRaisesRegex(ValueError, "生成语音或背景音乐"):
                    self.convert()
                with self.assertRaisesRegex(ValueError, "生成语音或背景音乐"):
                    agent.prepare_h3_video_prompt(source, effects_only=True)

    def test_natural_birdsong_and_wind_are_preserved_but_characters_cannot_sing(self):
        for sound in ("Birds sing in the trees. Wind whispers through the leaves.", "A bird is singing outside."):
            source = keyframe_prompt().replace("A quiet paper rustle.", sound)
            self.model.side_effect = None
            self.model.return_value = json.dumps({"h3_prompt": source})
            with self.subTest(sound=sound):
                converted, _ = self.convert()
                manual = agent.prepare_h3_video_prompt(source, effects_only=True)
                for result in (converted, manual):
                    self.assertEqual(agent._section_body(result, "overall_soundscape").strip(), sound)
        for sound in (
            "A child sings in the trees.",
            "Birds sing in the trees while the child sings a song.",
            "A child and birds sing in the trees.",
            "A bird sings the words hello.",
        ):
            source = keyframe_prompt().replace("A quiet paper rustle.", sound)
            self.model.return_value = json.dumps({"h3_prompt": source})
            with self.subTest(sound=sound):
                with self.assertRaisesRegex(ValueError, "生成语音或背景音乐"):
                    self.convert()
                with self.assertRaisesRegex(ValueError, "生成语音或背景音乐"):
                    agent.prepare_h3_video_prompt(source, effects_only=True)

    def test_manual_mixed_negative_lists_do_not_become_positive_singing(self):
        for direction in (
            "No cuts, new characters, speech, singing, captions or newly legible text.",
            "No delivery, long-distance travel, additional characters, cuts, transitions, talking, singing, captions or new legible writing.",
            "Their mouths remain non-speaking.",
        ):
            source = keyframe_prompt().replace("The camera remains fixed.", "The camera remains fixed. " + direction)
            self.model.side_effect = None
            self.model.return_value = json.dumps({"h3_prompt": source})
            with self.subTest(direction=direction):
                converted, _ = self.convert()
                manual = agent.prepare_h3_video_prompt(source, effects_only=True)
                for result in (converted, manual):
                    self.assertIn(direction, result)
                    self.assertEqual(agent._section_body(result, "overall_soundscape").strip(), "A quiet paper rustle.")
                    self.assertEqual(agent.prepare_h3_video_prompt(result, effects_only=True), result)
        for direction in ("No speech, but background music plays.", "No noise, the woman sings.",
                          "The non-speaking woman sings a song."):
            source = keyframe_prompt().replace("The camera remains fixed.", direction)
            self.model.return_value = json.dumps({"h3_prompt": source})
            with self.subTest(direction=direction):
                with self.assertRaisesRegex(ValueError, "生成语音或背景音乐"):
                    self.convert()
                with self.assertRaisesRegex(ValueError, "生成语音或背景音乐"):
                    agent.prepare_h3_video_prompt(source, effects_only=True)

    def test_effects_only_manual_rejects_positive_music_in_visual_description(self):
        source = keyframe_prompt().replace("The camera remains fixed.", "Background music plays.")
        with self.assertRaisesRegex(ValueError, "生成语音或背景音乐"):
            agent.prepare_h3_video_prompt(source, effects_only=True)
        self.assertIn("Background music plays.", agent.prepare_h3_video_prompt(source))
        self.model.assert_not_called()

    def test_previous_contract_cache_is_invalidated(self):
        images, assets = agent._prepare_images(self.first, "single_first", None, None)
        self.assertEqual(len(images), 1)
        current = agent.source_fingerprint(self.shot, 8, image_digest=assets[0]["sha256"], image_assets=assets)
        original_dumps = json.dumps

        def legacy_dumps(value, **kwargs):
            self.assertEqual(value["contract"], agent.H3_PROMPT_CONTRACT)
            legacy = {**value, "contract": 16}
            legacy.pop("prompt_language", None)
            return original_dumps(legacy, **kwargs)

        with patch.object(agent.json, "dumps", side_effect=legacy_dumps):
            legacy = agent.source_fingerprint(self.shot, 8, image_digest=assets[0]["sha256"], image_assets=assets)
        self.assertNotEqual(legacy, current)
        self.shot.update(h3_prompt=keyframe_prompt(), h3_prompt_source=legacy)
        result, fingerprint = self.convert()
        self.assertEqual(fingerprint, current)
        self.model.assert_called_once()
        self.assertEqual(agent._section_body(result, "overall_soundscape").strip(), "A quiet paper rustle.")
        self.assertEqual(agent._section_body(result, "non_diegetic_music").strip(), "N/A")

    def test_missing_and_undecodable_images_fail_before_model_call(self):
        invalid = self.directory / "invalid.png"
        invalid.write_text("This is not an image", encoding="utf-8")
        empty = self.directory / "empty.png"
        empty.touch()
        for path in (None, self.directory / "missing.png", self.directory, invalid, empty):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    agent.convert_for_h3(self.shot, 8, image_path=path)
        with self.assertRaises(ValueError):
            self.convert(input_mode="first_last", last_image_path=invalid)
        with self.assertRaises(ValueError):
            self.convert(input_mode="composition_reference", reference_images=[{**self.references[0], "path": invalid}])
        self.model.assert_not_called()

    def test_incompatible_modes_and_invalid_reference_metadata_fail_locally(self):
        invalid_options = [
            {"input_mode": "unknown"},
            {"last_image_path": self.last},
            {"reference_images": self.references},
            {"input_mode": "first_last"},
            {"input_mode": "first_last", "last_image_path": self.last, "reference_images": self.references},
            {"input_mode": "composition_reference", "last_image_path": self.last},
            {"input_mode": "composition_reference", "reference_images": {}},
            {"input_mode": "composition_reference", "reference_images": [self.references[0]] * 9},
            {"input_mode": "composition_reference", "reference_images": [self.references[0]] * 2},
            {"input_mode": "composition_reference", "reference_images": ["reference"]},
        ]
        for field in ("path", "id", "label", "role"):
            reference = dict(self.references[0])
            reference.pop(field)
            invalid_options.append({"input_mode": "composition_reference", "reference_images": [reference]})
        for options in invalid_options:
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.convert(**options)
        self.model.assert_not_called()

    def test_invalid_duration_fails_before_model_call(self):
        for duration in (0, 3, 16, True, 8.1, "8"):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                agent.convert_for_h3(self.shot, duration, image_path=self.first)
        self.model.assert_not_called()

    def test_unchanged_inputs_reuse_cache_and_revalidation_is_idempotent(self):
        for options in (
            {}, {"input_mode": "first_last", "last_image_path": self.last},
            {"input_mode": "composition_reference", "reference_images": self.references},
        ):
            with self.subTest(options=options):
                expected = self.cache(**options)
                self.assertEqual(self.convert(**options), expected)
                self.model.assert_not_called()
                self.shot.pop("h3_prompt")
                self.shot.pop("h3_prompt_source")

    def test_every_image_hash_invalidates_cache(self):
        for mode, options, paths in (
            ("single_first", {}, (self.first,)),
            ("first_last", {"last_image_path": self.last}, (self.first, self.last)),
            ("composition_reference", {"reference_images": self.references}, (self.first, self.last, self.person)),
        ):
            for index, path in enumerate(paths):
                with self.subTest(mode=mode, image=path.name):
                    _, previous = self.cache(input_mode=mode, **options)
                    Image.new("RGB", (13 + index, 10), "yellow").save(path)
                    _, current = self.convert(input_mode=mode, **options)
                    self.assertNotEqual(previous, current)
                    self.model.assert_called_once()
                    Image.new("RGB", (12, 10), "black").save(path)

    def test_reference_order_identity_label_and_role_invalidate_cache(self):
        options = {"input_mode": "composition_reference", "reference_images": self.references}
        _, original = self.cache(**options)
        for mutation in ("order", "id", "label", "role"):
            references = deepcopy(self.references)
            if mutation == "order":
                references.reverse()
            else:
                references[0][mutation] += "-changed"
            self.model.reset_mock()
            _, current = self.convert(input_mode="composition_reference", reference_images=references)
            self.assertNotEqual(original, current)
            self.model.assert_called_once()

    def test_mode_and_approved_visible_text_change_invalidate_cache(self):
        _, original = self.cache()
        _, changed = self.convert(input_mode="composition_reference")
        self.assertNotEqual(original, changed)
        self.model.assert_called_once()
        self.model.reset_mock()
        self.shot["motion_plan"]["beats"][0]["texts"] = [{"text": "你好"}]
        _, changed_text = self.convert()
        self.assertNotEqual(original, changed_text)
        self.model.assert_called_once()

    def test_bad_cached_mode_is_rejected_without_model_call(self):
        _, fingerprint = self.cache()
        self.shot.update(h3_prompt=reference_prompt(0), h3_prompt_source=fingerprint)
        with self.assertRaises(ValueError):
            self.convert()
        self.model.assert_not_called()

    def test_unprovided_picture_undefined_subject_and_wrong_mapping_are_rejected(self):
        base = reference_prompt()
        invalid = [
            base.replace("The camera stays fixed.", "The camera stays fixed. <Picture 4> supplies a guest."),
            base.replace("The camera stays fixed.", "The camera stays fixed. <Subject 9> enters."),
            base.replace("<Subject 1> is character 1, with identity and clothing from <Picture 2>.",
                         "<Subject 1> is character 1, with identity and clothing from <Picture 3>."),
            base.replace("<Subject 2> uses only the identity and clothing from <Picture 3>,",
                         "The second character enters,"),
        ]
        for prompt in invalid:
            with self.subTest(prompt=prompt):
                self.model.side_effect = None
                self.model.return_value = json.dumps({"h3_prompt": prompt})
                with self.assertRaises(ValueError):
                    self.convert(input_mode="composition_reference", reference_images=self.references)

    def test_first_last_cannot_pass_with_reference_prose_or_missing_final_endpoint(self):
        invalid = [
            reference_prompt(1),
            keyframe_prompt("single_first"),
            keyframe_prompt("first_last").replace("<Picture 2> supplies the final state", "The room supplies the final state"),
            keyframe_prompt("first_last").replace("The camera remains fixed.", "[Shot 2] At 00:09.000, the camera cuts to a close-up."),
            keyframe_prompt("first_last").replace("By the end of the 8-second video, ", "").replace(
                "<Picture 2> supplies the final state", "<Picture 2> provides identity cues"),
            keyframe_prompt("first_last").replace("supplies the final state", "is an identity-only reference and supplies the final state"),
            keyframe_prompt("first_last").replace("supplies the opening state", "is not a strict first frame and supplies the opening state"),
        ]
        for prompt in invalid:
            with self.subTest(prompt=prompt):
                self.model.side_effect = None
                self.model.return_value = json.dumps({"h3_prompt": prompt})
                with self.assertRaises(ValueError):
                    self.convert(input_mode="first_last", last_image_path=self.last)

    def test_reference_mode_rejects_positive_pixel_lock_promises(self):
        for direction in (
            "<Picture 1> is the exact first frame.",
            "<Picture 1> must match the first frame.",
            "It is not a strict first frame, but it must match the first frame.",
            "It is not just a style guide and is the exact first frame.",
            "<Picture 1> is not a strict first frame and is pixel-locked.",
        ):
            self.model.side_effect = None
            bad = reference_prompt().replace("Complete the approved", direction + " Complete the approved")
            self.model.return_value = json.dumps({"h3_prompt": bad})
            with self.subTest(direction=direction), self.assertRaises(ValueError):
                self.convert(input_mode="composition_reference", reference_images=self.references)

    def test_positive_speech_or_music_inside_action_description_is_rejected(self):
        for direction in (
            "A narrator says hello.", "Background music plays.", "No speech, but a narrator says hello.",
            "The child is silent and background music plays.", "No speech while background music plays.",
        ):
            bad = keyframe_prompt().replace("The camera remains fixed.", direction)
            self.model.side_effect = None
            self.model.return_value = json.dumps({"h3_prompt": bad})
            with self.subTest(direction=direction), self.assertRaises(ValueError):
                self.convert()

    def test_missing_visual_sections_are_not_replaced_with_generic_action(self):
        for bad in (
            keyframe_prompt().replace("integrated_multimodal_description:", "description:"),
            keyframe_prompt().replace("[Shot 1]", "[Shot 2]"),
        ):
            self.model.side_effect = None
            self.model.return_value = json.dumps({"h3_prompt": bad})
            with self.assertRaises(ValueError):
                self.convert()

    def test_official_single_shot_examples_pass_without_invented_action_timestamps(self):
        # Synthetic prose in base-en section 4.2 / ref-en section 5.1 syntax.
        for options, section, opening in (
            ({}, "integrated_multimodal_description", "[Shot 1] Illustrated animation."),
            ({"input_mode": "first_last", "last_image_path": self.last},
             "integrated_multimodal_description", "[Shot 1] Illustrated animation."),
            ({"input_mode": "composition_reference", "reference_images": self.references},
             "detailed_description", "The target video keeps the illustrated drawing style"),
        ):
            with self.subTest(options=options):
                prompt, _ = self.convert(**options)
                body = agent._section_body(prompt, section)
                self.assertTrue(body.startswith(opening), body)
                self.assertEqual(body.count("[Shot "), 1)
                self.assertNotIn("At 00:", body)
                self.assertNotIn("[00:", body)
                self.assertIn("lowers the letter onto the table", body)
                request = json.loads(self.model.call_args.kwargs["user_prompt"])
                self.assertEqual(request["requested_duration_seconds"], 8)
                self.assertFalse(request["allow_cuts"])

    def test_official_approved_cuts_keep_times_and_first_last_uses_actual_final_shot(self):
        self.shot["video_prompt"] += " The camera cuts to a close-up of the letter."
        for mode, options, section in (
            ("single_first", {}, "integrated_multimodal_description"),
            ("first_last", {"last_image_path": self.last}, "integrated_multimodal_description"),
            ("composition_reference", {"reference_images": self.references}, "detailed_description"),
        ):
            prompt = reference_prompt() if mode == "composition_reference" else keyframe_prompt(mode)
            cut = (
                "[Shot 2] At 00:03.500, the camera cuts to a close-up of the letter. "
                "The child lowers it carefully onto the table."
            )
            if mode == "first_last":
                # The actual endpoint belongs to the final shot, not Shot 1.
                prompt = prompt.replace(
                    "By the end of the 8-second video, <Picture 2> supplies the final state: "
                    "the same child has placed the letter on the table.", ""
                )
                cut += " At the end, the child's pose and letter position match <Picture 2>."
            prompt = prompt.replace("\n\noverall_soundscape:", "\n" + cut + "\n\noverall_soundscape:")
            self.model.side_effect = None
            self.model.return_value = json.dumps({"h3_prompt": prompt})
            with self.subTest(mode=mode):
                result, _ = self.convert(input_mode=mode, **options)
                self.assertIn(cut, agent._section_body(result, section))
                self.assertNotIn("[Shot 1] At", result)
                self.assertNotIn("At 00:08.000", result)
                self.assertTrue(json.loads(self.model.call_args.kwargs["user_prompt"])["allow_cuts"])
                if mode == "first_last":
                    self.assertTrue(result.startswith(agent._alignment(mode, 8, final_shot=2)))

    def test_cut_times_cannot_be_zero_outside_duration_duplicate_or_reversed(self):
        self.shot["video_prompt"] += " The camera cuts to a close-up of the table."
        for mode, options in (
            ("single_first", {}),
            ("first_last", {"last_image_path": self.last}),
            ("composition_reference", {"reference_images": self.references}),
        ):
            base = reference_prompt() if mode == "composition_reference" else keyframe_prompt(mode)
            for cut in (
                "[Shot 2] At 00:00.000, the camera cuts to the table.",
                "[Shot 2] At 00:08.000, the camera cuts to the table.",
                "[Shot 2] At 00:08.001, the camera cuts to the table.",
                "[Shot 2] At 01:00.000, the camera cuts to the table.",
                "[Shot 2] At 00:04.000, the camera cuts to the table.\n"
                "[Shot 3] At 00:04.000, the camera cuts back.",
                "[Shot 2] At 00:04.000, the camera cuts to the table.\n"
                "[Shot 3] At 00:03.000, the camera cuts back.",
            ):
                bad = base.replace("\n\noverall_soundscape:", "\n" + cut + "\n\noverall_soundscape:")
                self.model.side_effect = None
                self.model.return_value = json.dumps({"h3_prompt": bad})
                with self.subTest(mode=mode, cut=cut), self.assertRaisesRegex(ValueError, "切镜时间"):
                    self.convert(input_mode=mode, **options)

    def test_approved_source_shot_markers_and_cached_final_endpoint_remain_consistent(self):
        self.shot["video_prompt"] = (
            "[Shot 1] A child lowers a letter. "
            "[Shot 2] At 00:03.500, a close-up shows it settling on the table."
        )
        authored = keyframe_prompt("first_last").replace(
            "By the end of the 8-second video,",
            "[Shot 2] At 00:03.500, a close-up frames the letter. By the end of the 8-second video,",
        )
        self.model.side_effect = None
        self.model.return_value = json.dumps({"h3_prompt": authored})
        expected = self.cache(input_mode="first_last", last_image_path=self.last)
        self.assertTrue(expected[0].startswith(agent._alignment("first_last", 8, final_shot=2)))
        self.assertEqual(self.convert(input_mode="first_last", last_image_path=self.last), expected)
        self.model.assert_not_called()

    def test_malformed_or_nonsequential_cut_markers_are_rejected(self):
        self.shot["video_prompt"] += " Cut to a close-up of the table."
        for cut in (
            "[Shot 2] At 00:03, the camera cuts to the table.",
            "[Shot 2] At 00:03.5, the camera cuts to the table.",
            "[Shot 2] At 00:03.500.2, the camera cuts to the table.",
            "[Shot 2] At 00:63.000, the camera cuts to the table.",
            "[Shot 2] At -00:03.500, the camera cuts to the table.",
            "[Shot 2] The camera cuts to the table.",
            "[Shot 3] At 00:03.500, the camera cuts to the table.",
            "[Shot 0] At 00:03.500, the camera cuts to the table.",
            "[Shot 2] At 00:03.500, the camera cuts to the table.\n"
            "[Shot 2] At 00:04.500, the camera cuts back.",
        ):
            bad = keyframe_prompt().replace("\n\noverall_soundscape:", "\n" + cut + "\n\noverall_soundscape:")
            self.model.side_effect = None
            self.model.return_value = json.dumps({"h3_prompt": bad})
            with self.subTest(cut=cut), self.assertRaises(ValueError):
                self.convert()

    def test_unapproved_cuts_are_rejected_even_when_source_prohibits_cuts_explicitly(self):
        base_source = self.shot["video_prompt"]
        for source in (base_source, base_source + " Do not cut to a close-up.", base_source + " 禁止切镜。"):
            self.shot["video_prompt"] = source
            for direction in (
                "\n[Shot 2] At 00:03.500, the camera cuts to a close-up of the letter.",
                " The camera cuts to a close-up of the letter.",
            ):
                bad = keyframe_prompt().replace("\n\noverall_soundscape:", direction + "\n\noverall_soundscape:")
                self.model.side_effect = None
                self.model.return_value = json.dumps({"h3_prompt": bad})
                with self.subTest(source=source, direction=direction), self.assertRaisesRegex(ValueError, "未批准切镜"):
                    self.convert()

    def test_legacy_action_time_ranges_and_orphan_timestamps_are_not_turned_into_cuts(self):
        for mode, options in (
            ("single_first", {}),
            ("first_last", {"last_image_path": self.last}),
            ("composition_reference", {"reference_images": self.references}),
        ):
            base = reference_prompt() if mode == "composition_reference" else keyframe_prompt(mode)
            for bad in (
                base.replace("[Shot 1]", "[Shot 1] At 00:00.000"),
                base.replace("[Shot 1]", "[Shot 1] [00:00-00:08]"),
                base.replace("[Shot 1]", "[Shot 1] [00:00]"),
                base.replace("The child lowers", "At 00:03.000 The child lowers"),
            ):
                self.model.side_effect = None
                self.model.return_value = json.dumps({"h3_prompt": bad})
                with self.subTest(mode=mode, bad=bad), self.assertRaises(ValueError):
                    self.convert(input_mode=mode, **options)

    def test_natural_action_timing_is_allowed_but_cannot_exceed_video_duration(self):
        self.model.side_effect = None
        self.model.return_value = json.dumps({
            "h3_prompt": keyframe_prompt().replace("The child lowers", "At 3 seconds, the child lowers")
        })
        prompt, _ = self.convert()
        self.assertIn("At 3 seconds, the child lowers", prompt)
        self.assertNotIn("At 00:", prompt)
        self.model.return_value = json.dumps({
            "h3_prompt": keyframe_prompt().replace("The child lowers", "At 9 seconds, the child lowers")
        })
        with self.assertRaisesRegex(ValueError, "超出视频时长"):
            self.convert()

    def test_optional_audio_reference_keeps_silent_output_and_valid_cached_structure(self):
        for options in (
            {}, {"input_mode": "first_last", "last_image_path": self.last},
            {"input_mode": "composition_reference", "reference_images": self.references},
        ):
            with self.subTest(options=options):
                expected = self.cache(reference_audio=True, lipsync=True, **options)
                prompt, _ = expected
                self.assertIn("<Audio 1>", prompt)
                self.assertIn("must never become generated speech", prompt)
                self.assertEqual(self.convert(reference_audio=True, lipsync=True, **options), expected)
                self.model.assert_not_called()

    def test_unapproved_visible_text_is_checked_in_new_and_cached_prompts(self):
        bad = keyframe_prompt().replace("The camera remains fixed.", 'A label displays "Unapproved".')
        self.model.side_effect = None
        self.model.return_value = json.dumps({"h3_prompt": bad})
        with self.assertRaisesRegex(ValueError, "画中文字"):
            self.convert()
        self.model.side_effect = self.model_response
        prompt, _ = self.cache()
        self.shot["h3_prompt"] = prompt.replace("The camera remains fixed.", 'A label displays "Unapproved".')
        with self.assertRaisesRegex(ValueError, "画中文字"):
            self.convert()
        self.model.assert_not_called()


if __name__ == "__main__":
    unittest.main()
