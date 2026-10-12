"""Model-free tests for bounded development, evidence and exact edit scope."""

import copy
import json
import unittest
from unittest.mock import patch

from backend.app import story_development as development
from backend.app.story_writer import canonical_story_brief, story_text_sha256


STORY = "林舟每晚把一张木凳留在便利店门口。雨夜，修路工推来工具车，把工具箱放到凳子上，蹲着吃完了饭。临走时，他朝店员点头。第二晚，那张凳子还在原处。"
ACTION = "把工具箱放到凳子上，蹲着吃完了饭"
BETTER_ACTION = "先将工具箱垫到摇晃的车轮下，再坐上凳子吃饭"
ENDING = "第二晚，那张凳子还在原处"
BETTER_ENDING = "第二晚，店员给凳脚垫了块木片，修路工也替店员修好了晃动的门牌"


def brief(**changes):
    return canonical_story_brief(**{"idea": "面向成年观众写凌晨两点留凳子的故事。不要死亡，保留修路工。", **changes})


def directions():
    return [
        {"id": "wait", "title": "门边的凳子", "premise": "一个凳子连接两个夜班人。", "opening": "木凳深夜被搬出。",
         "action": "修路工想在雨里找到吃饭的位置，店员腾出干燥角落。", "turn": "修路工发现店员也有需要修理的东西。", "ending": "两人用各自能做的事帮助对方。"},
        {"id": "repair", "title": "凳脚上的木片", "premise": "被悄悄修好的木凳让夜班人找到了修理者。", "opening": "店员发现摇晃的凳脚被垫平。",
         "action": "店员夜里留意谁会坐下，发现修路工修好木凳后继续工作。", "turn": "店员将热饭留在门边。", "ending": "原本各自忙碌的人有了固定相遇的时刻。"},
        {"id": "return", "title": "归还的位置", "premise": "凳子总被移回同一处，原来是替另一个人留路。", "opening": "店员发现门边的凳子总换位置。",
         "action": "店员在凌晨观察，发现工人移动凳子是避免堵住坡道。", "turn": "店员理解后重新安排门边空间。", "ending": "椅子终于有了不妨碍通行的位置。"},
    ]


def selection():
    return {"selected_direction_id": "wait", "selection_reason": "两次小修理形成互助，行为有具体后果。"}


def plan():
    return {"directions": directions(), **selection()}


def draft(story=STORY):
    return {"title": "凌晨的木凳", "story": story, "synopsis": "两位夜班人在雨夜有了短暂交流。",
            "characters": [{"character_id": "linzhou", "name": "林舟", "role": "店员", "description": "夜班看店的人。"}],
            "scenes": [{"scene_id": "shop", "title": "便利店门口", "summary": "木凳位于可以避雨的门边。"}]}


def review(quote=None, *, severity="major", verdict=None):
    if quote is None:
        return {"verdict": "ready", "issues": []}
    return {"verdict": verdict or "needs_revision", "issues": [{
        "severity": severity, "dimension": "行动因果", "quote": quote,
        "reason": "动作的必要性没有说清楚。", "suggestion": "补充这个动作解决了什么具体困难。",
    }]}


def edits(quote=ACTION, replacement=BETTER_ACTION):
    return {"edits": [{"quote": quote, "replacement": replacement}]}


def audience_response(legacy_review, story=STORY):
    """Valid model transport for tests whose focus is the existing editor contract."""
    return {'checks': [dict(key=key, decision='keep', quote=story[:20],
                           reason='Fixture reader evidence; not an artistic acceptance test.', suggestion='')
                       for key in ('interest', 'naturalness', 'payoff')],
            'issues': legacy_review['issues']}


class QueueCall:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, **kwargs):
        if set(kwargs) != {"system_prompt", "user_prompt", "temperature", "max_output_tokens"}:
            raise AssertionError("The injectable adapter must use only its four keyword arguments")
        self.requests.append(copy.deepcopy(kwargs))
        if not self.responses:
            raise AssertionError("Unexpected extra model call")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if (kwargs['system_prompt'] == development.AUDIENCE_REVIEW_PROMPT
                and isinstance(response, dict) and set(response) == {'verdict', 'issues'}):
            response = audience_response(response, json.loads(kwargs['user_prompt'])['story'])
        return response if isinstance(response, str) else json.dumps(response, ensure_ascii=False)

    def payload(self, index):
        return json.loads(self.requests[index]["user_prompt"])


