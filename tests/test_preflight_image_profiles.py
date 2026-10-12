"""Preflight must validate the image configuration the job actually selected."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend.app import image_profiles as profiles
from backend.app import main


class ImageProfilePreflightTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.env = self.root / ".env"
        self.documents = self.root / "image_profiles.json"
        self.env.write_text("", encoding="utf-8")
        self.documents.write_text('{"profiles": []}', encoding="utf-8")

        replacements = (
            (profiles, "ENV_PATH", self.env),
            (profiles, "PROFILE_PATH", self.documents),
            (main, "PROJECT_ROOT", self.root),
            (main, "require_user", Mock(return_value={"id": 1})),
            (main, "user_upload_path", Mock(return_value=self.root / "voice.wav")),
            (main, "_project_config_values", Mock(return_value={})),
            (main, "_probe_language_api", Mock(return_value=("passed", "language configured"))),
            (main, "_ffmpeg_preflight", Mock(return_value=(True, "ffmpeg available"))),
            (main.shutil, "disk_usage", Mock(return_value=SimpleNamespace(free=64 * 1024 ** 3))),
            (main.requests.sessions.Session, "request", Mock(side_effect=AssertionError("unexpected network request"))),
        )
        for target, name, value in replacements:
            handle = patch.object(target, name, value)
            handle.start()
            self.addCleanup(handle.stop)

    def save_profile(self, *, protocol="openai_images", credential_source="profile", configured=True) -> None:
        document = profiles.ImageProfileRequest(
            id="selected-fixture", name="Selected image fixture", protocol=protocol,
            credential_source=credential_source, base_url="https://images.example.test/v1",
            model_id="image-fixture-v2",
        ).model_dump(exclude={"api_key", "api_keys"})
        self.documents.write_text(json.dumps({"profiles": [document]}), encoding="utf-8")
        key = "fake-selected-image-secret" if configured else ""
        values = (
            {"CUSTOM_LLM_API_BASE": document["base_url"], "CUSTOM_LLM_API_KEY": key}
            if credential_source == "language_custom"
            else {"OCV_IMAGE_PROFILE_KEYS_SELECTED_FIXTURE": key}
        )
        self.env.write_text("".join(f"{name}={value}\n" for name, value in values.items()), encoding="utf-8")

    def preflight(self, **changes):
        payload = main.GenerateRequest(**{
            "project_name": "Image preflight fixture", "script": "这是已经确认的测试旁白。",
            "skip_tts": True, "source_audio_id": "voice.wav", "visual_backend": "poster",
            **changes,
        })
        return main.preflight_job(payload, Mock())

    def assert_selected_profile_passes(self) -> None:
        with (
            patch.object(main, "_runninghub_api_keys", return_value=[]) as legacy_keys,
            patch.object(main, "_probe_image_api_pool", wraps=main._probe_image_api_pool) as legacy_probe,
        ):
            result = self.preflight(image_profile_id="selected-fixture", image_resolution="1k")
        image = next(item for item in result["items"] if item["id"] == "image_api")
        self.assertTrue(result["ok"], result)
        self.assertEqual(image["status"], "passed")
        self.assertIn("Selected image fixture", image["message"])
        self.assertIn("1K", image["message"])
        self.assertIn("未进行联网验证", image["message"])
        self.assertNotIn("fake-selected-image-secret", json.dumps(result))
        legacy_keys.assert_not_called()
        legacy_probe.assert_not_called()

    def test_native_profile_without_legacy_credentials_passes(self) -> None:
        self.save_profile()
        self.assert_selected_profile_passes()

    def test_native_profile_can_use_saved_shared_language_credentials(self) -> None:
        self.save_profile(credential_source="language_custom")
        self.assert_selected_profile_passes()

    def test_selected_async_profile_does_not_probe_an_unrelated_legacy_endpoint(self) -> None:
        self.save_profile(protocol="async_task")
        self.assert_selected_profile_passes()

    def test_invalid_selected_profile_never_falls_back_to_legacy_credentials(self) -> None:
        main._project_config_values.return_value = {"RUNNINGHUB_API_KEY": "fake-legacy-secret"}
        for case in ("missing", "no-key", "resolution", "changed-shared-base"):
            with self.subTest(case=case):
                self.save_profile(
                    configured=case != "no-key",
                    credential_source="language_custom" if case == "changed-shared-base" else "profile",
                )
                if case == "changed-shared-base":
                    self.env.write_text(
                        "CUSTOM_LLM_API_BASE=https://other.example.test/v1\n"
                        "CUSTOM_LLM_API_KEY=fake-rotated-secret\n", encoding="utf-8",
                    )
                with (
                    patch.object(main, "_probe_image_api_pool") as legacy_probe,
                    self.assertRaises(main.HTTPException) as raised,
                ):
                    self.preflight(
                        image_profile_id="missing-fixture" if case == "missing" else "selected-fixture",
                        image_resolution="2k" if case == "resolution" else "1k",
                    )
                self.assertEqual(raised.exception.status_code, 400)
                self.assertNotIn("fake-", raised.exception.detail)
                legacy_probe.assert_not_called()

    def test_no_selected_profile_still_reports_missing_legacy_credentials(self) -> None:
        result = self.preflight()
        image = next(item for item in result["items"] if item["id"] == "image_api")
        self.assertFalse(result["ok"])
        self.assertEqual(image["status"], "error")
        self.assertIn("未配置", image["message"])

    def test_no_selected_profile_still_probes_configured_legacy_accounts(self) -> None:
        main._project_config_values.return_value = {"RUNNINGHUB_API_KEY": "fake-legacy-secret"}
        with patch.object(main, "_probe_one_image_key", return_value=("valid", "global")) as probe:
            result = self.preflight()
        image = next(item for item in result["items"] if item["id"] == "image_api")
        self.assertTrue(result["ok"], result)
        self.assertEqual(image["status"], "passed")
        probe.assert_called_once_with("fake-legacy-secret")

    def test_cloud_pool_takes_precedence_over_a_stale_local_profile_selection(self) -> None:
        client = Mock()
        client.session_snapshot.return_value = {"configured": True, "authenticated": True}
        client.account_summary.return_value = {"credits": {"available": 20}}
        with (
            patch.object(main, "cloud_client_for", return_value=client),
            patch.object(main, "_probe_image_api_pool") as legacy_probe,
            patch.object(profiles, "profile_snapshot", wraps=profiles.profile_snapshot) as snapshot,
        ):
            result = self.preflight(use_cloud_image_pool=True, image_profile_id="missing-fixture")
        self.assertTrue(result["ok"], result)
        image = next(item for item in result["items"] if item["id"] == "image_api")
        self.assertEqual(image["label"], "云端号池")
        self.assertEqual(image["status"], "passed")
        client.model_pool_status.assert_called_once_with()
        client.image_pool_status.assert_called_once_with('running')
        main._probe_language_api.assert_not_called()
        legacy_probe.assert_not_called()
        snapshot.assert_not_called()

    def test_cloud_pool_still_requires_authentication(self) -> None:
        client = Mock()
        client.session_snapshot.return_value = {"configured": True, "authenticated": False}
        with patch.object(main, "cloud_client_for", return_value=client):
            result = self.preflight(use_cloud_image_pool=True)
        image = next(item for item in result["items"] if item["id"] == "image_api")
        self.assertFalse(result["ok"])
        self.assertEqual(image["status"], "error")
        self.assertIn("登录", image["message"])
        client.image_pool_status.assert_not_called()

    def test_workflows_without_online_images_do_not_add_an_image_probe(self) -> None:
        for changes in ({"visual_backend": "slides"}, {"module1_only": True}, {"subtitle_only": True}):
            with self.subTest(changes=changes), patch.object(main, "_probe_image_api_pool") as probe:
                result = self.preflight(**changes)
            self.assertTrue(result["ok"], result)
            self.assertNotIn("image_api", {item["id"] for item in result["items"]})
            probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
