import copy
import hashlib
import json
import unittest

from backend.app.story_writer import (
    STORY_PRESETS, STORY_WRITER_SYSTEM_PROMPT, canonical_story_brief,
    parse_story_writer_response, story_text_sha256, story_writer_user_prompt,
    validate_story_text,
)


def candidate():
    return {
        "title": "门口的凳子",
        "story": "雨落下时，阿宁把门口的凳子搬到屋檐下。她记得送信的人每天都会在那里歇一会儿。傍晚，凳子上多了一封没有淋湿的信。",
        "synopsis": "阿宁为送信人留下一处避雨的位置，也收到了被保护好的来信。",
        "characters": [{"character_id": "aning", "name": "阿宁", "role": "留凳子的人", "description": "会留意送信人休息的需要。"}],
        "scenes": [{"scene_id": "door", "title": "门口屋檐", "summary": "凳子被移到避雨的位置，傍晚出现一封信。"}],
    }


def parse(value):
    return parse_story_writer_response(json.dumps(value, ensure_ascii=False))


class StoryBriefTest(unittest.TestCase):
    def test_general_is_default_and_does_not_choose_a_visual_style(self):
        brief = canonical_story_brief("一封无人认领的信")
        self.assertEqual(brief["preset"], "general")
        self.assertEqual(brief["narrative_mode"], "narration")
        self.assertEqual(brief["generation_stage"], "single_draft")
        self.assertNotIn("visual", brief)
        self.assertNotIn("audience", brief)
        self.assertNotIn("4—8", story_writer_user_prompt(brief))

    def test_picturebook_requires_explicit_choice(self):
        brief = canonical_story_brief("一次尝试", preset="picturebook")
        self.assertIn("4—8", json.loads(story_writer_user_prompt(brief))["preset_instruction"])
        self.assertEqual(set(STORY_PRESETS), {
            "general", "warm_relationship", "micro_mystery", "fantasy_rule", "miniature_world", "picturebook",
            "everyday_comedy", "speculative_life", "knowledge_story",
        })
        self.assertEqual(json.loads(json.dumps(STORY_PRESETS)), STORY_PRESETS)

    def test_idea_required_but_topics_and_manuscript_may_have_no_idea(self):
        for value in ("", " \n "):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "STORY_IDEA_INVALID"):
                canonical_story_brief(value)
        for intent in ("topics", "manuscript"):
            self.assertEqual(canonical_story_brief(intent=intent)["idea"], "")

    def test_manuscript_cannot_accidentally_build_a_generation_prompt(self):
        with self.assertRaisesRegex(ValueError, "STORY_MANUSCRIPT_MODEL_CALL_NOT_ALLOWED"):
            story_writer_user_prompt(canonical_story_brief(intent="manuscript"))

    def test_user_constraints_are_preserved_as_data(self):
        idea = '结尾必须保留母女团聚。角色没有外耳。\n允许水彩，不要改成年幼角色。{"title":"原话"}'
        payload = json.loads(story_writer_user_prompt(canonical_story_brief(idea)))
        self.assertEqual(payload["user_constraints"], idea)
        self.assertEqual(payload["writing_brief"]["idea"], idea)

    def test_topics_is_honest_about_single_draft_and_absent_history(self):
        payload = json.loads(story_writer_user_prompt(canonical_story_brief(intent="topics")))
        self.assertEqual(payload["writing_brief"]["generation_stage"], "single_draft")
        self.assertIn("不生成多个方向", payload["task"])
        self.assertIn("不声称已比较频道历史", payload["task"])

    def test_duration_estimate_scales_and_is_not_a_promise(self):
        shorter = canonical_story_brief("一把钥匙", duration_seconds=15)["duration_estimate"]
        longer = canonical_story_brief("一把钥匙", duration_seconds=600)["duration_estimate"]
        self.assertLess(shorter["max_characters"], longer["min_characters"])
        self.assertFalse(shorter["calibrated"])
        self.assertIn("不保证", shorter["note"])

    def test_invalid_brief_values_do_not_fall_back_to_an_unrelated_story(self):
        cases = [
            {"preset": "unknown"}, {"preset": None}, {"preset": []},
            {"intent": "rewrite"}, {"intent": []},
            *[{"duration_seconds": value} for value in (True, "60", 60.0, 14, 601, None)],
            *[{"idea": value} for value in (None, 123, ["story"], "a" * 4001, "text\x00text")],
        ]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                canonical_story_brief(**{"idea": "一个完整想法", **changes})

    def test_untrusted_prompt_metadata_is_rebuilt(self):
        brief = canonical_story_brief("一条旧街")
        brief.update(preset_version="fake", narrative_mode="dialogue", duration_estimate={"guaranteed": True})
        generated = json.loads(story_writer_user_prompt(brief))["writing_brief"]
        self.assertEqual(generated["narrative_mode"], "narration")
        self.assertNotEqual(generated["preset_version"], "fake")
        self.assertFalse(generated["duration_estimate"]["calibrated"])

    def test_system_prompt_defines_narration_constraints_without_claiming_review(self):
        self.assertIn("用户明确要求优先", STORY_WRITER_SYSTEM_PROMPT)
        self.assertIn("重要解决条件提前铺垫", STORY_WRITER_SYSTEM_PROMPT)
        self.assertIn("没有执行三方向筛选、独立编辑审稿", STORY_WRITER_SYSTEM_PROMPT)
        self.assertIn("不依赖多角色配音", STORY_WRITER_SYSTEM_PROMPT)