class StoryPlanningTest(unittest.TestCase):
    def test_three_directions_then_independent_selection_without_scores(self):
        call = QueueCall({"directions": directions()}, selection())
        events = []
        result = development.plan_story(brief(), call=call, progress=lambda stage, details: events.append((stage, details)))
        self.assertEqual(result, plan())
        self.assertEqual(len(call.requests), 2)
        self.assertNotEqual(call.requests[0]["system_prompt"], call.requests[1]["system_prompt"])
        self.assertEqual(call.payload(1)["directions"], directions())
        self.assertNotIn("score", json.dumps(call.payload(1)))
        self.assertEqual([(stage, data["call_index"]) for stage, data in events if data["event"] == "started"],
                         [("planning", 1), ("selecting", 2)])
        self.assertEqual(events[-1][1]["plan"], result)

    def test_wrong_counts_duplicate_ids_or_identical_mechanisms_stop_before_selector(self):
        duplicate_id = directions()
        duplicate_id[1]["id"] = duplicate_id[0]["id"]
        renamed_copy = directions()
        renamed_copy[1] = {**renamed_copy[0], "id": "copy", "title": "换了一个标题"}
        missing = directions()
        del missing[0]["ending"]
        extra_score = directions()
        extra_score[0]["score"] = 100
        for rows in (directions()[:2], directions() + [directions()[0]], duplicate_id, renamed_copy, missing, extra_score):
            call = QueueCall({"directions": rows})
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                development.plan_story(brief(), call=call)
            self.assertEqual(len(call.requests), 1)

    def test_selector_cannot_invent_an_id_or_claim_no_selection_as_success(self):
        for value in ("missing", "", None, []):
            call = QueueCall({"directions": directions()}, {**selection(), "selected_direction_id": value})
            with self.subTest(value=value), self.assertRaises(ValueError):
                development.plan_story(brief(), call=call)
            self.assertEqual(len(call.requests), 2)

    def test_supplied_plan_and_user_selection_skip_all_replanning(self):
        saved = plan()
        before = copy.deepcopy(saved)
        call = QueueCall(draft(), review())
        result = development.develop_story(brief(), plan=saved, direction_id="repair", call=call)
        self.assertEqual(saved, before)
        self.assertEqual(result["plan"]["selected_direction_id"], "repair")
        self.assertEqual(call.payload(0)["selected_direction"]["id"], "repair")
        self.assertEqual(len(call.requests), 2)

    def test_invalid_saved_plan_or_direction_never_writes_a_draft(self):
        for saved, identity in (({**plan(), "selected_direction_id": "missing"}, None), (plan(), "missing")):
            call = QueueCall()
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                development.develop_story(brief(), plan=saved, direction_id=identity, call=call)
            self.assertFalse(call.requests)

    def test_manuscript_is_never_replanned_but_can_be_explicitly_reviewed(self):
        manuscript = brief(intent="manuscript", idea="")
        call = QueueCall()
        for operation in (development.plan_story, development.develop_story):
            with self.assertRaisesRegex(ValueError, "MANUSCRIPT_MODEL_CALL_NOT_ALLOWED"):
                operation(manuscript, call=call)
        self.assertFalse(call.requests)
        self.assertEqual(development.review_story(manuscript, STORY, call=QueueCall(review())), review())

    def test_default_adapter_uses_existing_json_language_provider(self):
        responses = [json.dumps({"directions": directions()}, ensure_ascii=False), json.dumps(selection(), ensure_ascii=False)]
        with patch.object(development, "generate_gemini_text", side_effect=responses) as provider:
            self.assertEqual(development.plan_story(brief()), plan())
        self.assertEqual(provider.call_count, 2)
        for invocation in provider.call_args_list:
            self.assertEqual(invocation.kwargs["response_mime_type"], "application/json")
            self.assertEqual(invocation.kwargs["json_root"], "object")


