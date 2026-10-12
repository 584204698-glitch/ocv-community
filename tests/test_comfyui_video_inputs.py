import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from backend.app import comfyui_bridge as bridge


def binding(node, field="image"):
    return {"node_id": str(node), "input_name": field}


def profile(mode="single_first", capacity=0, optional_last=False):
    graph = {"1": {"class_type": "LoadImage", "inputs": {"image": "old-core.png"}}}
    inputs = {"prompt": "old prompt", "length": 90, "width": 1280, "height": 720}
    mappings = {"prompt": binding(10, "prompt"), "image": binding(1), "output_node_id": "20"}
    if mode == "composition_reference":
        inputs["ref_images.ref_image_0"] = ["1", 0]
        mappings["reference_images"] = []
        for index in range(capacity):
            node = str(index + 2)
            graph[node] = {"class_type": "LoadImage", "inputs": {"image": f"old-{node}.png"}}
            inputs[f"ref_images.ref_image_{index + 1}"] = [node, 0]
            mappings["reference_images"].append(binding(node))
    else:
        inputs["first_frame"] = ["1", 0]
        if mode == "first_last" or optional_last:
            graph["2"] = {"class_type": "LoadImage", "inputs": {"image": "old-last.png"}}
            inputs["last_frame"] = ["2", 0]
            mappings["last_image"] = binding(2)
    graph["10"] = {"class_type": "MiniMaxH3ReferenceToVideo" if mode == "composition_reference" else "MiniMaxH3ImageToVideo", "inputs": inputs}
    graph["20"] = {"class_type": "VHS_VideoCombine", "inputs": {"images": ["10", 0]}}
    return {"id": "preset", "name": "Test", "kind": "video", "video_input_mode": mode,
            "workflow": graph, "mappings": mappings}


def audio_profile(*, direct=False):
    preset = profile()
    preset["workflow"]["4"] = {"class_type": "LoadAudio", "inputs": {"audio": "old-voice.wav"}}
    preset["workflow"]["10"]["inputs"]["audio"] = ["4", 0]
    preset["mappings"]["audio"] = binding(10 if direct else 4, "audio")
    return preset


