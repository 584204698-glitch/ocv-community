import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from backend.app import comfyui_bridge as bridge
from backend.app import comfyui_guard as guard
from tests.test_comfyui_video_inputs import profile


class GuardSubmissionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.directory = self.root / "attempt_001" / "comfyui_submission"
        self.script = self.root / "fake guard.py"
        self.script.write_text("# Test-only guard; never executed.\n", encoding="utf-8")
        self.environment = patch.dict(os.environ, {guard.ENV_NAME: str(self.script)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.graph = {"1": {"class_type": "Example", "inputs": {"text": "hello"}}}
        self.connection = dict(bridge.DEFAULT_CONNECTION)
        self.process = Mock(pid=987654321)
        self.process.poll.return_value = None
        guard._PROCESSES.clear()
        self.addCleanup(guard._PROCESSES.clear)

    def spawn(self, argv, **kwargs):
        response = Path(argv[argv.index("--response-file") + 1])
        response.write_text(json.dumps({"prompt_id": "remote-123", "number": 1}), encoding="utf-8")
        return self.process

    def submit(self, **kwargs):
        return guard.submit_prompt(self.connection, self.graph, "client-original", self.directory, **kwargs)

    def receipt(self):
        return json.loads((self.directory / "receipt.json").read_text(encoding="utf-8"))

    def test_disabled_guard_retains_http_payload_and_callback(self):
        callback = Mock()
        with patch.dict(os.environ, {guard.ENV_NAME: ""}), \
                patch.object(guard.requests, "post", return_value=Mock(ok=True, json=lambda: {"prompt_id": "direct"})) as post, \
                patch.object(guard.subprocess, "Popen") as popen:
            self.assertEqual(self.submit(on_submitted=callback), "direct")
        post.assert_called_once_with("http://127.0.0.1:8188/prompt",
                                     json={"prompt": self.graph, "client_id": "client-original"}, timeout=60)
        callback.assert_called_once_with("direct")
        popen.assert_not_called()
        self.assertFalse(self.directory.exists())

    def test_wrong_target_and_invalid_script_fail_without_http_fallback(self):
        for base, path, script in [
            ("http://other:8188", "/prompt", str(self.script)),
            (guard.TARGET, "/proxy/prompt", str(self.script)),
            (guard.TARGET, "/prompt", "relative.py"),
            (guard.TARGET, "/prompt", str(self.root / "missing.py")),
        ]:
            with self.subTest(base=base, path=path, script=script), \
                    patch.dict(os.environ, {guard.ENV_NAME: script}), \
                    patch.object(guard.requests, "post") as post, patch.object(guard.subprocess, "Popen") as popen:
                self.connection.update(base_url=base, prompt_path=path)
                with self.assertRaises(guard.GuardSubmissionError):
                    self.submit()
                post.assert_not_called()
                popen.assert_not_called()

    def test_detached_exact_argv_and_durable_private_artifacts(self):
        callback = Mock()
        with patch.object(guard.subprocess, "Popen", side_effect=self.spawn) as popen, \
                patch.object(guard.requests, "post") as post:
            self.assertEqual(self.submit(on_submitted=callback), "remote-123")
        receipt = self.receipt()
        argv = popen.call_args.args[0]
        self.assertEqual(argv[:5], [sys.executable, str(self.script.resolve()), "submit", "--owner", "ocv"])
        self.assertEqual(argv[argv.index("--task-id") + 1], receipt["task_id"])
        self.assertEqual(argv[-4:], ["--timeout", "86400", "--poll", "2"])
        options = popen.call_args.kwargs
        self.assertIs(options["shell"], False)
        self.assertEqual(options["stdin"], subprocess.DEVNULL)
        if os.name == "nt":
            self.assertTrue(options["creationflags"] & subprocess.DETACHED_PROCESS)
        else:
            self.assertTrue(options["start_new_session"])
        self.assertEqual(receipt["prompt_id"], "remote-123")
        self.assertTrue(receipt["delivered"])
        workflow = json.loads((self.directory / "workflow.json").read_text(encoding="utf-8"))
        self.assertEqual(workflow, {"prompt": self.graph, "client_id": "client-original"})
        for name in ("receipt.json", "workflow.json", "response.json", "guard.log"):
            self.assertTrue((self.directory / name).is_file())
            if os.name != "nt":
                self.assertEqual((self.directory / name).stat().st_mode & 0o777, 0o600)
        callback.assert_called_once_with("remote-123")
        self.process.terminate.assert_not_called()
        self.process.kill.assert_not_called()
        self.process.wait.assert_not_called()
        post.assert_not_called()

    def test_partial_response_waits_for_same_live_submission(self):
        counter = []

        def sleep(_):
            counter.append(1)
            content = '{"prompt_id":' if len(counter) == 1 else '{"prompt_id":"remote-123"}'
            (self.directory / "response.json").write_text(content, encoding="utf-8")

        with patch.object(guard.subprocess, "Popen", return_value=self.process) as popen, \
                patch.object(guard.time, "sleep", side_effect=sleep), patch.object(guard.requests, "post") as post:
            self.assertEqual(self.submit(), "remote-123")
        self.assertEqual(len(counter), 2)
        popen.assert_called_once()
        post.assert_not_called()
        self.process.terminate.assert_not_called()

    def test_recovery_does_not_require_current_script_or_profile_inputs(self):
        with patch.object(guard.subprocess, "Popen", side_effect=self.spawn):
            self.submit()
        self.script.unlink()
        guard._PROCESSES.clear()
        with patch.dict(os.environ, {guard.ENV_NAME: ""}), patch.object(guard.subprocess, "Popen") as popen:
            self.assertEqual(guard.get_guard_prompt_id(self.directory), "remote-123")
            callback = Mock()
            self.assertEqual(guard.recover_guard_submission(self.connection, self.directory, on_submitted=callback), "remote-123")
        callback.assert_called_once_with("remote-123")
        popen.assert_not_called()

    def test_restart_waits_using_original_process_identity(self):
        with patch.object(guard.subprocess, "Popen", side_effect=self.spawn), \
                patch.object(guard, "_process_identity", return_value="original-process"):
            self.submit()
        (self.directory / "response.json").write_text("", encoding="utf-8")
        receipt = self.receipt()
        receipt.pop("prompt_id")
        receipt.update(state="running", delivered=False)
        guard._write_json(self.directory / "receipt.json", receipt)
        guard._PROCESSES.clear()
        with patch.object(guard, "_process_identity", return_value="original-process"), \
                patch.object(guard.time, "sleep", side_effect=lambda _: (self.directory / "response.json").write_text('{"prompt_id":"remote-123"}', encoding="utf-8")), \
                patch.object(guard.subprocess, "Popen") as popen:
            self.assertEqual(guard.recover_guard_submission(self.connection, self.directory), "remote-123")
        popen.assert_not_called()

    def test_busy_and_admission_refusals_remain_explicit(self):
        for code in ("GPU_LOCK_BUSY", "COMFYUI_NOT_IDLE", "OTHER_GPU_PROCESS", "H3_RUNTIME_CONTRACT_ERROR", "PROMPT_REJECTED"):
            directory = self.root / code / "comfyui_submission"

            def reject(argv, **kwargs):
                kwargs["stdout"].write(json.dumps({"ok": False, "code": code, "http_status": 400,
                                                  "message": "fixture refusal"}, indent=2).encode())
                self.process.poll.return_value = 3
                return self.process

            with self.subTest(code=code), patch.object(guard.subprocess, "Popen", side_effect=reject) as popen, \
                    patch.object(guard.requests, "post") as post:
                with self.assertRaisesRegex(guard.GuardSubmissionError, code):
                    guard.submit_prompt(self.connection, self.graph, "c", directory)
                with self.assertRaisesRegex(guard.GuardSubmissionError, code):
                    guard.recover_guard_submission(self.connection, directory)
                self.assertEqual(guard.get_guard_prompt_id(directory, require_resolved=True), "")
                popen.assert_called_once()
                post.assert_not_called()

    def test_transport_failure_or_missing_id_is_uncertain_and_never_retried(self):
        for code in ("SUBMIT_TRANSPORT_ERROR", "MISSING_PROMPT_ID", "UNKNOWN_EXIT"):
            directory = self.root / code / "comfyui_submission"

            def reject(argv, **kwargs):
                kwargs["stdout"].write(json.dumps({"ok": False, "code": code}).encode())
                self.process.poll.return_value = 6
                return self.process

            with self.subTest(code=code), patch.object(guard.subprocess, "Popen", side_effect=reject) as popen, \
                    patch.object(guard.requests, "post") as post:
                for _ in range(2):
                    with self.assertRaisesRegex(guard.GuardSubmissionError, "阻止重复提交"):
                        guard.submit_prompt(self.connection, self.graph, "c", directory)
                popen.assert_called_once()
                post.assert_not_called()

    def test_spawn_crash_window_and_pending_receipt_block_new_attempt(self):
        with patch.object(guard.subprocess, "Popen", side_effect=self.spawn):
            self.submit()
        receipt = self.receipt()
        receipt.pop("prompt_id")
        receipt.pop("pid")
        receipt.update(state="starting", delivered=False)
        guard._write_json(self.directory / "receipt.json", receipt)
        (self.directory / "response.json").write_text("", encoding="utf-8")
        guard._PROCESSES.clear()
        self.assertEqual(guard.get_guard_prompt_id(self.directory), "")
        with self.assertRaisesRegex(guard.GuardSubmissionError, "等待任务 ID"):
            guard.get_guard_prompt_id(self.directory, require_resolved=True)
        next_attempt = self.root / "attempt_002" / "comfyui_submission"
        with patch.dict(os.environ, {guard.ENV_NAME: ""}), patch.object(guard.requests, "post") as post:
            with self.assertRaisesRegex(guard.GuardSubmissionError, "attempt_001"):
                guard.submit_prompt(self.connection, self.graph, "new", next_attempt)
            post.assert_not_called()

    def test_callback_failure_keeps_id_recoverable_without_respawn(self):
        with patch.object(guard.subprocess, "Popen", side_effect=self.spawn) as popen:
            with self.assertRaisesRegex(RuntimeError, "local persistence"):
                self.submit(on_submitted=Mock(side_effect=RuntimeError("local persistence")))
            self.assertEqual(guard.get_guard_prompt_id(self.directory), "remote-123")
            self.assertFalse(self.receipt()["delivered"])
            callback = Mock()
            self.assertEqual(self.submit(on_submitted=callback), "remote-123")
            popen.assert_called_once()
        callback.assert_called_once_with("remote-123")

    def test_concurrent_submitters_share_one_guard(self):
        with patch.object(guard.subprocess, "Popen", side_effect=self.spawn) as popen:
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                results = list(executor.map(lambda _: self.submit(), range(8)))
        self.assertEqual(results, ["remote-123"] * 8)
        popen.assert_called_once()

    def test_malformed_conflicting_and_changed_workflow_receipts_fail_closed(self):
        with patch.object(guard.subprocess, "Popen", side_effect=self.spawn):
            self.submit()
        with patch.object(guard.subprocess, "Popen") as popen:
            self.graph["1"]["inputs"]["text"] = "changed"
            with self.assertRaisesRegex(guard.GuardSubmissionError, "工作流"):
                self.submit()
            (self.directory / "response.json").write_text('{"prompt_id":"foreign"}', encoding="utf-8")
            with self.assertRaisesRegex(guard.GuardSubmissionError, "冲突"):
                guard.get_guard_prompt_id(self.directory)
            (self.directory / "receipt.json").write_text('{"unfinished":', encoding="utf-8")
            with self.assertRaisesRegex(guard.GuardSubmissionError, "损坏"):
                guard.get_guard_prompt_id(self.directory)
            popen.assert_not_called()

    def test_bridge_recovery_skips_upload_and_deleted_profile(self):
        with patch.object(guard.subprocess, "Popen", side_effect=self.spawn):
            self.submit()
        self.process.poll.return_value = 0
        history = {"remote-123": {"status": {"completed": True}, "outputs": {"20": {"videos": [{"filename": "result.mp4"}]}}}}
        output = self.directory.parent / "clip.mp4"
        callback = Mock()
        with patch.object(bridge, "_connection", return_value=self.connection), \
                patch.object(bridge, "video_profile", side_effect=AssertionError("deleted profile must not be loaded")), \
                patch.object(bridge, "_upload_reference") as upload, \
                patch.object(bridge, "_history_payload", return_value=history), \
                patch.object(bridge.requests, "get", return_value=Mock(content=b"video")), \
                patch.object(guard.subprocess, "Popen") as popen:
            bridge.run_video_profile(7, "deleted", prompt="p", image_path=self.root / "missing.png",
                                     duration=5, ratio="16:9", output_path=output,
                                     existing_output_node_id="20", on_submitted=callback)
        self.assertEqual(output.read_bytes(), b"video")
        callback.assert_called_once_with("remote-123")
        upload.assert_not_called()
        popen.assert_not_called()

    def _download_existing_video(self, **kwargs):
        history = {"remote-123": {"status": {"completed": True},
                   "outputs": {"20": {"videos": [{"filename": "result.mp4"}]}}}}
        output = self.directory.parent / "clip.mp4"
        with patch.object(bridge, "_connection", return_value=self.connection), \
                patch.object(bridge, "video_profile", side_effect=AssertionError("must recover original request")), \
                patch.object(bridge, "_upload_reference", side_effect=AssertionError("must not upload again")), \
                patch.object(bridge, "_history_payload", return_value=history), \
                patch.object(bridge.requests, "get", return_value=Mock(content=b"video")), \
                patch.object(bridge.requests, "post") as post, \
                patch.object(guard.subprocess, "Popen") as popen:
            try:
                bridge.run_video_profile(7, "deleted", prompt="p", image_path=self.root / "missing.png",
                                         duration=5, ratio="16:9", output_path=output,
                                         existing_prompt_id="remote-123", existing_output_node_id="20", **kwargs)
            finally:
                post.assert_not_called()
                popen.assert_not_called()
        return output

    def test_download_waits_for_its_own_guard_exit_before_returning_to_queue(self):
        source = self.root / "core.png"
        source.write_bytes(b"image")
        output = self.directory.parent / "clip.mp4"
        history = {"remote-123": {"status": {"completed": True},
                   "outputs": {"20": {"videos": [{"filename": "result.mp4"}]}}}}
        events, messages = [], []
        foreign_guard = Mock()
        guard._PROCESSES["foreign-task"] = foreign_guard

        def cleanup_tick(_seconds):
            self.assertEqual(output.read_bytes(), b"video")
            self.assertEqual(self.receipt()["prompt_id"], "remote-123")
            self.assertTrue(self.receipt()["delivered"])
            events.append("guard-still-finishing")
            if len(events) == 2:
                self.process.poll.return_value = 0

        with patch.object(bridge, "_connection", return_value=self.connection), \
                patch.object(bridge, "video_profile", return_value=profile()), \
                patch.object(bridge, "_upload_reference", return_value="core.png"), \
                patch.object(bridge, "_history_payload", return_value=history), \
                patch.object(bridge.requests, "get", return_value=Mock(content=b"video")), \
                patch.object(bridge.requests, "post") as post, \
                patch.object(guard.subprocess, "Popen", side_effect=self.spawn) as popen, \
                patch.object(guard.time, "sleep", side_effect=cleanup_tick):
            bridge.run_video_profile(7, "preset", prompt="p", image_path=source, duration=5,
                                     ratio="16:9", output_path=output, progress=messages.append)
            events.append("queue-can-advance")
        self.assertEqual(events, ["guard-still-finishing", "guard-still-finishing", "queue-can-advance"])
        self.assertEqual(sum("等待本次 guard 收尾" in item for item in messages), 1)
        popen.assert_called_once()
        post.assert_not_called()
        foreign_guard.poll.assert_not_called()
        self.process.terminate.assert_not_called()
        self.process.kill.assert_not_called()

    def test_stop_during_guard_cleanup_keeps_download_and_resumes_same_request(self):
        with patch.object(guard.subprocess, "Popen", side_effect=self.spawn):
            self.submit()
        before = self.receipt()
        stopped = False

        def stop_after_wait(_seconds):
            nonlocal stopped
            self.assertEqual((self.directory.parent / "clip.mp4").read_bytes(), b"video")
            stopped = True

        with patch.object(guard.time, "sleep", side_effect=stop_after_wait):
            with self.assertRaisesRegex(bridge.ComfyUIStopped, "停止本地等待 guard 收尾"):
                self._download_existing_video(should_stop=lambda: stopped)
        self.assertEqual(self.receipt(), before)
        self.assertEqual(guard.get_guard_prompt_id(self.directory), "remote-123")
        self.process.terminate.assert_not_called()
        self.process.kill.assert_not_called()
        self.process.poll.return_value = 0
        with patch.object(guard.time, "sleep") as sleep:
            self.assertEqual(self._download_existing_video().read_bytes(), b"video")
        sleep.assert_not_called()

    def test_direct_existing_result_does_not_wait_for_unrelated_guard_configuration(self):
        with patch.object(guard, "_alive") as alive, patch.object(guard.time, "sleep") as sleep:
            self.assertEqual(self._download_existing_video().read_bytes(), b"video")
        alive.assert_not_called()
        sleep.assert_not_called()
        self.assertFalse((self.directory / "receipt.json").exists())

    def test_recovered_receipt_waits_on_process_identity_without_script_or_handle(self):
        saved_identity = "00000000-0000-0000-0000-000000000001:100"
        with patch.object(guard.subprocess, "Popen", side_effect=self.spawn), \
                patch.object(guard, "_process_identity", return_value=saved_identity):
            self.submit()
        original = self.receipt()
        guard._PROCESSES.clear()
        self.script.unlink()
        with patch.dict(os.environ, {guard.ENV_NAME: ""}), \
                patch.object(guard, "_process_identity", side_effect=[saved_identity, saved_identity, ""]) as identity, \
                patch.object(guard.time, "sleep") as sleep:
            self.assertEqual(self._download_existing_video().read_bytes(), b"video")
        self.assertEqual(identity.call_count, 3)
        self.assertTrue(all(call.kwargs == {"require_known": True} for call in identity.call_args_list))
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(self.receipt(), original)

    def test_missing_recovered_process_identity_is_resumable_not_success(self):
        with patch.object(guard.subprocess, "Popen", side_effect=self.spawn):
            self.submit()
        receipt = self.receipt()
        guard._PROCESSES.clear()
        for identity in ("", 42, "malformed"):
            receipt["process_identity"] = identity
            guard._write_json(self.directory / "receipt.json", receipt)
            with self.subTest(identity=identity), self.assertRaisesRegex(bridge.ComfyUIStopped, "进程身份缺失"):
                self._download_existing_video()
        self.assertEqual((self.directory.parent / "clip.mp4").read_bytes(), b"video")
        self.assertEqual(self.receipt(), receipt)

    def test_reused_pid_does_not_wait_on_or_stop_a_different_process(self):
        with patch.object(guard.subprocess, "Popen", side_effect=self.spawn), \
                patch.object(guard, "_process_identity", return_value="00000000-0000-0000-0000-000000000001:100"):
            self.submit()
        guard._PROCESSES.clear()
        with patch.object(guard, "_process_identity", return_value="00000000-0000-0000-0000-000000000001:101"), \
                patch.object(guard.time, "sleep") as sleep:
            self.assertEqual(self._download_existing_video().read_bytes(), b"video")
        sleep.assert_not_called()
        self.process.terminate.assert_not_called()
        self.process.kill.assert_not_called()

    def test_release_wait_rejects_mismatched_result_without_mutating_receipt(self):
        with patch.object(guard.subprocess, "Popen", side_effect=self.spawn):
            self.submit()
        before = self.receipt()
        with patch.object(guard, "_alive") as alive:
            with self.assertRaisesRegex(guard.GuardWaitingStopped, "收尾身份"):
                guard.wait_for_guard_release(self.directory, "other-prompt")
        self.assertEqual(self.receipt(), before)
        alive.assert_not_called()

    def test_unreadable_response_after_download_pauses_without_losing_identity(self):
        with patch.object(guard.subprocess, "Popen", side_effect=self.spawn):
            self.submit()
        before = self.receipt()
        output = self.directory.parent / "clip.mp4"
        original_read = Path.read_text

        def read(path, *args, **kwargs):
            if path == self.directory.resolve() / "response.json" and output.is_file():
                raise PermissionError("response temporarily unreadable")
            return original_read(path, *args, **kwargs)

        with patch.object(Path, "read_text", new=read):
            with self.assertRaisesRegex(bridge.ComfyUIStopped, "收尾身份"):
                self._download_existing_video()
        self.assertEqual(output.read_bytes(), b"video")
        self.assertEqual(self.receipt(), before)
        self.process.terminate.assert_not_called()
        self.process.kill.assert_not_called()

    def test_linux_unreadable_process_or_boot_identity_never_proves_exit(self):
        stat = "123 (guard) S " + " ".join(["0"] * 18 + ["12345"])
        for reads in ([PermissionError("denied")], [stat, FileNotFoundError("boot identity unavailable")]):
            with self.subTest(reads=reads), patch.object(guard.sys, "platform", "linux"), \
                    patch.object(Path, "read_text", side_effect=reads):
                with self.assertRaises(guard.GuardWaitingStopped):
                    guard._process_identity(123, require_known=True)
        with patch.object(guard.sys, "platform", "linux"), \
                patch.object(Path, "read_text", side_effect=FileNotFoundError("process exited")):
            self.assertEqual(guard._process_identity(123, require_known=True), "")

    def test_guard_stop_delegates_scoped_cancellation_without_global_interrupt(self):
        with patch.object(guard.subprocess, "Popen", side_effect=self.spawn):
            self.submit()
        with patch.object(bridge, "_connection", return_value=self.connection), \
                patch('backend.app.comfyui_cancel.cancel',return_value=dict(status='cancelled',message='本镜任务已取消')) as cancel, \
                patch.object(bridge.requests, "post") as post:
            with self.assertRaisesRegex(bridge.ComfyUIStopped, "本镜任务已取消") as stopped:
                bridge.run_video_profile(7, "deleted", prompt="p", image_path=self.root / "missing.png",
                                         duration=5, ratio="16:9", output_path=self.directory.parent / "clip.mp4",
                                         existing_prompt_id="remote-123", should_stop=lambda: True)
        self.assertTrue(stopped.exception.terminal)
        cancel.assert_called_once()
        post.assert_not_called()
        self.process.terminate.assert_not_called()
        self.process.kill.assert_not_called()

    def test_existing_bridge_id_finishes_delivery_and_checks_original_target(self):
        with patch.object(guard.subprocess, "Popen", side_effect=self.spawn):
            with self.assertRaises(RuntimeError):
                self.submit(on_submitted=Mock(side_effect=RuntimeError("callback interrupted")))
        self.assertFalse(self.receipt()["delivered"])
        with patch.object(bridge, "_connection", return_value={**self.connection, "base_url": "http://other:8188"}), \
                patch.object(bridge, "_history_payload") as history, \
                patch.object(bridge.requests, "post") as post:
            with self.assertRaisesRegex(guard.GuardSubmissionError, "只允许"):
                bridge.run_video_profile(7, "deleted", prompt="p", image_path=self.root / "missing.png",
                                         duration=5, ratio="16:9", output_path=self.directory.parent / "clip.mp4",
                                         existing_prompt_id="remote-123")
            history.assert_not_called()
            post.assert_not_called()
        callback = Mock()
        with patch.object(bridge, "_connection", return_value=self.connection), \
                patch('backend.app.comfyui_cancel.cancel',return_value=dict(status='cancelled',message='本镜任务已取消')):
            with self.assertRaises(bridge.ComfyUIStopped):
                bridge.run_video_profile(7, "deleted", prompt="p", image_path=self.root / "missing.png",
                                         duration=5, ratio="16:9", output_path=self.directory.parent / "clip.mp4",
                                         existing_prompt_id="remote-123", should_stop=lambda: True,
                                         on_submitted=callback)
        self.assertTrue(self.receipt()["delivered"])
        callback.assert_called_once_with("remote-123")

    def test_existing_bridge_id_conflict_never_calls_persistence_callback(self):
        with patch.object(guard.subprocess, "Popen", side_effect=self.spawn):
            self.submit()
        callback = Mock()
        with patch.object(bridge, "_connection", return_value=self.connection), \
                patch.object(bridge, "_history_payload") as history:
            with self.assertRaisesRegex(guard.GuardSubmissionError, "冲突"):
                bridge.run_video_profile(7, "deleted", prompt="p", image_path=self.root / "missing.png",
                                         duration=5, ratio="16:9", output_path=self.directory.parent / "clip.mp4",
                                         existing_prompt_id="foreign", on_submitted=callback)
        callback.assert_not_called()
        history.assert_not_called()

    def test_guard_rejects_public_global_interrupt_api(self):
        with patch.object(bridge, "require_user", return_value={"id": 7}), \
                patch.object(bridge.requests, "post") as post:
            with self.assertRaises(bridge.HTTPException) as error:
                bridge.interrupt(Mock())
        self.assertEqual(error.exception.status_code, 409)
        post.assert_not_called()

    def test_pending_identity_does_not_allow_attempt_freeze(self):
        with patch.object(guard.subprocess, "Popen", side_effect=self.spawn):
            self.submit()
        receipt = self.receipt()
        receipt.pop("prompt_id")
        receipt.update(state="running", delivered=False)
        guard._write_json(self.directory / "receipt.json", receipt)
        (self.directory / "response.json").write_text('{"prompt_id":', encoding="utf-8")
        self.assertEqual(guard.get_guard_prompt_id(self.directory), "")
        with self.assertRaisesRegex(guard.GuardSubmissionError, "等待任务 ID"):
            guard.get_guard_prompt_id(self.directory, require_resolved=True)

    def test_pid_reuse_without_id_is_uncertain(self):
        with patch.object(guard.subprocess, "Popen", side_effect=self.spawn), \
                patch.object(guard, "_process_identity", return_value="original-process"):
            self.submit()
        receipt = self.receipt()
        receipt.pop("prompt_id")
        receipt.update(state="running", delivered=False)
        guard._write_json(self.directory / "receipt.json", receipt)
        (self.directory / "response.json").write_text("", encoding="utf-8")
        guard._PROCESSES.clear()
        with patch.object(guard, "_process_identity", return_value="different-process"), \
                patch.object(guard.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(guard.GuardSubmissionError, "阻止重复提交"):
                guard.recover_guard_submission(self.connection, self.directory)
        popen.assert_not_called()

    def test_stop_during_pending_identity_preserves_guard_and_prevents_new_attempt(self):
        with patch.object(guard.subprocess, "Popen", return_value=self.process) as popen, \
                patch('backend.app.comfyui_cancel.cancel',side_effect=ValueError('任务编号尚未确认')):
            with self.assertRaisesRegex(guard.GuardWaitingStopped, "任务编号尚未确认"):
                self.submit(should_stop=lambda: True)
            popen.assert_called_once()
        self.process.terminate.assert_not_called()
        self.process.kill.assert_not_called()
        with self.assertRaises(guard.GuardSubmissionError):
            guard.get_guard_prompt_id(self.directory, require_resolved=True)
        with patch.object(bridge, "_connection", return_value=self.connection), \
                patch('backend.app.comfyui_cancel.cancel',side_effect=ValueError('任务编号尚未确认')), \
                patch.object(bridge, "_upload_reference") as upload:
            with self.assertRaisesRegex(bridge.ComfyUIStopped, "任务编号尚未确认"):
                bridge.run_video_profile(7, "deleted", prompt="p", image_path=self.root / "missing.png",
                                         duration=5, ratio="16:9", output_path=self.directory.parent / "clip.mp4",
                                         should_stop=lambda: True)
        upload.assert_not_called()
        (self.directory / "response.json").write_text('{"prompt_id":"remote-123"}', encoding="utf-8")
        with patch.object(guard.subprocess, "Popen") as popen:
            self.assertEqual(guard.recover_guard_submission(self.connection, self.directory), "remote-123")
        popen.assert_not_called()

    def test_nested_error_text_is_not_an_explicit_guard_refusal(self):
        def uncertain(argv, **kwargs):
            kwargs["stdout"].write(json.dumps({"ok": False, "code": "SUBMIT_TRANSPORT_ERROR",
                                              "detail": {"ok": False, "code": "GPU_LOCK_BUSY"}}, indent=2).encode())
            self.process.poll.return_value = 6
            return self.process

        with patch.object(guard.subprocess, "Popen", side_effect=uncertain):
            with self.assertRaisesRegex(guard.GuardSubmissionError, "阻止重复提交"):
                self.submit()
        self.assertEqual(self.receipt()["state"], "uncertain")

    def test_http_server_error_cannot_prove_prompt_was_rejected(self):
        for status in (None, 408, 500, 502, 504):
            directory = self.root / str(status) / "comfyui_submission"

            def uncertain(argv, **kwargs):
                kwargs["stdout"].write(json.dumps({"ok": False, "code": "PROMPT_REJECTED",
                                                  "http_status": status}).encode())
                self.process.poll.return_value = 6
                return self.process

            with self.subTest(status=status), patch.object(guard.subprocess, "Popen", side_effect=uncertain) as popen:
                with self.assertRaisesRegex(guard.GuardSubmissionError, "阻止重复提交"):
                    guard.submit_prompt(self.connection, self.graph, "client", directory)
                with self.assertRaises(guard.GuardSubmissionError):
                    guard.get_guard_prompt_id(directory, require_resolved=True)
                popen.assert_called_once()

    def test_partial_or_truncated_outer_log_never_promotes_nested_refusal(self):
        self.directory.mkdir(parents=True)
        partial = '{"ok":false,"code":"MISSING_PROMPT_ID","response":{"ok":false,"code":"GPU_LOCK_BUSY"}'
        for log in (partial, "x" * (1024 * 1024) + '\n{"ok":false,"code":"GPU_LOCK_BUSY"}'):
            with self.subTest(length=len(log)):
                (self.directory / "guard.log").write_text(log, encoding="utf-8")
                self.assertIsNone(guard._refusal(self.directory))

    def test_workbench_uses_shared_submission_callback(self):
        preset = profile()
        job = self.root / "job"
        job.mkdir()
        history = {"remote-123": {"status": {"completed": True}, "outputs": {"20": {"videos": [{"filename": "result.mp4"}]}}}}
        source = self.root / "image.png"
        source.write_bytes(b"image")
        with patch.object(bridge, "_job_path", return_value=job), \
                patch.object(bridge, "_connection", return_value=self.connection), \
                patch.object(bridge, "_upload_reference", return_value="uploaded.png"), \
                patch.object(bridge, "_history_payload", return_value=history), \
                patch.object(bridge.requests, "get", return_value=Mock(content=b"video")), \
                patch.object(guard.subprocess, "Popen", side_effect=self.spawn) as popen, \
                patch.object(guard.requests, "post") as post:
            bridge._execute(7, "job", preset, source, None,
                            {"prompt": "p", "duration": 5, "width": 1280, "height": 720, "seed": 1})
        record = json.loads((job / "record.json").read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["remote_prompt_id"], "remote-123")
        self.assertEqual((job / "result.mp4").read_bytes(), b"video")
        popen.assert_called_once()
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