class StoryDevelopmentTest(unittest.TestCase):
    def test_picturebook_default_writes_then_reviews_without_inventing_a_plan(self):
        call = QueueCall(draft(), review())
        result = development.develop_story(brief(preset="picturebook"), call=call)
        self.assertIsNone(result["plan"])
        self.assertEqual(len(call.requests), 2)
        self.assertNotIn("selected_direction", call.payload(0))
        self.assertNotIn("directions", call.payload(1))
        self.assertNotIn("selection_reason", call.payload(1))
        self.assertEqual(call.payload(1)["story"], result["draft"]["story"])

    def test_picturebook_explicit_direction_is_preserved_in_writing_and_rebuild(self):
        bad = audience_response(review())
        bad["checks"][0].update(decision="rework", suggestion="让事件有具体发展。")
        call = QueueCall(draft(), bad, draft(), review())
        result = development.develop_story(brief(preset="picturebook"), plan=plan(), direction_id="repair", call=call)
        self.assertEqual(result["plan"]["selected_direction_id"], "repair")
        self.assertEqual(call.payload(0)["selected_direction"]["id"], "repair")
        self.assertEqual(call.payload(2)["selected_direction"]["id"], "repair")
        self.assertTrue(call.payload(2)["direction_is_user_selected"])

    def test_automatic_direction_does_not_lock_a_rejected_story_premise(self):
        bad = audience_response(review())
        bad["checks"][0].update(decision="rework", suggestion="重新构思事件。")
        call = QueueCall({"directions": directions()}, selection(), draft(), bad, draft(), review())
        development.develop_story(brief(), call=call)
        self.assertFalse(call.payload(4)["direction_is_user_selected"])

    def test_direct_rebuild_stops_after_a_second_core_rejection(self):
        bad = audience_response(review())
        bad["checks"][0].update(decision="rework", suggestion="重新构思事件。")
        call = QueueCall(draft(), bad, draft(), bad)
        result = development.develop_story(brief(preset="picturebook"), call=call)
        self.assertEqual(result["editorial_review"], "needs_revision")
        self.assertEqual(len(call.requests), 4)
        self.assertFalse(call.payload(2)["direction_is_user_selected"])
        self.assertIsNone(call.payload(2)["selected_direction"])

    def test_selection_rejection_preserves_editorial_reason_without_writing(self):
        events = []
        call = QueueCall({"directions": directions()}, {"selected_direction_id": "", "selection_reason": "三个方向都还只有设定。"})
        with self.assertRaises(development.StoryDirectionsRejected):
            development.plan_story(brief(), call=call, progress=lambda stage, detail: events.append(detail))
        self.assertEqual(len(call.requests), 2)
        self.assertEqual(events[-1]["selection_rejection"], "三个方向都还只有设定。")

    def test_ready_draft_takes_four_calls_and_retains_current_metadata(self):
        call = QueueCall({"directions": directions()}, selection(), draft(), review())
        events = []
        result = development.develop_story(brief(), call=call, progress=lambda stage, data: events.append((stage, data)))
        self.assertEqual(result["draft"]["story"], STORY)
        self.assertEqual(result["editorial_review"], "ready")
        self.assertEqual(result["revision_rounds"], 0)
        self.assertEqual(result["draft"]["derived_status"], "current")
        self.assertEqual(result["draft"]["story_sha256"], result["draft"]["derived_from_story_sha256"])
        self.assertEqual([data["call_index"] for _, data in events if data["event"] == "started"], [1, 2, 3, 4])
        self.assertEqual(events[-1][0], "completed")
        self.assertEqual(events[-1][1]["draft"], result["draft"])

    def test_one_patch_and_review_update_only_body_and_mark_summaries_stale(self):
        call = QueueCall({"directions": directions()}, selection(), draft(), review(ACTION), edits(), review())
        result = development.develop_story(brief(), call=call)
        changed = STORY.replace(ACTION, BETTER_ACTION)
        self.assertEqual(result["draft"]["story"], changed)
        self.assertEqual(result["revision_rounds"], 1)
        self.assertEqual(len(call.requests), 6)
        self.assertEqual(result["draft"]["story_sha256"], story_text_sha256(changed))
        self.assertEqual(result["draft"]["derived_from_story_sha256"], story_text_sha256(STORY))
        self.assertEqual(result["draft"]["derived_status"], "stale")
        self.assertEqual(result["draft"]["characters"], draft()["characters"])
        self.assertEqual(result["draft"]["synopsis"], draft()["synopsis"])
        self.assertEqual(call.payload(5)["story"], changed)

    def test_two_repairs_stop_at_eight_calls_even_when_major_issue_remains(self):
        call = QueueCall({"directions": directions()}, selection(), draft(), review(ACTION), edits(), review(ENDING),
                         edits(ENDING, BETTER_ENDING), review("临走时，他朝店员点头"))
        events = []
        result = development.develop_story(brief(), call=call, progress=lambda stage, data: events.append((stage, data)))
        self.assertEqual(len(call.requests), 8)
        self.assertEqual(result["revision_rounds"], 2)
        self.assertEqual(len(result["reviews"]), 3)
        self.assertEqual(result["editorial_review"], "needs_revision")
        self.assertEqual(result["draft"]["editorial_review"], "needs_revision")
        self.assertEqual(result["draft"]["derived_from_story_sha256"], story_text_sha256(STORY))
        self.assertEqual([data["call_index"] for _, data in events if data["event"] == "started"], list(range(1, 9)))
        self.assertEqual([data["round"] for stage, data in events if stage == "reviewing" and data["event"] == "started"], [0, 1, 2])

    def test_review_has_only_brief_and_current_story_not_writer_or_selector_self_assessment(self):
        call = QueueCall({"directions": directions()}, selection(), draft(), review())
        development.develop_story(brief(), call=call)
        payload = call.payload(3)
        self.assertEqual(set(payload), {"writing_brief", "preset_instruction", "story"})
        self.assertNotIn("selection_reason", payload)
        self.assertNotIn("synopsis", payload)
        self.assertNotIn("previous_review", payload)
        self.assertNotEqual(call.requests[2]["system_prompt"], call.requests[3]["system_prompt"])

    def test_hard_user_requirements_reach_every_stage_unchanged(self):
        requirements = "最后必须团聚；米粒没有可见外耳，始终穿蓝背心；画风不确定。"
        call = QueueCall({"directions": directions()}, selection(), draft(), review(ACTION), edits(), review())
        development.develop_story(brief(idea=requirements), call=call)
        for index in range(len(call.requests)):
            self.assertEqual(call.payload(index)["writing_brief"]["idea"], requirements)

    def test_ambiguous_overlapping_or_whole_story_patch_is_rejected(self):
        invalid = [
            {"edits": []}, {"edits": "change"}, edits("不存在的原文", "新文字"),
            edits("凳子", "椅子"), edits(STORY, STORY + "新的结局。"), edits(ACTION, ACTION),
            {"edits": [{"quote": ACTION, "replacement": BETTER_ACTION}, {"quote": "工具箱放到", "replacement": "调整"}]},
            {"edits": [{"quote": ACTION, "replacement": BETTER_ACTION, "reason": "extra"}]},
        ]
        for patch_payload in invalid:
            call = QueueCall(draft(), review(ACTION), patch_payload)
            with self.subTest(patch=patch_payload), self.assertRaises(ValueError):
                development.develop_story(brief(), plan=plan(), call=call)
            self.assertEqual(len(call.requests), 3)

    def test_overlapping_occurrences_are_not_misread_as_unique(self):
        story = "他在纸上写下aaaa，想从这些重复的符号里找出规律。最后，他把纸折好，放回抽屉。"
        call = QueueCall(draft(story), review("aaaa"), edits("aaa", "新的符号"))
        with self.assertRaisesRegex(ValueError, "QUOTE_NOT_UNIQUE"):
            development.develop_story(brief(), plan=plan(), call=call)

    def test_multiple_nonoverlapping_patches_apply_against_the_same_original(self):
        patches = {"edits": [{"quote": ENDING, "replacement": BETTER_ENDING},
                             {"quote": "林舟", "replacement": "守夜的林舟"}]}
        call = QueueCall(draft(), review(ENDING), patches, review())
        result = development.develop_story(brief(), plan=plan(), call=call)
        self.assertEqual(result["draft"]["story"], STORY.replace("林舟", "守夜的林舟").replace(ENDING, BETTER_ENDING))

    def test_production_directions_in_a_repair_fail_without_review_or_fallback(self):
        call = QueueCall(draft(), review(ACTION), edits(ACTION, "\n镜头切到便利店。\n"))
        with self.assertRaisesRegex(ValueError, "PRODUCTION_DIRECTION"):
            development.develop_story(brief(), plan=plan(), call=call)
        self.assertEqual(len(call.requests), 3)

    def test_model_failure_after_initial_draft_propagates_without_success(self):
        call = QueueCall(draft(), RuntimeError("provider failed"))
        events = []
        with self.assertRaisesRegex(RuntimeError, "provider failed"):
            development.develop_story(brief(), plan=plan(), call=call,
                                      progress=lambda stage, data: events.append((stage, data)))
        self.assertFalse(any(stage == "completed" for stage, _ in events))
        self.assertEqual(len(call.requests), 2)

    def test_checkpoint_consumer_cannot_mutate_internal_artifacts(self):
        def mutate(stage, details):
            if "draft" in details:
                details["draft"]["story"] = "tampered"
            if "plan" in details:
                details["plan"]["directions"].clear()
        result = development.develop_story(brief(), plan=plan(), call=QueueCall(draft(), review()), progress=mutate)
        self.assertEqual(result["draft"]["story"], STORY)
        self.assertEqual(len(result["plan"]["directions"]), 3)

    def test_checkpoint_failure_stops_before_next_paid_stage(self):
        call = QueueCall({"directions": directions()}, selection())
        def fail(stage, details):
            if stage == "planning" and details["event"] == "completed":
                raise RuntimeError("could not save checkpoint")
        with self.assertRaisesRegex(RuntimeError, "checkpoint"):
            development.plan_story(brief(), call=call, progress=fail)
        self.assertEqual(len(call.requests), 1)