class VideoInputValidationTests(unittest.TestCase):
    def test_h3_four_reference_preset_expands_without_mutating_saved_graph(self):
        preset = profile("composition_reference", 4)
        original = copy.deepcopy(preset)
        with patch.object(bridge, "_profiles", return_value=[preset]):
            expanded = bridge.video_profile(7, "preset")
        self.assertEqual(preset, original)
        self.assertEqual(len(expanded["mappings"]["reference_images"]), 8)
        for count in (0, 2, 4, 8):
            with self.subTest(count=count):
                info = bridge.validate_video_inputs(expanded, input_mode="composition_reference", reference_count=count, has_last=False)
                self.assertEqual(info["image_count"], 1 + count)
                graph = bridge._patch_graph(expanded["workflow"], bridge.WorkflowMappings.model_validate(expanded["mappings"]),
                                            prompt="new", duration=5, width=1280, height=720, seed=1,
                                            image_name="core.png", reference_image_names=[f"ref-{index}.png" for index in range(count)])
                self.assertEqual(len([key for key in graph["10"]["inputs"] if key.startswith("ref_images")]), 1 + count)
                self.assertEqual(len([node for node in graph.values() if node["class_type"] == "LoadImage"]), 1 + count)
        self.assertEqual(preset, original)

    def test_h3_capacity_expansion_rejects_unverified_wiring(self):
        preset = profile("composition_reference", 4)
        preset["mappings"]["reference_images"].reverse()
        with self.assertRaises(ValueError):
            bridge.expand_h3_reference_capacity(preset)
        generic = profile("composition_reference", 4)
        generic["workflow"]["10"]["class_type"] = "CustomVideoGenerator"
        self.assertEqual(bridge.expand_h3_reference_capacity(generic), generic)

    def test_legacy_defaults_and_explicit_modes(self):
        legacy = profile()
        legacy.pop("video_input_mode")
        parsed = bridge.ProfileRequest.model_validate(legacy)
        self.assertEqual(parsed.video_input_mode, "single_first")
        self.assertEqual(parsed.mappings.reference_images, [])
        for mode, count, last in [("single_first", 0, False), ("first_last", 0, True), ("composition_reference", 2, False)]:
            with self.subTest(mode=mode):
                value = bridge.validate_video_inputs(profile(mode, 2 if count else 0), input_mode=mode, reference_count=count, has_last=last)
                self.assertEqual(value["image_count"], 1 + count + int(last))

    def test_modes_counts_missing_slots_and_capacity_fail(self):
        cases = [
            (profile(), "first_last", 0, True),
            (profile("first_last"), "first_last", 0, False),
            (profile(), "single_first", 1, False),
            (profile("composition_reference", 1), "composition_reference", 2, False),
            (profile("composition_reference", 1), "composition_reference", 1, True),
            (profile(), "single_first", -1, False),
            (profile(), "unknown", 0, False),
        ]
        missing = profile("first_last")
        missing["mappings"].pop("last_image")
        cases.append((missing, "first_last", 0, True))
        for preset, mode, count, last in cases:
            with self.subTest(mode=mode, count=count, last=last):
                with self.assertRaises(ValueError):
                    bridge.validate_video_inputs(preset, input_mode=mode, reference_count=count, has_last=last)

    def test_duplicate_media_slots_rejected(self):
        preset = profile("first_last")
        preset["mappings"]["last_image"] = binding(1)
        with self.assertRaisesRegex(ValueError, "重复映射"):
            bridge.validate_video_inputs(preset, input_mode="first_last", reference_count=0, has_last=True)
        preset = profile()
        preset["mappings"]["audio"] = binding(1)
        with self.assertRaisesRegex(ValueError, "重复映射"):
            bridge.validate_video_inputs(preset, input_mode="single_first", reference_count=0, has_last=False)

    def test_known_h3_type_and_actual_picture_order_checked(self):
        preset = profile("composition_reference", 2)
        preset["mappings"]["reference_images"].reverse()
        with self.assertRaisesRegex(ValueError, "实际送入"):
            bridge.validate_video_inputs(preset, input_mode="composition_reference", reference_count=2, has_last=False)
        preset = profile()
        preset["workflow"]["10"]["class_type"] = "MiniMaxH3ReferenceToVideo"
        with self.assertRaisesRegex(ValueError, "节点类型冲突"):
            bridge.validate_video_inputs(preset, input_mode="single_first", reference_count=0, has_last=False)

    def test_undeclared_old_h3_image_is_rejected(self):
        preset = profile("composition_reference", 2)
        preset["mappings"]["reference_images"].pop()
        with self.assertRaisesRegex(ValueError, "旧图片输入"):
            bridge.validate_video_inputs(preset, input_mode="composition_reference", reference_count=1, has_last=False)

    def test_unused_optional_reference_loaders_and_links_are_removed(self):
        preset = profile("composition_reference", 3)
        original = copy.deepcopy(preset)
        bridge.validate_video_inputs(preset, input_mode="composition_reference", reference_count=1, has_last=False)
        graph = bridge._patch_graph(preset["workflow"], bridge.WorkflowMappings.model_validate(preset["mappings"]),
                                    prompt="new", duration=5, width=1280, height=720, seed=1,
                                    image_name="core.png", reference_image_names=["role.png"])
        self.assertEqual(graph["2"]["inputs"]["image"], "role.png")
        self.assertNotIn("3", graph)
        self.assertNotIn("4", graph)
        self.assertNotIn("ref_images.ref_image_2", graph["10"]["inputs"])
        self.assertEqual(preset, original)

    def test_unused_optional_last_frame_is_removed(self):
        preset = profile(optional_last=True)
        bridge.validate_video_inputs(preset, input_mode="single_first", reference_count=0, has_last=False)
        graph = bridge._patch_graph(preset["workflow"], bridge.WorkflowMappings.model_validate(preset["mappings"]),
                                    prompt="new", duration=5, width=1280, height=720, seed=1, image_name="core.png")
        self.assertNotIn("2", graph)
        self.assertNotIn("last_frame", graph["10"]["inputs"])

    def test_unsafe_optional_removal_is_rejected(self):
        for custom in (False, True):
            preset = profile("composition_reference", 2)
            if custom:
                preset["workflow"]["3"]["class_type"] = "CustomImageLoader"
            else:
                preset["workflow"]["30"] = {"class_type": "ImageScale", "inputs": {"image": ["3", 0]}}
            with self.subTest(custom=custom), self.assertRaisesRegex(ValueError, "安全"):
                bridge.validate_video_inputs(preset, input_mode="composition_reference", reference_count=1, has_last=False)

    def test_saved_profile_retains_mode_and_order(self):
        preset = profile("composition_reference", 2)
        payload = bridge.ProfileRequest.model_validate(preset)
        with tempfile.TemporaryDirectory() as folder, patch.object(bridge, "require_user", return_value={"id": 7}), \
                patch.object(bridge, "_user_root", return_value=Path(folder)), \
                patch.object(bridge, "_profiles", return_value=[]), patch.object(bridge, "_write_json") as save:
            result = bridge.save_profile(payload, Mock())
        self.assertEqual(result["video_input_mode"], "composition_reference")
        self.assertEqual([item["node_id"] for item in result["mappings"]["reference_images"]], ["2", "3"])
        self.assertEqual(save.call_count, 1)