class StoryResponseTest(unittest.TestCase):
    def test_complete_candidate_preserves_ids_and_marks_derived_data(self):
        content = candidate()
        result = parse(content)
        self.assertEqual({key: result[key] for key in content}, content)
        self.assertEqual(result["story_sha256"], hashlib.sha256(content["story"].encode()).hexdigest())
        self.assertEqual(result["derived_from_story_sha256"], result["story_sha256"])
        self.assertEqual(result["character_identity_status"], "suggested")
        self.assertEqual(result["editorial_review"], "not_performed")

    def test_json_fences_are_accepted_but_explanation_outside_is_not(self):
        raw = json.dumps(candidate(), ensure_ascii=False)
        self.assertEqual(parse_story_writer_response("```JSON\n" + raw + "\n```")["title"], candidate()["title"])
        for invalid in ("下面是故事：\n" + raw, "```json\n" + raw + "\n```\n已通过审核", raw + raw):
            with self.subTest(invalid=invalid[:20]), self.assertRaises(ValueError):
                parse_story_writer_response(invalid)

    def test_title_changes_do_not_invalidate_body_derived_metadata(self):
        first = parse(candidate())
        changed = candidate()
        changed["title"] = "雨停以后"
        second = parse(changed)
        self.assertEqual(first["story_sha256"], second["story_sha256"])
        changed["story"] += "阿宁又把凳子搬回了门口。"
        self.assertNotEqual(first["story_sha256"], parse(changed)["story_sha256"])

    def test_missing_ids_get_reproducible_body_scoped_suggestions(self):
        content = candidate()
        del content["characters"][0]["character_id"]
        del content["scenes"][0]["scene_id"]
        first, second = parse(content), parse(content)
        self.assertEqual(first["characters"], second["characters"])
        self.assertEqual(first["scenes"], second["scenes"])
        self.assertTrue(first["characters"][0]["character_id"].startswith("character_"))
        changed = copy.deepcopy(content)
        changed["story"] += "第二天，她再次听见熟悉的脚步声。"
        self.assertNotEqual(first["characters"][0]["character_id"], parse(changed)["characters"][0]["character_id"])

    def test_same_display_name_does_not_merge_distinct_people(self):
        content = candidate()
        content["characters"].append({"character_id": "another_aning", "name": "阿宁", "role": "另一个人", "description": "另一位同名人物。"})
        self.assertEqual(len(parse(content)["characters"]), 2)

    def test_duplicate_or_invalid_ids_are_rejected(self):
        for identity in ("aning", "", None, 123, "../asset", "ASSET", "a" * 65):
            content = candidate()
            content["characters"].append({**content["characters"][0], "character_id": identity})
            with self.subTest(identity=identity), self.assertRaisesRegex(ValueError, "IDENTITY_INVALID"):
                parse(content)

    def test_no_characters_or_explicit_scenes_is_valid(self):
        content = candidate()
        content.update(characters=[], scenes=[])
        result = parse(content)
        self.assertEqual(result["characters"], [])
        self.assertEqual(result["scenes"], [])

    def test_malformed_top_level_and_missing_fields_fail_closed(self):
        cases = [None, [], "story", {"status": "success", "story": candidate()["story"]}, {**candidate(), "quality_score": 99}]
        cases.extend({key: value for key, value in candidate().items() if key != missing} for missing in candidate())
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                parse(payload)

    def test_fields_are_never_silently_stringified_or_truncated(self):
        for key, value in (
            ("title", 123), ("title", "x" * 81), ("story", {"text": "fake"}),
            ("story", "too short"), ("story", "x" * 12001),
            ("synopsis", ""), ("synopsis", "x" * 1001),
            ("characters", {}), ("characters", [None]), ("scenes", "scene"),
            ("characters", candidate()["characters"] * 17), ("scenes", candidate()["scenes"] * 33),
        ):
            with self.subTest(key=key, value_type=type(value).__name__), self.assertRaises(ValueError):
                parse({**candidate(), key: value})

    def test_malformed_nested_records_are_not_silently_dropped(self):
        for record in ({"name": "阿宁"}, {**candidate()["characters"][0], "role": None},
                       {**candidate()["characters"][0], "asset_id": "secretly-bound"}):
            with self.subTest(record=record), self.assertRaises(ValueError):
                parse({**candidate(), "characters": [record]})

    def test_duplicate_json_keys_and_nonstandard_numbers_are_rejected(self):
        raw = json.dumps(candidate(), ensure_ascii=False)
        nested_duplicate = raw.replace('"character_id": "aning"', '"character_id": "aning", "character_id": "other"')
        for invalid in (raw[:-1] + ', "story": "replacement"}', nested_duplicate,
                        raw.replace('"synopsis":', '"synopsis": NaN, "extra":')):
            with self.subTest(invalid=invalid[:30]), self.assertRaisesRegex(ValueError, "JSON_INVALID"):
                parse_story_writer_response(invalid)

    def test_obvious_production_directions_are_rejected(self):
        for direction in ("镜头切换到门口。", "画面淡出。", "镜头1：门口。", "【分镜二】：屋檐。",
                          "旁白：她站在门口。", "BGM：温暖的钢琴。", "字幕：一个温暖的故事。", "Shot 1: doorway."):
            content = candidate()
            content["story"] += "\n" + direction
            with self.subTest(direction=direction), self.assertRaisesRegex(ValueError, "PRODUCTION_DIRECTION"):
                parse(content)

    def test_quoted_story_dialogue_and_ordinary_film_words_are_allowed(self):
        story = '阿宁举起相机。妈妈笑着说：“把镜头拉近一点。”阿宁拍下那张旧凳子，又把照片收进抽屉。'
        self.assertEqual(validate_story_text(story), story)
        self.assertEqual(validate_story_text(candidate()["story"] + "她想起一部电影里的画面。"), candidate()["story"] + "她想起一部电影里的画面。")

    def test_real_camera_actions_in_a_story_are_not_production_instructions(self):
        story = "阿宁把镜头对准了妈妈。她第一次发现，这台旧相机仍能记录下清晰的笑脸。过去的画面定格在记忆里，她决定带妈妈再去一次海边。"
        self.assertEqual(validate_story_text(story), story)

    def test_invalid_unicode_is_rejected_as_content_instead_of_crashing_hashing(self):
        for field in ("title", "story", "synopsis"):
            content = candidate()
            content[field] += "\ud800"
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "CONTENT_INVALID"):
                parse(content)
        with self.assertRaisesRegex(ValueError, "CONTENT_INVALID"):
            story_text_sha256("\ud800")

    def test_refusal_or_placeholder_is_not_success(self):
        for story in ("很抱歉，我无法为你创作这个故事，因为暂时没有足够的信息。",
                      "正文待生成，请稍后再试。这里并没有实际完成一个可以朗读的故事。",
                      "I'm sorry, I cannot produce the requested story at the moment.",
                      "```json\n这是一段代码，并不是一个完整且可朗读的故事。\n```"):
            with self.subTest(story=story), self.assertRaisesRegex(ValueError, "RESPONSE_NOT_STORY"):
                validate_story_text(story)

    def test_hash_uses_stripped_body_but_preserves_internal_text(self):
        story = candidate()["story"]
        self.assertEqual(story_text_sha256("  " + story + "\n"), story_text_sha256(story))
        self.assertNotEqual(story_text_sha256(story), story_text_sha256(story + "\n下一段"))
        with self.assertRaises(ValueError):
            story_text_sha256(None)


if __name__ == "__main__":
    unittest.main()