class StoryReviewValidationTest(unittest.TestCase):
    def test_minor_issue_may_be_ready_but_major_needs_revision(self):
        self.assertEqual(development.validate_review(review(ACTION, severity="minor", verdict="ready"), STORY)["verdict"], "ready")
        with self.assertRaisesRegex(ValueError, "REVIEW_INVALID"):
            development.validate_review(review(ACTION, verdict="ready"), STORY)

    def test_unanchored_or_fabricated_quote_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "QUOTE_NOT_FOUND"):
            development.review_story(brief(), STORY, call=QueueCall(review("这是作者没有写过的一句原话。")))

    def test_reviews_are_strict_and_bounded(self):
        invalid = [
            {"verdict": "needs_revision", "issues": []}, {"verdict": "excellent", "issues": []},
            {"verdict": "ready", "issues": None}, {**review(), "score": 100},
            {"verdict": "needs_revision", "issues": review(ACTION)["issues"] * 4},
            review(ACTION, severity="warning"), review(" "),
            {"verdict": "needs_revision", "issues": [{"severity": "major", "quote": ACTION}]},
        ]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                development.validate_review(payload, STORY)

    def test_quote_whitespace_is_not_normalized_away(self):
        story = STORY.replace("雨夜", "\n雨夜")
        evidence = "门口。\n雨夜"
        self.assertEqual(development.validate_review(review(evidence), story)["issues"][0]["quote"], evidence)

    def test_json_fences_are_allowed_but_duplicates_nan_and_bad_roots_fail(self):
        raw = json.dumps(audience_response(review()), ensure_ascii=False)
        self.assertEqual(development.review_story(brief(), STORY, call=QueueCall("```json\n" + raw + "\n```")), review())
        for invalid in ('{"verdict":"ready","verdict":"needs_revision","issues":[]}',
                        '{"verdict":"ready","issues":NaN}', '[{"verdict":"ready","issues":[]}]',
                        raw + "\n审核结束", {**review(), "issues": float("inf")}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                development.review_story(brief(), STORY, call=QueueCall(invalid))

    def test_invalid_unicode_or_wrong_field_types_never_become_a_review(self):
        for field, value in (("dimension", 123), ("reason", "\ud800"), ("quote", None), ("suggestion", "")):
            result = review(ACTION)
            result["issues"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                development.validate_review(result, STORY)


class StoryRewriteTest(unittest.TestCase):
    def test_unicode_codepoint_selection_preserves_all_outside_text_and_reviews_new_story(self):
        story = "  雨停了🌧️。阿宁将一封信放到木凳上。\n\n她等了很久，直到门边的灯亮起来，才听见熟悉的脚步声。  "
        selected = "她等了很久"
        start = story.index(selected)
        end = start + len(selected)
        replacement = "她将伞上的水轻轻抖落"
        call = QueueCall({"replacement": replacement}, review())
        result = development.propose_rewrite(brief(), story, start, end, "把等待写成可见动作", call=call)
        expected = story[:start] + replacement + story[end:]
        self.assertEqual(result["story"], expected)
        self.assertEqual(result["story"][:start], story[:start])
        self.assertEqual(result["story"][start + len(replacement):], story[end:])
        self.assertEqual(call.payload(0)["selected_text"], selected)
        self.assertEqual(call.payload(1)["story"], expected)
        self.assertEqual(result["replacement"], replacement)

    def test_deletion_is_an_explicit_valid_replacement(self):
        start = STORY.index("临走时")
        end = STORY.index("第二晚")
        result = development.propose_rewrite(brief(), STORY, start, end, "删除这段重复的反应", call=QueueCall({"replacement": ""}, review()))
        self.assertEqual(result["story"], STORY[:start] + STORY[end:])

    def test_invalid_ranges_or_instruction_fail_before_model_call(self):
        cases = [(True, 5, "改写"), (0, 1.5, "改写"), (-1, 5, "改写"), (5, 5, "改写"),
                 (5, 2, "改写"), (0, len(STORY) + 1, "改写"), (0, 5, ""), (0, 5, "x" * 2001)]
        for start, end, instruction in cases:
            call = QueueCall()
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                development.propose_rewrite(brief(), STORY, start, end, instruction, call=call)
            self.assertFalse(call.requests)

    def test_model_cannot_return_full_story_or_offsets_to_change_the_outside(self):
        for payload in ({"story": "全新故事"}, {"replacement": "新片段", "start": 0, "end": len(STORY)},
                        {"replacement": None}, {"replacement": STORY[:3]}):
            call = QueueCall(payload)
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                development.propose_rewrite(brief(), STORY, 0, 3, "使开头自然一些", call=call)
            self.assertEqual(len(call.requests), 1)

    def test_review_failure_cannot_return_an_unreviewed_proposal_as_success(self):
        call = QueueCall({"replacement": "第二天"}, RuntimeError("review unavailable"))
        events = []
        with self.assertRaisesRegex(RuntimeError, "review unavailable"):
            development.propose_rewrite(brief(), STORY, 0, 2, "调整开头", call=call,
                                        progress=lambda stage, data: events.append((stage, data)))
        self.assertFalse(any(stage == "completed" for stage, _ in events))
        self.assertEqual(len(call.requests), 2)

    def test_full_range_rewrite_is_allowed_only_when_explicitly_selected(self):
        replacement = STORY.replace(ACTION, BETTER_ACTION)
        result = development.propose_rewrite(brief(), STORY, 0, len(STORY), "保留人物和事件，澄清行动", call=QueueCall({"replacement": replacement}, review()))
        self.assertEqual(result["story"], replacement)


if __name__ == "__main__":
    unittest.main()