class AudioBindingTests(unittest.TestCase):
    def patched_graph(self, preset, *, audio_name=""):
        return bridge._patch_graph(preset["workflow"], bridge.WorkflowMappings.model_validate(preset["mappings"]),
                                   prompt="new", duration=5, width=1280, height=720, seed=1,
                                   image_name="core.png", audio_name=audio_name)

    def test_unused_h3_audio_socket_keeps_generator_and_output(self):
        preset = audio_profile(direct=True)
        original = copy.deepcopy(preset)
        graph = self.patched_graph(preset)
        self.assertEqual(graph["10"]["class_type"], "MiniMaxH3ImageToVideo")
        self.assertEqual(graph["10"]["inputs"]["first_frame"], ["1", 0])
        self.assertEqual(graph["10"]["inputs"]["prompt"], "new")
        self.assertEqual(graph["20"]["inputs"]["images"], ["10", 0])
        self.assertNotIn("audio", graph["10"]["inputs"])
        self.assertNotIn("4", graph)
        self.assertEqual(preset, original)

    def test_h3_audio_socket_upload_patches_loader_not_socket(self):
        graph = self.patched_graph(audio_profile(direct=True), audio_name="uploaded.wav")
        self.assertEqual(graph["10"]["inputs"]["audio"], ["4", 0])
        self.assertEqual(graph["4"]["inputs"]["audio"], "uploaded.wav")

    def test_traditional_loader_binding_supports_audio_present_and_absent(self):
        for audio_name in ("", "uploaded.wav"):
            with self.subTest(audio_name=audio_name):
                graph = self.patched_graph(audio_profile(), audio_name=audio_name)
                self.assertIn("10", graph)
                if audio_name:
                    self.assertEqual(graph["4"]["inputs"]["audio"], audio_name)
                    self.assertEqual(graph["10"]["inputs"]["audio"], ["4", 0])
                else:
                    self.assertNotIn("4", graph)
                    self.assertNotIn("audio", graph["10"]["inputs"])

    def test_disconnect_socket_preserves_loader_used_elsewhere(self):
        preset = audio_profile(direct=True)
        preset["workflow"]["20"]["inputs"]["audio"] = ["4", 0]
        graph = self.patched_graph(preset)
        self.assertNotIn("audio", graph["10"]["inputs"])
        self.assertIn("4", graph)
        self.assertEqual(graph["20"]["inputs"]["audio"], ["4", 0])

    def test_unsafe_audio_loader_deletion_is_rejected(self):
        for reason in ("required_consumer", "output_node", "unknown_node"):
            preset = audio_profile()
            if reason == "required_consumer":
                preset["workflow"]["30"] = {"class_type": "AudioTransform", "inputs": {"audio": ["4", 0]}}
            elif reason == "output_node":
                preset["mappings"]["output_node_id"] = "4"
            else:
                preset["workflow"]["4"]["class_type"] = "CustomProcessor"
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, "安全断开"):
                self.patched_graph(preset)


