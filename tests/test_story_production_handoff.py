"""Story adoption and production handoff at owner-scoped API boundaries.

These checks use saved drafts and actual route logic. Language calls, media
generation and external services are forbidden; later stages use boundary doubles.
"""
import ast
import copy
import json
import sys
import tempfile
import unittest
import wave
from contextlib import ExitStack, nullcontext
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.requests import Request

from backend.app import story_workspace as stories


STORY = "阿宁把门口的木凳搬到屋檐下，因为她记得送信人常在那里歇脚。傍晚，木凳上多了一封没有被雨淋湿的信。"
REVISED_STORY = "阿宁把门口的木凳搬回了屋里，因为她以为送信人今天不会来了。傍晚，她听见门外响起脚步，赶紧把木凳搬了出去。"
BRIEF_A = "用日常动作表现邻里间克制的善意，不加说教。"
BRIEF_B = "保留误会，侧重人物在雨夜里的犹豫与反思。"


class StoryProductionFixture:
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.identity = "a" * 32

        def owner(request):
            return {"id": int(request.headers.get("x-test-owner", "1"))}

        for name, value in (("ROOT", self.root), ("ACTIVE", set()), ("require_user", owner)):
            handle = patch.object(stories, name, value)
            handle.start()
            self.addCleanup(handle.stop)
        generator = patch.object(stories, "_generate_story",
            side_effect=AssertionError("Production handoff must not call a language model"))
        self.model = generator.start()
        self.addCleanup(generator.stop)
        starter = patch.object(stories, "_start_generation",
            side_effect=AssertionError("Adoption must not schedule generation"))
        self.start = starter.start()
        self.addCleanup(starter.stop)
        app = FastAPI()
        app.include_router(stories.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def tearDown(self):
        self.model.assert_not_called()
        self.start.assert_not_called()

    def ok(self, response):
        self.assertIn(response.status_code, (200, 201, 202), response.text)
        return response.json()

    def create(self, *, owner=1, identity=None, **changes):
        return self.ok(self.client.post("/api/stories", headers={"x-test-owner": str(owner)}, json={
            "id": identity or self.identity, "intent": "manuscript", "idea": BRIEF_A,
            "preset": "warm_relationship", "duration_seconds": 60,
            "title": "雨中的木凳", "story": STORY, **changes,
        }))

    def update(self, record, *, owner=1, **changes):
        body = {key: record["brief"][key] for key in ("intent", "idea", "preset", "duration_seconds")}
        body.update(title=record["title"], story=record["story"], revision=record["revision"])
        body.update(changes)
        return self.ok(self.client.put("/api/stories/" + record["id"], json=body,
                                      headers={"x-test-owner": str(owner)}))

    def adopt(self, record, *, owner=1):
        return self.ok(self.client.post("/api/stories/" + record["id"] + "/adopt",
            json={"revision": record["revision"]}, headers={"x-test-owner": str(owner)}))

    @staticmethod
    def legacy_source(source):
        return {key: source[key] for key in ("story_id", "story_revision", "story_sha256")}

    def resolve(self, adopted, *, owner=1, story=STORY):
        return stories.resolve_story_source(owner, adopted["source"], story)

    def assert_conflict(self, source, *, owner=1, story=STORY, status=409):
        with self.assertRaises(HTTPException) as caught:
            stories.resolve_story_source(owner, source, story)
        self.assertEqual(caught.exception.status_code, status)


class StoryProductionAdoptionTest(StoryProductionFixture, unittest.TestCase):
    def test_adoption_freezes_canonical_brief_with_the_exact_body(self):
        created = self.create(story="\n  " + STORY + "\n")
        adopted = self.adopt(created)
        self.assertTrue(adopted["source"]["adoption_id"])
        self.assertEqual(stories.StorySource.model_validate(adopted["source"]).model_dump(exclude_none=True),
                         adopted["source"])
        frozen = self.resolve(adopted, story=created["story"])
        self.assertEqual(frozen["story"], created["story"])
        self.assertEqual(frozen["brief"], created["brief"])
        self.assertEqual(frozen["story_sha256"], adopted["source"]["story_sha256"])
        self.assertEqual(frozen["adoption_id"], adopted["source"]["adoption_id"])
        for field in ("synopsis", "characters", "scenes", "development", "review", "rewrite_proposal"):
            self.assertNotIn(field, frozen)
        self.assertEqual(adopted["record"]["story_revision"], created["story_revision"])
        self.assertEqual(adopted["record"]["versions"], created["versions"])

    def test_repeated_adoption_of_same_body_and_brief_reuses_identity(self):
        first = self.adopt(self.create())
        again = self.adopt(first["record"])
        self.assertEqual(again["source"], first["source"])
        self.assertEqual(self.resolve(again), self.resolve(first))

    def test_brief_only_edit_creates_new_adoption_and_preserves_old_brief(self):
        first = self.adopt(self.create())
        old_snapshot = copy.deepcopy(self.resolve(first))
        changed = self.update(first["record"], idea=BRIEF_B, duration_seconds=90)
        second = self.adopt(changed)
        self.assertEqual(self.legacy_source(second["source"]), self.legacy_source(first["source"]))
        self.assertNotEqual(second["source"]["adoption_id"], first["source"]["adoption_id"])
        self.assertEqual(self.resolve(first), old_snapshot)
        self.assertEqual(self.resolve(second)["brief"], changed["brief"])
        self.assertNotEqual(self.resolve(second)["brief"], old_snapshot["brief"])

    def test_title_only_edit_keeps_body_context_identity(self):
        first = self.adopt(self.create())
        changed = self.update(first["record"], title="留在屋檐下")
        second = self.adopt(changed)
        self.assertEqual(second["title"], "留在屋檐下")
        self.assertEqual(second["source"], first["source"])
        self.assertEqual(self.resolve(second)["brief"], self.resolve(first)["brief"])

    def test_historical_adoption_stays_frozen_after_later_body_and_brief_edit(self):
        first = self.adopt(self.create())
        frozen = copy.deepcopy(self.resolve(first))
        changed = self.update(first["record"], story=REVISED_STORY, idea=BRIEF_B)
        second = self.adopt(changed)
        self.assertEqual(self.resolve(first), frozen)
        self.assertEqual(self.resolve(second, story=REVISED_STORY)["brief"], changed["brief"])
        self.assert_conflict(first["source"], story=REVISED_STORY)

    def test_legacy_three_field_source_never_borrows_current_brief(self):
        first = self.adopt(self.create())
        legacy = self.legacy_source(first["source"])
        changed = self.update(first["record"], idea=BRIEF_B)
        self.adopt(changed)
        resolved = stories.resolve_story_source(1, legacy, STORY)
        self.assertEqual(resolved["story"], STORY)
        self.assertEqual(self.legacy_source(resolved), legacy)
        self.assertNotIn("brief", resolved)
        self.assertNotIn("adoption_id", resolved)
        parsed = stories.StorySource.model_validate(legacy)
        self.assertEqual(parsed.model_dump(exclude_none=True), legacy)

    def test_unknown_adoption_id_is_rejected_instead_of_falling_back_to_body(self):
        adopted = self.adopt(self.create())
        self.assert_conflict({**adopted["source"], "adoption_id": "f" * 32})

    def test_adoption_identity_cannot_be_borrowed_from_another_owned_story(self):
        first = self.adopt(self.create())
        other = self.adopt(self.create(identity="b" * 32, idea=BRIEF_B))
        forged = {**first["source"], "adoption_id": other["source"]["adoption_id"]}
        self.assert_conflict(forged)

    def test_foreign_owner_cannot_resolve_an_adoption(self):
        first = self.adopt(self.create())
        self.assert_conflict(first["source"], owner=2, status=404)
        # Even the same public draft ID in another account names a distinct record.
        other = self.adopt(self.create(owner=2, idea=BRIEF_B), owner=2)
        self.assert_conflict(first["source"], owner=2)
        self.assertEqual(self.resolve(other, owner=2)["brief"]["idea"], BRIEF_B)

    def test_client_cannot_inject_a_brief_or_derived_roles_into_source_contract(self):
        adopted = self.adopt(self.create())
        for extra in ({"brief": {"idea": BRIEF_B}}, {"characters": [{"character_id": "invented"}]},
                      {"story": REVISED_STORY}, {"synopsis": "伪造剧情"}):
            with self.subTest(extra=extra), self.assertRaises(ValidationError):
                stories.StorySource.model_validate({**adopted["source"], **extra})


class StoryProductionPipelineTest(StoryProductionFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        from backend.app import video_agents, video_plan, video_sources, video_studio
        self.studio, self.plan, self.agents, self.sources = video_studio, video_plan, video_agents, video_sources
        self.boundaries = ExitStack()
        self.addCleanup(self.boundaries.close)
        for name, value in (("ROOT", self.root / "videos"), ("ACTIVE", set()),
                            ("IMAGE_EDITS", set()), ("require_user", lambda request: {"id": 1})):
            self.boundaries.enter_context(patch.object(self.studio, name, value))
        self.source_dir = self.root / "source-output"
        (self.source_dir / "input").mkdir(parents=True)
        (self.source_dir / "other").mkdir()
        with wave.open(str(self.source_dir / "input" / "配音.wav"), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(800)
            stream.writeframes(b"\0\0" * 6400)
        self.write_subtitles(STORY)
        self.pipeline = ModuleType("backend.app.pipeline")
        self.pipeline.validate_step_audio_snapshot = Mock()
        self.pipeline.user_reference_image_path = Mock(side_effect=AssertionError("No reference assets in this fixture"))
        self.tts = ModuleType("backend.app.tts_editor")
        self.tts.tts_editor = SimpleNamespace(status=Mock(return_value={"status": "idle"}), commit_step_review=Mock())
        self.boundaries.enter_context(patch.dict(sys.modules, {
            "backend.app.pipeline": self.pipeline, "backend.app.tts_editor": self.tts,
        }))

    def write_subtitles(self, body):
        first, second = body.split("。", 1)
        # The adopted prose retains punctuation; subtitle formatting does not.
        text = "1\n00:00:00,000 --> 00:00:04,000\n" + first + "\n\n"
        text += "2\n00:00:04,000 --> 00:00:08,000\n" + second.strip("。") + "\n"
        (self.source_dir / "other" / "最终字幕.srt").write_text(text, encoding="utf-8")

    def import_adopted(self, *, audio_task=True, legacy=False):
        adopted = self.adopt(self.create())
        source = self.legacy_source(adopted["source"]) if legacy else copy.deepcopy(adopted["source"])
        request = {"dynamic_video": True, "script": STORY, "project_name": "采用故事制作",
                   "story_source": source, "story_snapshot": stories.resolve_story_source(1, source, STORY),
                   "content_mode": "general", "global_character_prompt": "保持阿宁的蓝色围裙",
                   "story_environment_prompt": "雨天的老街屋檐", "visual_style_prompt": "水彩",
                   "private_api_key": "test-only-not-a-real-key"}
        self.job = SimpleNamespace(id="fixture-audio-job", user_id=1, request=request)
        with patch.object(self.studio, "source_project", return_value=(self.job, self.source_dir)):
            record = self.studio.import_new_project(self.studio.ImportProject(
                job_id=self.job.id, from_audio_task=audio_task), {"id": 1})
        return adopted, record

    def run_plan(self, record, *, resume_state=None):
        scenes = record["scenes"]
        raw_shot = {"id": "shot-A", "kind": "video", "slide_ids": [row["slide_id"] for row in scenes],
                    "intent": "看见一次邻里间的照顾"}
        motion = {"version": 2, "scene_anchor": "老街屋檐下", "participants": [],
                  "beats": [{"action": "雨水沿屋檐滑落", "texts": []}], "reference_beat": 1,
                  "reference_visual": "屋檐下的一张木凳", "reference_participants": [], "reference_texts": []}
        doubles = {
            "plan_groups": Mock(return_value=[raw_shot]),
            "design_core_images": Mock(return_value=[{"id": "shot-A", "visual_description": "屋檐下的木凳"}]),
            "direct_motion": Mock(return_value=[{"id": "shot-A", "motion_plan": motion, "action": "雨水落下"}]),
            "write_image_prompts": Mock(return_value=[{"id": "shot-A", "image_prompt": "屋檐下的木凳"}]),
            "write_video_prompts": Mock(return_value=[{"id": "shot-A", "video_prompt": "雨水滑落"}]),
            "audit_storyboard": Mock(side_effect=lambda rows, reference_ids: rows),
        }
        agent0 = Mock(return_value={"characters": [], "summary": "fixture understanding"})
        reading = ModuleType("story_agents")
        reading.create_story_context = agent0
        with ExitStack() as stack:
            stack.enter_context(patch.dict(sys.modules, {"story_agents": reading}))
            for name, mock in doubles.items():
                stack.enter_context(patch.object(self.agents, name, mock))
            settings = record["settings"]
            context, shots = self.plan.plan_storyboard(scenes, settings["style"], settings["characters"],
                settings["world"], record["references"], lambda message: None,
                self.studio.planning_parameters(record), resume_state=resume_state)
        return context, shots, agent0, doubles

    def refresh_single(self, record):
        record["shots"][0]["action"] = ""
        record["status"] = "storyboard_review"
        self.studio.save(self.studio.directory(1, record["id"]), record)
        motion = {"version": 2, "scene_anchor": "老街屋檐下", "participants": [],
                  "beats": [{"action": "雨水沿屋檐滑落", "texts": []}], "reference_beat": 1,
                  "reference_visual": "屋檐下的一张木凳", "reference_participants": [], "reference_texts": []}
        model_response = json.dumps({"shots": [{"id": "shot-A", "motion_plan": motion}]}, ensure_ascii=False)
        with patch.object(self.agents, "generate_gemini_text", return_value=model_response) as language, \
                patch.object(self.studio, "project_language_scope", return_value=nullcontext()):
            changed = self.studio.refresh_shot_prompts(record["id"], "shot-A", self.studio.ShotPromptRefresh(
                revision=record["revision"], basis="image", image_prompt="屋檐下的一张木凳", action_only=True),
                Request({"type": "http", "headers": []}))
        language.assert_called_once()
        return changed, json.loads(language.call_args.kwargs["user_prompt"])

    def test_audio_task_import_preserves_adopted_body_and_frozen_brief(self):
        adopted, record = self.import_adopted()
        self.assertEqual(record["creation_parameters"]["story_source"], adopted["source"])
        self.assertNotIn("private_api_key", record["creation_parameters"])
        handoff = self.studio.planning_parameters(record)["story_handoff"]
        self.assertEqual(handoff["status"], "current")
        self.assertEqual(handoff["source"], adopted["source"])
        self.assertEqual(handoff["story"], STORY)
        self.assertEqual(handoff["brief"], adopted["record"]["brief"])
        returned = self.studio.get(record["id"], Request({"type": "http", "headers": []}))
        self.assertEqual(returned["story_handoff"], handoff)
        self.pipeline.validate_step_audio_snapshot.assert_called_once_with(self.job)
        self.tts.tts_editor.commit_step_review.assert_called_once_with(job=self.job, user_id=1)

    def test_invalid_import_snapshot_stops_before_audio_commit_or_asset_copy(self):
        _, _ = self.import_adopted()
        original = copy.deepcopy(self.job.request)
        before = sorted(path.relative_to(self.root / "videos").as_posix()
                        for path in (self.root / "videos").rglob("*"))
        for cause in ("changed_brief", "mismatched_adoption"):
            with self.subTest(cause=cause):
                self.job.request = copy.deepcopy(original)
                if cause == "changed_brief":
                    self.job.request["story_snapshot"]["brief"]["idea"] = BRIEF_B
                else:
                    self.job.request["story_source"]["adoption_id"] = "f" * 32
                self.tts.tts_editor.status.reset_mock()
                self.tts.tts_editor.commit_step_review.reset_mock()
                self.pipeline.validate_step_audio_snapshot.reset_mock()
                with patch.object(self.studio, "source_project", return_value=(self.job, self.source_dir)), \
                        patch.object(self.studio, "copy_assets") as copy_assets:
                    with self.assertRaises(HTTPException) as caught:
                        self.studio.import_new_project(self.studio.ImportProject(
                            job_id=self.job.id, from_audio_task=True), {"id": 1})
                self.assertEqual(caught.exception.status_code, 409)
                self.tts.tts_editor.status.assert_not_called()
                self.tts.tts_editor.commit_step_review.assert_not_called()
                self.pipeline.validate_step_audio_snapshot.assert_not_called()
                copy_assets.assert_not_called()
                self.assertEqual(sorted(path.relative_to(self.root / "videos").as_posix()
                                        for path in (self.root / "videos").rglob("*")), before)

    def test_adopted_context_reaches_every_dynamic_director_without_becoming_image_evidence(self):
        adopted, record = self.import_adopted()
        context, shots, reader, directors = self.run_plan(record)
        reader.assert_called_once()
        self.assertEqual(reader.call_args.args[0], STORY)
        handoff = reader.call_args.kwargs["adopted_story"]
        self.assertEqual(handoff["status"], "current")
        self.assertEqual(handoff["brief"], adopted["record"]["brief"])
        self.assertEqual(context["story_handoff"], handoff)
        for phase in ("plan_groups", "design_core_images", "direct_motion", "write_image_prompts", "write_video_prompts"):
            with self.subTest(phase=phase):
                directors[phase].assert_called_once()
                self.assertEqual(directors[phase].call_args.args[0]["story_handoff"], handoff)
        self.assertEqual(shots[0]["source_subtitles"], record["scenes"])
        self.assertEqual(shots[0]["start"], 0)
        self.assertEqual(shots[0]["end"], 8)
        self.assertFalse(shots[0]['reference_audio_lipsync'])
        self.assertTrue(shots[0]['reference_audio_enabled'])
        self.assertNotIn("video_character_review", shots[0])

    def test_legacy_job_is_body_only_and_does_not_invent_a_brief(self):
        _, record = self.import_adopted(legacy=True)
        handoff = self.studio.planning_parameters(record)["story_handoff"]
        self.assertEqual(handoff["status"], "legacy_body_only")
        self.assertNotIn("brief", handoff)
        context, shots, reader, _ = self.run_plan(record)
        self.assertIsNone(reader.call_args.kwargs.get("adopted_story"))
        self.assertNotIn("brief", context["story_handoff"])
        self.assertFalse(shots[0]['reference_audio_lipsync'])

    def test_general_source_import_does_not_inherit_story_or_visual_configuration(self):
        _, record = self.import_adopted(audio_task=False)
        self.assertNotIn("story_snapshot", record["creation_parameters"])
        self.assertNotIn("story_source", record["creation_parameters"])
        self.assertEqual(record["settings"]["characters"], "")
        self.assertEqual(record["settings"]["world"], "")
        self.pipeline.validate_step_audio_snapshot.assert_not_called()

    def test_cached_understanding_cannot_cross_an_adoption_with_a_changed_brief(self):
        first, record = self.import_adopted()
        old_context, _, _, _ = self.run_plan(record)
        settings = record["settings"]
        old_fingerprint = self.plan.planning_fingerprint(record["scenes"], settings["style"],
            settings["characters"], settings["world"], record["references"], self.studio.planning_parameters(record))
        old_context["obsolete_marker"] = "old adoption understanding"
        changed = self.update(first["record"], idea=BRIEF_B)
        second = self.adopt(changed)
        record["creation_parameters"].update(story_source=second["source"], story_snapshot=self.resolve(second))
        context, _, reader, _ = self.run_plan(record, resume_state={"fingerprint": old_fingerprint, "context": old_context})
        reader.assert_called_once()
        self.assertEqual(context["story_handoff"]["brief"]["idea"], BRIEF_B)
        self.assertEqual(context["story_handoff"]["source"], second["source"])
        self.assertNotIn("obsolete_marker", context)

    def test_matching_adoption_resume_reuses_the_same_understanding(self):
        _, record = self.import_adopted()
        original, _, _, _ = self.run_plan(record)
        original["resumed_marker"] = "saved understanding"
        context, _, reader, _ = self.run_plan(record, resume_state={
            "fingerprint": original["production_fingerprint"], "context": original})
        reader.assert_not_called()
        self.assertEqual(context["resumed_marker"], "saved understanding")
        self.assertEqual(context["story_handoff"], original["story_handoff"])

    def test_scene_coordinator_payload_and_cache_follow_the_current_adoption(self):
        from backend.app.video_scene_references import plan_references
        first, record = self.import_adopted()
        _, record["shots"], _, _ = self.run_plan(record)
        path = self.studio.directory(1, record["id"])
        with patch("scene_reference_coordinator.generate_gemini_text", return_value='{"scenes": []}') as generate:
            original = plan_references(path, record)
            self.assertEqual(plan_references(path, record), original)
            self.assertEqual(generate.call_count, 1)
            payload = json.loads(generate.call_args.kwargs["user_prompt"])
            self.assertEqual(payload["source"]["story_handoff"]["story"], STORY)
            self.assertEqual(payload["source"]["story_handoff"]["brief"], first["record"]["brief"])
            self.assertEqual(payload["source"]["subtitles"], record["scenes"])

            second = self.adopt(self.update(first["record"], idea=BRIEF_B))
            record["creation_parameters"].update(story_source=second["source"], story_snapshot=self.resolve(second))
            changed = plan_references(path, record)
            self.assertEqual(generate.call_count, 2)
            self.assertNotEqual(changed["fingerprint"], original["fingerprint"])
            payload = json.loads(generate.call_args.kwargs["user_prompt"])
            self.assertEqual(payload["source"]["story_handoff"]["brief"]["idea"], BRIEF_B)

            record["scenes"][0]["text"] = record["scenes"][0]["text"].replace("搬到屋檐下", "搬回了屋里")
            plan_references(path, record)
            self.assertEqual(generate.call_count, 3)
            payload = json.loads(generate.call_args.kwargs["user_prompt"])
            self.assertEqual(payload["source"]["story_handoff"]["status"], "detached")
            self.assertNotIn("brief", payload["source"]["story_handoff"])
            self.assertNotIn("story", payload["source"]["story_handoff"])

    def test_single_shot_refresh_rebuilds_current_frozen_handoff_without_cached_context(self):
        first, record = self.import_adopted()
        _, shots, _, _ = self.run_plan(record)
        record["shots"] = shots
        record.pop("context", None)
        _, payload = self.refresh_single(record)
        self.assertEqual(payload["story_context"]["story_handoff"]["status"], "current")
        self.assertEqual(payload["story_context"]["story_handoff"]["brief"], first["record"]["brief"])
        self.assertEqual(payload["story_context"]["story_handoff"]["story"], STORY)
        self.assertEqual(payload["story_context"]["production_text"],
                         "\n".join(row["text"] for row in record["scenes"]))
        self.assertEqual(payload["shot"]["source_subtitles"], record["scenes"])

    def test_single_shot_refresh_rejects_a_context_from_another_adoption(self):
        first, record = self.import_adopted()
        old_context, shots, _, _ = self.run_plan(record)
        old_context["obsolete_marker"] = "old adoption understanding"
        record.update(context=old_context, shots=shots)
        changed = self.update(first["record"], idea=BRIEF_B)
        second = self.adopt(changed)
        record["creation_parameters"].update(story_source=second["source"], story_snapshot=self.resolve(second))
        _, payload = self.refresh_single(record)
        self.assertEqual(payload["story_context"]["story_handoff"]["brief"]["idea"], BRIEF_B)
        self.assertNotIn("obsolete_marker", payload["story_context"])

    def test_same_count_subtitle_edit_detaches_the_frozen_brief_before_replanning(self):
        _, record = self.import_adopted()
        context, shots, _, _ = self.run_plan(record)
        record.update(context=context, shots=shots, status="storyboard_review",
                      manual_groups=[{"id": row["id"], "slide_ids": row["slide_ids"]} for row in shots])
        path = self.studio.directory(1, record["id"])
        self.studio.save(path, record)
        frozen = copy.deepcopy(record["creation_parameters"]["story_snapshot"])
        self.write_subtitles(REVISED_STORY)
        store = SimpleNamespace(get=Mock(return_value=self.job))
        editor = SimpleNamespace(output_dir=Mock(return_value=self.source_dir))
        with patch.object(self.sources, "dependencies", return_value=(store, editor, None)):
            changed = self.studio.sync_edited_audio(record["id"], self.studio.Review(revision=record["revision"]),
                Request({"type": "http", "headers": []}))
        self.assertEqual(len(changed["scenes"]), len(record["scenes"]))
        handoff = self.studio.planning_parameters(changed)["story_handoff"]
        self.assertEqual(handoff["status"], "detached")
        self.assertNotIn("story", handoff)
        self.assertNotIn("brief", handoff)
        self.assertEqual(changed["creation_parameters"]["story_snapshot"], frozen)
        changed, refresh_payload = self.refresh_single(changed)
        self.assertEqual(refresh_payload["shot"]["source_subtitles"], changed["scenes"])
        self.assertEqual(refresh_payload["story_context"]["story_handoff"]["status"], "detached")
        self.assertNotIn("brief", refresh_payload["story_context"]["story_handoff"])
        self.assertEqual(refresh_payload["story_context"]["production_text"],
                         "\n".join(row["text"] for row in changed["scenes"]))
        current_context, _, reader, directors = self.run_plan(changed)
        self.assertEqual(reader.call_args.args[0], "\n".join(row["text"] for row in changed["scenes"]))
        self.assertIsNone(reader.call_args.kwargs.get("adopted_story"))
        self.assertEqual(current_context["story_handoff"]["status"], "detached")
        for phase in ("plan_groups", "design_core_images", "direct_motion", "write_image_prompts", "write_video_prompts"):
            self.assertNotIn("brief", directors[phase].call_args.args[0]["story_handoff"])

        # Exercise the route's fixed-group resume branch, which used to attach a
        # fresh fingerprint to an old context after a same-count subtitle edit.
        planner = Mock(return_value=(current_context, changed["shots"]))
        with patch.object(self.studio, "plan_storyboard", planner), \
                patch.object(self.studio, "project_language_scope", return_value=nullcontext()), \
                patch.object(self.studio.threading, "Thread",
                             side_effect=lambda target, daemon: SimpleNamespace(start=target)):
            self.studio.plan(changed["id"], Request({"type": "http", "headers": []}))
        planner.assert_called_once()
        reused = (planner.call_args.kwargs.get("resume_state") or {}).get("context")
        self.assertNotEqual(reused, context)
        if reused:
            self.assertNotEqual(reused.get("story_handoff", {}).get("status"), "current")
            self.assertNotIn("brief", reused.get("story_handoff", {}))


class StoryProductionJobBoundaryTest(StoryProductionFixture, unittest.TestCase):
    """Run the actual route body, stopping before configuration or service work."""

    def test_invalid_adoption_stops_before_configuration_cloud_or_job_creation(self):
        adopted = self.adopt(self.create())
        path = Path(__file__).resolve().parents[1] / "backend" / "app" / "main.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        route = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "create_job")
        route.decorator_list = []
        module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                                 route], type_ignores=[])
        configuration = Mock(side_effect=AssertionError("Source failed after configuration boundary"))
        cloud = Mock(side_effect=AssertionError("Source failed after cloud boundary"))
        store = Mock()
        namespace = {"HTTPException": HTTPException, "require_user": lambda request: {"id": 1},
            "MAX_SCRIPT_CHARACTERS": 12000, "normalize_project_name": lambda value: value or "新作品",
            "resolve_story_source": stories.resolve_story_source, "_required_job_config_error": configuration,
            "cloud_client_for": cloud, "store": store}
        exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
        source = {**adopted["source"], "adoption_id": "f" * 32}
        # Validate the public source schema as well as invoking the real route.
        stories.StorySource.model_validate(source)
        data = {"script": STORY, "story_source": source, "dynamic_video": True,
                "tts_engine": "qwen", "use_cloud_image_pool": True}
        payload = SimpleNamespace(model_fields_set=set(data), model_dump=lambda: copy.deepcopy(data))
        with self.assertRaises(HTTPException) as caught:
            namespace["create_job"](payload, Request({"type": "http", "headers": []}))
        self.assertEqual(caught.exception.status_code, 409)
        configuration.assert_not_called()
        cloud.assert_not_called()
        self.assertEqual(store.mock_calls, [])


if __name__ == "__main__":
    unittest.main()
