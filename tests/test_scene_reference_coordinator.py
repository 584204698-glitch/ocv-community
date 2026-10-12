import copy
import tempfile
import unittest
import json
from unittest.mock import patch
from pathlib import Path
from scene_reference_coordinator import validate_plan, bind_scene_references, plan_scene_references, filter_eligible_members


class SceneReferenceTest(unittest.TestCase):
    def test_mixed_scene_excludes_metaphor_but_keeps_real_pair(self):
        self.mapping[2]["visual_design"]["expression"] = "metaphor"
        raw = copy.deepcopy(self.plan)
        raw["scenes"][0]["members"] = [0, 1, 2]
        filtered = filter_eligible_members(raw, self.mapping)
        self.assertEqual(validate_plan(filtered, self.mapping)[0]["members"], [0, 1])
        self.assertEqual(raw["scenes"][0]["members"], [0, 1, 2])

    def test_ineligible_pair_becomes_cached_no_reference_plan(self):
        self.mapping[0]["visual_design"]["fact_status"] = "hypothetical"
        with tempfile.TemporaryDirectory() as directory, patch("scene_reference_coordinator.generate_gemini_text", return_value=json.dumps(self.plan)) as call:
            cache = Path(directory) / "plan.json"
            result = plan_scene_references(self.mapping, [], cache)
            self.assertEqual(result["scenes"], [])
            self.assertTrue(result["exclusions"])
            self.assertEqual(plan_scene_references(self.mapping, [], cache), result)
            self.assertEqual(call.call_count, 1)

    def test_model_labels_are_normalized_and_cached_without_retry(self):
        for label in ("出租屋/餐桌", "scene-1", "", None):
            raw = copy.deepcopy(self.plan)
            raw["scenes"][0]["scene_id"] = label
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory, patch(
                "scene_reference_coordinator.generate_gemini_text", return_value=json.dumps(raw)
            ) as generate:
                cache = Path(directory) / "plan.json"
                result = plan_scene_references(self.mapping, [], cache)
                self.assertEqual(result["scenes"][0]["scene_id"], "location_1")
                self.assertEqual(result["scenes"][0]["members"], [0, 1])
                self.assertEqual(result, plan_scene_references(self.mapping, [], cache))
                self.assertEqual(generate.call_count, 1)

    def test_normalization_does_not_relax_membership_validation(self):
        raw = copy.deepcopy(self.plan)
        raw["scenes"][0].update(scene_id="任意编号", members=[0, 99])
        with tempfile.TemporaryDirectory() as directory, patch(
            "scene_reference_coordinator.generate_gemini_text", return_value=json.dumps(raw)
        ):
            with self.assertRaises(ValueError):
                plan_scene_references(self.mapping, [], Path(directory) / "plan.json")

    def test_same_plan_reuses_cache_without_llm(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "scene_reference_coordinator.generate_gemini_text", return_value=json.dumps(self.plan)
        ) as generate:
            cache = Path(directory) / "plan.json"
            first = plan_scene_references(self.mapping, [], cache)
            second = plan_scene_references(self.mapping, [], cache)
            self.assertEqual(first, second)
            self.assertEqual(generate.call_count, 1)

    def setUp(self):
        self.mapping = [{"image_prompt": "桌面", "visual_design": {"fact_status": "fact"}},
                        {"image_prompt": "桌子特写", "visual_design": {"fact_status": "fact"}},
                        {"image_prompt": "地铁", "visual_design": {"fact_status": "fact"}}]
        self.plan = {"scenes": [{"scene_id": "location_1", "members": [0, 1], "reference_prompt": "无人餐桌", "reason": "同一现场"}]}

    def test_only_related_shots_receive_scene(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scene.jpg"
            path.write_bytes(b"test")
            before = copy.deepcopy(self.mapping)
            result = bind_scene_references(self.mapping, self.plan, {"location_1": path}, {})
            self.assertEqual(self.mapping, before)
            self.assertIn("scene_reference", result[0])
            self.assertIn("scene_reference", result[1])
            self.assertNotIn("reference_image_paths", result[2])
            self.assertEqual(result[2], self.mapping[2])

    def test_rejects_singleton_overlap_unknown_and_metaphor(self):
        for members in ([0], [0, 0], [0, 9]):
            with self.subTest(members=members), self.assertRaises(ValueError):
                validate_plan({"scenes": [{**self.plan["scenes"][0], "members": members}]}, self.mapping)
        self.mapping[0]["visual_design"]["fact_status"] = "metaphorical"
        with self.assertRaises(ValueError):
            validate_plan(self.plan, self.mapping)

    def test_exact_character_selection_and_numbering(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scene.jpg"
            path.write_bytes(b"test")
            self.mapping[0].update(image_prompt="参考图2人物", reference_image_ids=["图2"])
            result = bind_scene_references(self.mapping, self.plan, {"location_1": path}, {"图1": "unused", "图2": "selected"})
            self.assertEqual(result[0]["reference_image_paths"], ["selected", str(path.resolve())])
            self.assertIn("参考图1人物", result[0]["image_prompt"])
            self.assertEqual(result[0]["scene_reference"]["input_number"], 2)

    def test_dynamic_scene_payload_preserves_action_routes_and_obstacle_states(self):
        from backend.app.video_scene_references import plan_references
        record = {
            "settings": {"style": "写实", "world": "仓库", "ratio": "16:9"},
            "scenes": [{"slide_id": "s1", "text": "工人先移开挡路的木箱，再将货物推进仓库。"}],
            "shots": [{
                "id": "arrival", "image_prompt": "木箱挡着仓库入口，工人停在门外。",
                "native_dialogue": {
                    "scene": "The doorway connects to the aisle beside the window.",
                    "speakers": [{"character_id": "pc_worker", "kind": "character",
                                  "on_screen": True, "presence": "opening",
                                  "position": "Outside the doorway", "voice_id": "private-voice"}],
                    "performance_beats": [{"start": 0, "end": 4,
                                           "action": "Move the crate against the wall.",
                                           "action_zh": "将木箱移到墙边。"}],
                },
                "action": "先把木箱挪到墙边，工人再从门外向库内推行。",
                "motion_basis": "木箱阻挡通行，必须先挪开。",
                "progression_plan": "入口受阻→移箱→向内通行。",
                "semantic": {"continuity_requirement": "木箱造型与大小不变；移到墙边后不能回到通路。"},
                "motion_plan": {
                    "scene_anchor": "仓库门口及内侧通路",
                    "reference_visual": "木箱仍在入口，尚未被移开。",
                    "beats": [{"action": "移开木箱。", "texts": []},
                              {"action": "车头朝向库内，从门外推进。", "texts": []}],
                },
            }],
        }
        before = copy.deepcopy(record)
        with tempfile.TemporaryDirectory() as directory, patch(
            "scene_reference_coordinator.generate_gemini_text", return_value='{"scenes": []}'
        ) as generate:
            plan_references(Path(directory), record)
            payload = json.loads(generate.call_args.kwargs["user_prompt"])
        design = payload["items"][0]["visual_design"]
        self.assertEqual(design["motion_design"]["motion_plan"], record["shots"][0]["motion_plan"])
        self.assertEqual(design["motion_design"]["action"], record["shots"][0]["action"])
        native = design['motion_design']['native_staging']
        self.assertEqual(native['scene'], record['shots'][0]['native_dialogue']['scene'])
        self.assertEqual(native['performance_beats'], record['shots'][0]['native_dialogue']['performance_beats'])
        self.assertEqual(native['participants'][0]['position'], 'Outside the doorway')
        self.assertNotIn('voice_id', native['participants'][0])
        native['performance_beats'][0]['action'] = 'Changed outside record'
        self.assertEqual(record, before)
        self.assertEqual(design["motion_design"]["progression_plan"], record["shots"][0]["progression_plan"])
        self.assertEqual(design["semantic"], record["shots"][0]["semantic"])
        self.assertEqual(payload["source"]["subtitles"], record["scenes"])
        self.assertEqual(record, before)

    def test_dynamic_scene_cache_changes_when_later_route_changes_with_same_opening(self):
        from backend.app.video_scene_references import plan_references
        record = {"settings": {}, "scenes": [], "shots": [{
            "id": "entry", "image_prompt": "同一仓库入口，工人站在车旁。",
            "motion_plan": {"scene_anchor": "仓库入口", "reference_visual": "工人站在车旁。",
                            "beats": [{"action": "从门外推进库内。", "texts": []}]},
        }]}
        with tempfile.TemporaryDirectory() as directory, patch(
            "scene_reference_coordinator.generate_gemini_text", return_value='{"scenes": []}'
        ) as generate:
            root = Path(directory)
            first = plan_references(root, record)
            self.assertEqual(plan_references(root, record), first)
            self.assertEqual(generate.call_count, 1)
            record["shots"][0]["motion_plan"]["beats"][0]["action"] = "先绕开入口内侧的支柱，再推向右侧通道。"
            changed = plan_references(root, record)
            self.assertEqual(generate.call_count, 2)
            self.assertNotEqual(changed["fingerprint"], first["fingerprint"])
            self.assertEqual(plan_references(root, record), changed)
            self.assertEqual(generate.call_count, 2)

    def test_native_staging_change_invalidates_scene_plan_but_voice_change_does_not(self):
        from backend.app.video_scene_references import plan_references
        record = {'settings': {}, 'scenes': [], 'shots': [{
            'id': 'entry', 'image_prompt': 'An open doorway.',
            'native_dialogue': {'scene': 'Doorway beside the window.',
                'speakers': [{'character_id': 'pc_a', 'position': 'Outside doorway',
                              'voice_id': 'first'}],
                'performance_beats': [{'start': 0, 'end': 4, 'action': 'Enter the room.'}]}}]}
        with tempfile.TemporaryDirectory() as directory, patch(
            'scene_reference_coordinator.generate_gemini_text', return_value='{"scenes": []}'
        ) as generate:
            root = Path(directory)
            first = plan_references(root, record)
            native = record['shots'][0]['native_dialogue']
            native['speakers'][0]['voice_id'] = 'second'
            self.assertEqual(plan_references(root, record), first)
            self.assertEqual(generate.call_count, 1)
            native['performance_beats'][0]['action'] = 'Enter along the wall, avoiding the table.'
            changed = plan_references(root, record)
            self.assertNotEqual(changed['fingerprint'], first['fingerprint'])
            self.assertEqual(generate.call_count, 2)
            self.assertEqual(plan_references(root, record), changed)
            self.assertEqual(generate.call_count, 2)

    def test_dynamic_scene_cache_ignores_transport_suffixes_and_local_numbering(self):
        from backend.app.video_scene_references import plan_references
        record={'settings':{},'scenes':[], 'references':[
            {'id':'rabbit','label':'图1'},{'id':'turtle','label':'图3'}],
            'shots':[{'id':'door','reference_ids':['turtle','rabbit'],
                      'image_prompt':'图3在门外，图1站在门内。'}]}
        with tempfile.TemporaryDirectory() as directory, patch(
            'scene_reference_coordinator.generate_gemini_text',return_value='{"scenes": []}'
        ) as generate:
            root=Path(directory);first=plan_references(root,record)
            shot=record['shots'][0]
            shot.update(image_material_numbers_bound=True,
                image_prompt='图1在门外，图2站在门内。\n【已选项目角色】\n图1是乌龟。\n【原生镜头开场构图】\n门口。\n【场景参考】图3保持背景。')
            self.assertEqual(plan_references(root,record),first)
            self.assertEqual(generate.call_count,1)
            shot['image_prompt']='图1在窗边，图2站在门内。'
            self.assertNotEqual(plan_references(root,record)['fingerprint'],first['fingerprint'])
            self.assertEqual(generate.call_count,2)

    def test_dynamic_scene_legacy_action_updates_are_not_hidden_by_missing_motion_plan(self):
        from backend.app.video_scene_references import plan_references
        record = {"settings": {}, "scenes": [], "shots": [{
            "id": "repair", "image_prompt": "工人检查损坏的桥板。",
            "action": "桥板仍有破损，工人止步检查。",
        }]}
        with tempfile.TemporaryDirectory() as directory, patch(
            "scene_reference_coordinator.generate_gemini_text", return_value='{"scenes": []}'
        ) as generate:
            first = plan_references(Path(directory), record)
            record["shots"][0]["action"] = "工人更换损坏桥板后才通行。"
            changed = plan_references(Path(directory), record)
            payload = json.loads(generate.call_args.kwargs["user_prompt"])
        self.assertEqual(generate.call_count, 2)
        self.assertNotEqual(first["fingerprint"], changed["fingerprint"])
        self.assertEqual(payload["items"][0]["visual_design"]["motion_design"]["action"],
                         "工人更换损坏桥板后才通行。")
        self.assertEqual(payload["items"][0]["visual_design"]["motion_design"]["motion_plan"], {})