class VideoOutputSelectionTests(unittest.TestCase):
    def test_video_lookup_skips_previews_including_vhs_gifs(self):
        for video_key in ("videos", "gifs", "images"):
            history = {"outputs": {
                "preview": {"images": [{"filename": "preview.png"}]},
                "animation": {"gifs": [{"filename": "preview.gif"}]},
                "20": {video_key: [{"filename": "result.MP4"}]},
            }}
            with self.subTest(video_key=video_key):
                self.assertEqual(bridge._find_output_file(history, "", video_only=True)["filename"], "result.MP4")
                self.assertEqual(bridge._find_output_file(history, "")["filename"], "preview.png")
                self.assertIsNone(bridge._find_output_file(history, "preview", video_only=True))

    def test_image_outputs_are_not_video_even_when_mislabeled(self):
        for group in ("videos", "gifs", "images"):
            history = {"outputs": {"20": {group: [{"filename": "preview.png"}]}}}
            with self.subTest(group=group):
                self.assertIsNone(bridge._find_output_file(history, "", video_only=True))
                self.assertEqual(bridge._find_output_file(history, "")["filename"], "preview.png")

    def test_preview_alone_does_not_mark_video_task_completed(self):
        history = {"remote": {"outputs": {"preview": {"images": [{"filename": "preview.png"}]}}}}
        with patch.object(bridge, "_connection", return_value=bridge.DEFAULT_CONNECTION), \
                patch.object(bridge, "_history_payload", return_value=history):
            self.assertEqual(bridge.video_task_state(7, "remote"), "history")


