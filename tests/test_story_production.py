"""Pure provenance checks: these tests never call a model or media service."""
import copy
import hashlib
import json
import unittest

from backend.app.story_production import (
    make_story_snapshot, production_story_handoff, validate_story_snapshot,
)
from backend.app.story_writer import canonical_story_brief, story_text_sha256


STORY = "米粒穿着蓝背心在窗边写信。写好以后，他走到门口，小鹿才从树后走出来接信。两个人一起把信送到山脚下。"
IDEA = "米粒没有外耳，全片穿蓝背心，不得换装。小鹿在米粒写完信以后才出现，不能提前入场。"


def make(**changes):
    values = {"story_id": "a" * 32, "story_revision": 2, "story": STORY,
              "title": "送信", "brief": canonical_story_brief(IDEA), "adoption_id": "b" * 32}
    values.update(changes)
    return make_story_snapshot(**values)


def source(snapshot):
    return {key: snapshot[key] for key in ("story_id", "story_revision", "story_sha256", "adoption_id") if key in snapshot}


def legacy():
    return {"story_id": "a" * 32, "story_revision": 2, "story_sha256": story_text_sha256(STORY),
            "title": "送信", "story": STORY}


class AdoptedSnapshotTest(unittest.TestCase):
    def test_exact_body_and_user_requirements_are_frozen_without_derived_facts(self):
        brief = canonical_story_brief(IDEA)
        snapshot = make(story=" \n" + STORY + "\n ", brief=brief)
        self.assertEqual(snapshot["story"], " \n" + STORY + "\n ")
        self.assertEqual(snapshot["brief"]["idea"], IDEA)
        brief["idea"] = "当前草稿改成红色衣服。"
        self.assertEqual(snapshot["brief"]["idea"], IDEA)
        for absent in ("synopsis", "characters", "scenes", "references", "reference_ids"):
            self.assertNotIn(absent, snapshot)
        expected = hashlib.sha256(json.dumps(snapshot["brief"], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        self.assertEqual(snapshot["brief_sha256"], expected)

    def test_digest_reuses_same_adoption_content_but_not_new_brief_or_body(self):
        original = make()
        title_and_id = make(title="另一个展示标题", adoption_id="c" * 32)
        self.assertEqual(original["snapshot_sha256"], title_and_id["snapshot_sha256"])
        changes = [
            {"story": "\n" + STORY}, {"story": STORY.replace("蓝", "红")},
            {"story_revision": 3}, {"story_id": "d" * 32},
            {"brief": canonical_story_brief(IDEA + "结尾保留信件。")},
            {"brief": canonical_story_brief(IDEA, duration_seconds=90)},
        ]
        for change in changes:
            with self.subTest(change=change):
                self.assertNotEqual(original["snapshot_sha256"], make(**change)["snapshot_sha256"])

    def test_new_id_is_generated_and_validated_independently_of_digest(self):
        first, second = make(adoption_id=None), make(adoption_id=None)
        self.assertRegex(first["adoption_id"], r"^[a-f0-9]{32}$")
        self.assertNotEqual(first["adoption_id"], second["adoption_id"])
        self.assertEqual(first["snapshot_sha256"], second["snapshot_sha256"])

    def test_validation_returns_independent_copy(self):
        original = make()
        checked = validate_story_snapshot(original, source(original))
        checked["brief"]["idea"] = "修改副本"
        self.assertEqual(original["brief"]["idea"], IDEA)

    def test_inconsistent_body_brief_or_digest_is_rejected(self):
        for field, value in (("story", STORY.replace("蓝", "红")), ("story_sha256", "c" * 64),
                             ("brief_sha256", "c" * 64), ("snapshot_sha256", "c" * 64),
                             ("snapshot_version", True), ("snapshot_version", 2),
                             ("story_revision", True), ("story_revision", 0),
                             ("adoption_id", "../another"), ("story_id", "a" * 31)):
            damaged = make()
            damaged[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_story_snapshot(damaged)
        damaged = make()
        damaged["brief"]["idea"] = "不要蓝背心。"
        with self.assertRaises(ValueError):
            validate_story_snapshot(damaged)

    def test_source_must_identify_the_exact_adoption(self):
        snapshot = make()
        for field, value in (("story_id", "c" * 32), ("story_revision", 3),
                             ("story_revision", True), ("story_sha256", "c" * 64),
                             ("adoption_id", "c" * 32), ("adoption_id", None)):
            supplied = source(snapshot)
            supplied[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "SOURCE_MISMATCH"):
                validate_story_snapshot(snapshot, supplied)
        supplied = source(snapshot)
        supplied.pop("adoption_id")
        with self.assertRaisesRegex(ValueError, "SOURCE_MISMATCH"):
            validate_story_snapshot(snapshot, supplied)

    def test_legacy_record_never_infers_a_brief(self):
        original = legacy()
        checked = validate_story_snapshot(original, source(original))
        self.assertEqual(checked, original)
        self.assertNotIn("brief", checked)
        self.assertEqual(validate_story_snapshot({**original, "adoption_id": None}), original)
        for patch in ({"brief": canonical_story_brief(IDEA)}, {"adoption_id": "b" * 32},
                      {"snapshot_sha256": "c" * 64}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                validate_story_snapshot({**original, **patch})

    def test_incomplete_or_noncanonical_briefs_cannot_be_frozen(self):
        for brief in ({}, {"idea": IDEA}, None, {**canonical_story_brief(IDEA), "characters": ["米粒"]}):
            with self.subTest(brief=brief), self.assertRaisesRegex(ValueError, "BRIEF_INVALID"):
                make(brief=brief)
        damaged = make()
        damaged.pop("brief")
        with self.assertRaises(ValueError):
            validate_story_snapshot(damaged)


class ProductionTextHandoffTest(unittest.TestCase):
    def test_exact_or_formatted_subtitles_keep_exact_adopted_inputs(self):
        snapshot = make(story="\n" + STORY + "\n")
        scenes = [
            {"slide_id": "s1", "text": "米粒穿着蓝背心，在窗边写信！"},
            {"slide_id": "s2", "text": "写好以后\n他走到门口。小鹿才从树后走出来接信"},
            {"slide_id": "s3", "text": "两个人一起把信送到山脚下"},
        ]
        before = copy.deepcopy(scenes)
        handoff = production_story_handoff(snapshot, scenes)
        self.assertEqual(handoff["status"], "current")
        self.assertEqual(handoff["story"], "\n" + STORY + "\n")
        self.assertEqual(handoff["brief"]["idea"], IDEA)
        self.assertEqual(handoff["source"], source(snapshot))
        self.assertEqual(scenes, before)
        handoff["brief"]["idea"] = "独立副本"
        self.assertEqual(snapshot["brief"]["idea"], IDEA)

    def test_changes_do_not_leak_old_story_or_constraints_to_director(self):
        altered = [STORY.replace("蓝", "红"), STORY.replace("才", "已经"), STORY.replace("不", "") + "不是原结尾。",
                   STORY.replace("山脚下", "山顶"), STORY + "后来他们又回去了。", STORY[:-10], STORY[::-1]]
        for text in altered:
            with self.subTest(text=text):
                handoff = production_story_handoff(make(), [{"text": text}])
                self.assertEqual(handoff["status"], "detached")
                self.assertEqual(handoff["source"], source(make()))
                for forbidden in ("story", "brief", "brief_sha256"):
                    self.assertNotIn(forbidden, handoff)

    def test_letters_digits_negation_and_order_are_not_fuzzy_matched(self):
        body = "米粒在 A2 号门口等了15分钟，仍然没有看见小鹿。他决定留下信再去山脚下找朋友。"
        snapshot = make(story=body)
        for text in (body.replace("没有", "已经"), body.replace("A2", "a2"), body.replace("15", "16"),
                     body.replace("A2", "2A"), body.replace("15", "十五"), body.replace("没有", "有")):
            with self.subTest(text=text):
                self.assertEqual(production_story_handoff(snapshot, [{"text": text}])["status"], "detached")

    def test_numeric_separators_and_semantic_symbols_are_preserved(self):
        body = "米粒走了1.5公里，在2:30抵达 A-2 门口。他带着10%的糖水和写给朋友的一封信。"
        snapshot = make(story=body)
        for text in (body.replace("1.5", "15"), body.replace("2:30", "230"),
                     body.replace("A-2", "A2"), body.replace("10%", "10")):
            with self.subTest(text=text):
                self.assertEqual(production_story_handoff(snapshot, [{"text": text}])["status"], "detached")

    def test_emoji_is_not_dropped_and_fullwidth_letters_are_not_transliterated(self):
        body = "米粒在信上画了一个🌱，又写上ＡＢ两个字母，准备带给住在山脚下的小鹿朋友。"
        snapshot = make(story=body)
        self.assertEqual(production_story_handoff(snapshot, [{"text": body}])["status"], "current")
        for text in (body.replace("🌱", ""), body.replace("ＡＢ", "AB")):
            self.assertEqual(production_story_handoff(snapshot, [{"text": text}])["status"], "detached")

    def test_subtitle_schema_errors_cannot_count_as_current(self):
        for scenes in (None, [], {}, [STORY], [{"other": STORY}], [{"text": None}],
                       [{"text": STORY, "text_content": "冲突的旧文稿"}], [{"text": STORY + "\x00"}]):
            with self.subTest(scenes=scenes):
                handoff = production_story_handoff(make(), scenes)
                self.assertEqual(handoff["status"], "detached")
                self.assertNotIn("brief", handoff)

    def test_official_text_content_key_is_accepted(self):
        result = production_story_handoff(make(), [{"text_content": STORY}])
        self.assertEqual(result["status"], "current")

    def test_legacy_same_body_has_provenance_but_no_invented_brief(self):
        handoff = production_story_handoff(legacy(), [{"text": STORY}])
        self.assertEqual(handoff["status"], "legacy_body_only")
        self.assertEqual(handoff["source"], source(legacy()))
        self.assertNotIn("brief", handoff)
        self.assertNotIn("story", handoff)
        self.assertNotIn("snapshot_sha256", handoff)
        changed = production_story_handoff(legacy(), [{"text": STORY.replace("蓝", "红")}])
        self.assertEqual(changed["status"], "detached")

    def test_invalid_snapshot_does_not_silently_become_detached(self):
        snapshot = make()
        snapshot["brief_sha256"] = "c" * 64
        with self.assertRaises(ValueError):
            production_story_handoff(snapshot, [{"text": STORY}])


if __name__ == "__main__":
    unittest.main()
