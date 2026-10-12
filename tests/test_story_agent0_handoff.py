"""Inspect actual Agent0 provider payloads; never call a remote model."""
import copy
import json
import unittest
from unittest.mock import patch

import story_agents
from backend.app.story_production import make_story_snapshot, production_story_handoff
from backend.app.story_writer import canonical_story_brief


STORY = "\n  阿宁把门口的木凳搬到屋檐下，因为她记得送信人常在那里歇脚。\n傍晚，木凳上多了一封没有被雨淋湿的信。\n"
IDEA = "阿宁始终穿蓝色围裙，送信人右手不能提重物；结尾保持克制，不添加旁白说教。"


def response(characters=None):
    return json.dumps({
        "story_type": "everyday", "logline": "雨天的照顾", "theme": "邻里善意", "narrative_tone": "克制",
        "characters": characters or [], "locations": [], "clues_and_payoffs": [],
        "continuity_rules": [], "visual_safety": [],
        # A model is not allowed to replace the frozen handoff with its own text.
        "story_handoff": {"status": "current", "brief": {"idea": "伪造的模型要求"}},
    }, ensure_ascii=False)


class Agent0StoryHandoffTest(unittest.TestCase):
    def setUp(self):
        self.brief = canonical_story_brief(idea=IDEA, preset="warm_relationship", duration_seconds=60, intent="manuscript")
        self.snapshot = make_story_snapshot("a" * 32, 1, STORY, "木凳", self.brief)
        self.handoff = production_story_handoff(self.snapshot, [{"text": STORY}])

    def test_initial_provider_payload_receives_exact_adopted_words_and_brief(self):
        with patch.object(story_agents, "gemini_configured", return_value=True), \
                patch.object(story_agents, "generate_gemini_text", return_value=response()) as generate:
            result = story_agents.create_story_context(STORY, content_mode="general", adopted_story=self.handoff,
                global_character_prompt="阿宁穿蓝色围裙", world_prompt="雨夜的老街", require_ai_success=True)
        generate.assert_called_once()
        payload = json.loads(generate.call_args.kwargs["user_prompt"])
        self.assertEqual(payload["complete_text"], STORY.strip())
        self.assertEqual(payload["adopted_story"], self.handoff)
        self.assertEqual(payload["adopted_story"]["story"], STORY)
        self.assertEqual(payload["adopted_story"]["brief"], self.brief)
        self.assertEqual(payload["user_world_bible"], "雨夜的老街")
        self.assertEqual(payload["user_global_character_bible"], "阿宁穿蓝色围裙")
        self.assertEqual(result["story_handoff"], self.handoff)
        self.assertEqual(result["generation_source"], "gemini")
        self.assertNotIn("伪造的模型要求", json.dumps(result, ensure_ascii=False))

    def test_identity_repair_receives_the_same_frozen_brief_and_world(self):
        ambiguous = [
            {"character_id": "aning", "name": "邻居", "aliases": [], "group_aliases": [], "role": "店主", "appearance": "蓝色围裙"},
            {"character_id": "postman", "name": "邻居", "aliases": [], "group_aliases": [], "role": "送信人", "appearance": "绿色外套"},
        ]
        repaired = copy.deepcopy(ambiguous)
        repaired[0]["name"], repaired[1]["name"] = "阿宁", "送信人"
        for row in repaired:
            row["group_aliases"] = ["邻居"]
        with patch.object(story_agents, "gemini_configured", return_value=True), \
                patch.object(story_agents, "generate_gemini_text",
                             side_effect=[response(ambiguous), response(repaired)]) as generate:
            result = story_agents.create_story_context(STORY, content_mode="general", adopted_story=self.handoff,
                world_prompt="雨夜的老街", require_ai_success=True)
        self.assertEqual(generate.call_count, 2)
        first, retry = [json.loads(call.kwargs["user_prompt"]) for call in generate.call_args_list]
        self.assertIn("validation_errors", retry)
        self.assertIn("previous_output", retry)
        for payload in (first, retry):
            self.assertEqual(payload["adopted_story"], self.handoff)
            self.assertEqual(payload["user_world_bible"], "雨夜的老街")
            self.assertEqual(payload["complete_text"], STORY.strip())
        self.assertEqual([row["character_id"] for row in result["characters"]], ["aning", "postman"])
        self.assertEqual(result["story_handoff"], self.handoff)

    def test_legacy_agent_call_keeps_its_existing_payload_contract(self):
        with patch.object(story_agents, "gemini_configured", return_value=True), \
                patch.object(story_agents, "generate_gemini_text", return_value=response()) as generate:
            result = story_agents.create_story_context(STORY, content_mode="general", require_ai_success=True)
        payload = json.loads(generate.call_args.kwargs["user_prompt"])
        self.assertNotIn("adopted_story", payload)
        self.assertNotIn("story_handoff", result)

    def test_detached_or_legacy_inputs_fail_before_any_provider_work(self):
        for status in ("detached", "legacy_body_only"):
            with self.subTest(status=status), \
                    patch.object(story_agents, "gemini_configured") as configured, \
                    patch.object(story_agents, "generate_gemini_text") as generate:
                with self.assertRaises(ValueError):
                    story_agents.create_story_context(STORY, adopted_story={**self.handoff, "status": status})
                configured.assert_not_called()
                generate.assert_not_called()

    def test_local_fallback_retains_an_independent_frozen_handoff(self):
        before = copy.deepcopy(self.handoff)
        with patch.object(story_agents, "gemini_configured", return_value=False), \
                patch.object(story_agents, "generate_gemini_text") as generate:
            result = story_agents.create_story_context(STORY, adopted_story=self.handoff)
        self.assertEqual(result["story_handoff"], before)
        result["story_handoff"]["brief"]["idea"] = "调用方后续编辑"
        self.assertEqual(self.handoff, before)
        generate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