class VideoInputSubmissionTests(unittest.TestCase):
    def submit(self, preset, mode, *, extra=0, last=False, resume=False):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            files = []
            # Deliberately equal basenames: each upload must remain distinct.
            for index in range(1 + extra + int(last)):
                path = root / str(index) / "image.png"
                path.parent.mkdir()
                path.write_bytes(f"picture-{index}".encode())
                files.append(path)
            uploads, prompts = [], []

            def post(url, **kwargs):
                if url.endswith("/upload/image"):
                    name, stream, _ = kwargs["files"]["image"]
                    uploads.append((name, stream.read(), kwargs["data"]))
                    return Mock(ok=True, json=lambda: {"name": name})
                if url.endswith("/prompt"):
                    prompts.append(kwargs["json"]["prompt"])
                    return Mock(ok=True, json=lambda: {"prompt_id": "remote"})
                raise AssertionError(f"Unexpected POST {url}")

            history = {"remote": {"status": {"completed": True}, "outputs": {"20": {"videos": [{"filename": "result.mp4"}]}}}}
            with patch.object(bridge, "video_profile", return_value=preset), \
                    patch.object(bridge, "_connection", return_value=bridge.DEFAULT_CONNECTION), \
                    patch.object(bridge, "_history_payload", return_value=history), \
                    patch.object(bridge.requests, "post", side_effect=post), \
                    patch.object(bridge.requests, "get", return_value=Mock(content=b"video")):
                output = root / "result.mp4"
                bridge.run_video_profile(7, "preset", prompt="final prompt", image_path=files[0],
                                         duration=5, ratio="16:9", output_path=output,
                                         input_mode=mode, last_image_path=files[1] if last else None,
                                         reference_image_paths=files[1:] if extra else [],
                                         existing_prompt_id="remote" if resume else "")
                self.assertEqual(output.read_bytes(), b"video")
            return uploads, prompts

    def test_first_last_uploads_and_submits_both_images(self):
        uploads, prompts = self.submit(profile("first_last"), "first_last", last=True)
        self.assertEqual(len(uploads), 2)
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0]["1"]["inputs"]["image"], uploads[0][0])
        self.assertEqual(prompts[0]["2"]["inputs"]["image"], uploads[1][0])
        self.assertNotEqual(uploads[0][0], uploads[1][0])
        self.assertTrue(all(row[2]["overwrite"] == "false" for row in uploads))

    def test_direct_h3_audio_mapping_without_audio_submits_video_graph(self):
        uploads, prompts = self.submit(audio_profile(direct=True), "single_first")
        self.assertEqual(len(uploads), 1)
        self.assertEqual(prompts[0]["10"]["class_type"], "MiniMaxH3ImageToVideo")
        self.assertNotIn("audio", prompts[0]["10"]["inputs"])
        self.assertEqual(prompts[0]["20"]["inputs"]["images"], ["10", 0])

    def test_unsafe_audio_mapping_fails_before_upload_or_submit(self):
        preset = audio_profile()
        preset["workflow"]["4"]["class_type"] = "CustomProcessor"
        with patch.object(bridge, "video_profile", return_value=preset), \
                patch.object(bridge, "_connection", return_value=bridge.DEFAULT_CONNECTION), \
                patch.object(bridge.requests, "post") as post, patch.object(bridge.requests, "get") as get:
            with self.assertRaisesRegex(ValueError, "安全断开"):
                bridge.run_video_profile(7, "preset", prompt="p", image_path=Path("missing.png"), duration=5,
                                         ratio="16:9", output_path=Path("unused.mp4"))
        post.assert_not_called()
        get.assert_not_called()

    def test_composition_uploads_every_reference_in_declared_order(self):
        uploads, prompts = self.submit(profile("composition_reference", 3), "composition_reference", extra=2)
        self.assertEqual([row[1] for row in uploads], [b"picture-0", b"picture-1", b"picture-2"])
        self.assertEqual(len({row[0] for row in uploads}), 3)
        for index in range(3):
            self.assertEqual(prompts[0][str(index + 1)]["inputs"]["image"], uploads[index][0])
        self.assertNotIn("4", prompts[0])
        self.assertNotIn("ref_images.ref_image_3", prompts[0]["10"]["inputs"])

    def test_invalid_input_is_rejected_before_any_network_request(self):
        with patch.object(bridge, "video_profile", return_value=profile()), \
                patch.object(bridge, "_connection", return_value=bridge.DEFAULT_CONNECTION), \
                patch.object(bridge.requests, "post") as post, patch.object(bridge.requests, "get") as get:
            with self.assertRaises(ValueError):
                bridge.run_video_profile(7, "preset", prompt="p", image_path=Path("missing.png"), duration=5,
                                         ratio="16:9", output_path=Path("unused.mp4"), input_mode="first_last", last_image_path=Path("last.png"))
        post.assert_not_called()
        get.assert_not_called()

    def test_resume_queries_existing_task_without_uploading_or_resubmitting(self):
        uploads, prompts = self.submit(profile("first_last"), "first_last", last=True, resume=True)
        self.assertEqual(uploads, [])
        self.assertEqual(prompts, [])

    def test_resume_does_not_load_deleted_or_broken_profile_or_media(self):
        for current_profile in (ValueError("preset deleted"), {"mappings": {"reference_images": "invalid"}}):
            for frozen_output in ("20", ""):
                history = {"remote": {"outputs": {"20": {"videos": [{"filename": "result.mp4"}]}}}}
                history["remote"]["outputs"] = {"preview": {"images": [{"filename": "wrong.png"}]}, **history["remote"]["outputs"]}
                with self.subTest(current_profile=current_profile, frozen_output=frozen_output), \
                        tempfile.TemporaryDirectory() as folder, \
                        patch.object(bridge, "video_profile", side_effect=current_profile if isinstance(current_profile, Exception) else None,
                                     return_value=current_profile) as load_profile, \
                        patch.object(bridge, "validate_video_inputs") as validate, \
                        patch.object(bridge, "_connection", return_value=bridge.DEFAULT_CONNECTION), \
                        patch.object(bridge, "_history_payload", return_value=history), \
                        patch.object(bridge.requests, "post") as post, \
                        patch.object(bridge.requests, "get", return_value=Mock(content=b"original video")) as get:
                    root = Path(folder)
                    output = root / "result.mp4"
                    bridge.run_video_profile(7, "deleted", prompt="edited", image_path=root / "missing.png",
                                             duration=5, ratio="invalid current ratio", output_path=output,
                                             input_mode="invalid current mode", last_image_path=root / "missing-last.png",
                                             reference_image_paths=[root / "missing-reference.png"],
                                             existing_prompt_id="remote", existing_output_node_id=frozen_output)
                    self.assertEqual(output.read_bytes(), b"original video")
                    self.assertEqual(get.call_args.kwargs["params"]["filename"], "result.mp4")
                    load_profile.assert_not_called()
                    validate.assert_not_called()
                    post.assert_not_called()

    def test_completed_video_without_container_fails_without_downloading_preview(self):
        history = {"remote": {"status": {"completed": True},
                              "outputs": {"preview": {"images": [{"filename": "preview.png"}]}}}}
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(bridge, "_connection", return_value=bridge.DEFAULT_CONNECTION), \
                patch.object(bridge, "_history_payload", return_value=history), \
                patch.object(bridge.requests, "post") as post, patch.object(bridge.requests, "get") as get:
            output = Path(folder) / "clip.mp4"
            with self.assertRaisesRegex(RuntimeError, "没有视频文件"):
                bridge.run_video_profile(7, "missing", prompt="p", image_path=Path("missing.png"), duration=5,
                                         ratio="16:9", output_path=output, existing_prompt_id="remote")
            self.assertFalse(output.exists())
            post.assert_not_called()
            get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
