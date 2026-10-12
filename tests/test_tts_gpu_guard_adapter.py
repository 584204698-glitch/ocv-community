"""Guard integration CPU tests. No machine lock, GPU or model is used."""
from contextlib import redirect_stdout
import hashlib
import io
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import wave

from backend.app import tts_gpu_guard as adapter
import module1_agent_director as director


class GuardAdapterTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        clean = {key: value for key, value in os.environ.items()
                 if key not in {"INDEXTTS25_GPU_GUARD", "INDEXTTS25_GPU_GUARD_SHA256", "OCV_COMFYUI_GUARD_SCRIPT"}}
        env = patch.dict(os.environ, clean, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.guard = self.root / "installed_guard.py"
        self.guard.write_text("# Test argv only; never executed.\n", encoding="utf-8")

    def configure(self):
        os.environ["INDEXTTS25_GPU_GUARD"] = str(self.guard)
        os.environ["INDEXTTS25_GPU_GUARD_SHA256"] = hashlib.sha256(self.guard.read_bytes()).hexdigest()

    def test_standalone_install_without_shared_guard_keeps_local_path(self):
        command = ["python", "runner.py"]
        self.assertEqual(adapter.guarded_tts_command(command, owner=None, task_id="manual"), (command, False))

    def test_shared_guard_requires_explicit_tts_guard_and_matching_hash(self):
        os.environ["OCV_COMFYUI_GUARD_SCRIPT"] = str(self.root / "h3_guard.py")
        with self.assertRaisesRegex(RuntimeError, "拒绝无保护推理"):
            adapter.tts_guard_script()
        self.configure()
        self.assertEqual(adapter.tts_guard_script(), self.guard)
        self.guard.write_text("# changed deployment\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "版本已变化"):
            adapter.guarded_tts_command(["runner"], owner=1, task_id="job-1")

    def test_missing_bad_hash_relative_or_directory_guard_refuses(self):
        self.configure()
        for value in ["", "g" * 64, "a" * 63]:
            os.environ["INDEXTTS25_GPU_GUARD_SHA256"] = value
            with self.assertRaisesRegex(RuntimeError, "版本摘要"):
                adapter.tts_guard_script()
        self.configure()
        for value in ["guard.py", str(self.root), str(self.root / "missing.py")]:
            os.environ["INDEXTTS25_GPU_GUARD"] = value
            with self.assertRaises(RuntimeError):
                adapter.tts_guard_script()

    def test_argv_has_fixed_owner_task_boundary_and_no_shell(self):
        self.configure()
        native = ["/venv/bin/python", "runner.py", "--voice", "has spaces.mp3"]
        command, guarded = adapter.guarded_tts_command(native, owner=1, task_id="job-123")
        self.assertTrue(guarded)
        self.assertEqual(command, [sys.executable, "-B", str(self.guard), "--owner", "ocv-user-1",
                                   "--task-id", "job-123", "--", *native])
        for owner, task in [(None, "job"), (0, "job"), (True, "job"), (1, "bad task"), (1, "../job")]:
            with self.assertRaises(RuntimeError):
                adapter.guarded_tts_command(native, owner=owner, task_id=task)

    def test_guard_failure_refuses_before_runner_or_temporary_output(self):
        os.environ["OCV_COMFYUI_GUARD_SCRIPT"] = "shared-policy-is-present"
        config = SimpleNamespace(missing_resources=lambda: [])
        with patch.object(director, "load_indextts25_config", return_value=config), patch.object(
            director, "ensure_dir"
        ) as mkdir, patch.object(director, "_run_and_stream") as run:
            with self.assertRaises(RuntimeError):
                director.step2_indextts25_synthesize(["new story"], SimpleNamespace())
            mkdir.assert_not_called()
            run.assert_not_called()

    def test_requested_parallelism_executes_one_guarded_batch(self):
        self.configure()
        voice = self.root / "mother.wav"
        voice.write_bytes(b"reference-fixture")
        config = SimpleNamespace(missing_resources=lambda: [], runtime_environment=lambda: {},
                                 root=self.root, runner_cwd=self.root / "isolated-runner", device="cuda:0")
        args = SimpleNamespace(tts_voice_path=str(voice), tts_voice_id="immutable-reference", user_id=1,
                               tts_emotion=None, tts_parallelism=3, tts_speed=1, tts_volume=1,
                               tts_pitch=0, job_id="fresh-job")
        calls = []

        def build(_config, **kwargs):
            return ["never-executed-runner", str(kwargs["manifest"]), str(kwargs["output_dir"])]

        def run(command, **kwargs):
            calls.append((command, kwargs))
            native = command[command.index("--") + 1:]
            rows = Path(native[1]).read_text(encoding="utf-8").splitlines()
            for index, _row in enumerate(rows, 1):
                with wave.open(str(Path(native[2]) / f"chunk-{index:04d}.wav"), "wb") as wav:
                    wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                    wav.writeframes(b"\0\0" * 160)
                kwargs["on_generated"]()

        output = io.StringIO()
        with patch.object(director, "load_indextts25_config", return_value=config), patch.object(
            director, "TEMP_DIR", self.root / "temp"
        ), patch.object(director, "_build_indextts25_command", side_effect=build), patch.object(
            director, "_run_and_stream", side_effect=run
        ), patch.object(director, "_apply_audio_controls"), redirect_stdout(output):
            paths, subtitles = director.step2_indextts25_synthesize(["one", "two", "three"], args)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(paths), 3)
        self.assertEqual(len(subtitles), 3)
        self.assertTrue(calls[0][1]["guarded"])
        self.assertEqual(calls[0][1]["cwd"], config.runner_cwd)
        self.assertIn("requested=3 effective=1", output.getvalue())
        self.assertEqual(args.tts_parallelism, 3)
        self.assertEqual(args.tts_voice_id, "immutable-reference")

    def test_nonzero_guard_exit_never_retries_direct_runner(self):
        process = Mock(returncode=3)
        process.communicate.return_value = (b'{"code":"TTS_GPU_LOCK_BUSY"}\n', None)
        process.poll.return_value = 3
        with patch.object(director.subprocess, "Popen", return_value=process) as popen, patch.object(director.sys, "platform", "linux"):
            with self.assertRaises(RuntimeError):
                director._run_and_stream(["fixture-guard"], cwd=self.root, env={}, guarded=True)
        popen.assert_called_once()
        process.terminate.assert_not_called()
        process.kill.assert_not_called()

    def test_interrupt_during_spawn_waits_for_handler_registration(self):
        process = Mock(pid=12345)
        process.poll.return_value = None
        process.communicate.side_effect = [subprocess.TimeoutExpired(["guard"], .2), (b"", None)]

        def spawning(*args, **kwargs):
            # A real SIGINT invokes this installed handler during Popen. It
            # must defer both interruptions until the handle is assigned.
            handler = signal.getsignal(signal.SIGINT)
            handler(signal.SIGINT, None)
            handler(signal.SIGINT, None)
            return process

        previous = signal.getsignal(signal.SIGINT)
        with patch.object(director.subprocess, "Popen", side_effect=spawning) as popen, patch.object(
            director.sys, "platform", "linux"
        ), patch.object(director, "_guard_accepts_sigterm", side_effect=[False, True]) as armed:
            with self.assertRaises(KeyboardInterrupt):
                director._run_and_stream(["fixture-guard"], cwd=self.root, env={}, guarded=True)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(armed.call_count, 2)
        process.terminate.assert_called_once()
        self.assertEqual(process.communicate.call_count, 2)
        self.assertTrue(all(call.kwargs["timeout"] == .2 for call in process.communicate.call_args_list))
        process.kill.assert_not_called()
        self.assertIs(signal.getsignal(signal.SIGINT), previous)

    def test_cleanup_contains_interrupts_from_poll_and_proc_checks(self):
        process = Mock(pid=12345)
        process.poll.side_effect = [KeyboardInterrupt(), None, None, None]
        process.communicate.side_effect = [KeyboardInterrupt(), (b"", None)]
        with patch.object(director, "_guard_accepts_sigterm", side_effect=[KeyboardInterrupt(), True, True]):
            director._wait_for_guard_release(process)
        process.terminate.assert_called_once()
        process.kill.assert_not_called()

    def test_unreadable_proc_waits_for_natural_exit_without_signal(self):
        process = Mock(pid=12345)
        process.poll.side_effect = [None, None, 0]
        process.communicate.side_effect = [subprocess.TimeoutExpired(["guard"], .2),
                                           subprocess.TimeoutExpired(["guard"], .2)]
        with patch.object(director.sys, "platform", "linux"), patch.object(director.Path, "read_text", side_effect=OSError("not readable")):
            director._wait_for_guard_release(process)
        process.terminate.assert_not_called()
        process.kill.assert_not_called()

    def test_proc_signal_mask_is_read_only_for_own_pid(self):
        process = Mock(pid=4321)
        term_mask = 1 << (signal.SIGTERM - 1)
        with patch.object(director.sys, "platform", "linux"), patch.object(
            director.Path, "read_text", autospec=True, return_value=f"Name:\tfixture\nSigCgt:\t{term_mask:016x}\n"
        ) as read:
            self.assertTrue(director._guard_accepts_sigterm(process))
            self.assertEqual(str(read.call_args.args[0]).replace("\\", "/"), "/proc/4321/status")
        for value in ["SigCgt:\t0000000000000000", "SigCgt:\tinvalid", "Name:\tfixture"]:
            with patch.object(director.sys, "platform", "linux"), patch.object(director.Path, "read_text", return_value=value):
                self.assertFalse(director._guard_accepts_sigterm(process))

    def test_guarded_stream_deduplicates_incremental_utf8_and_generated_lines(self):
        output = "日志😀\nGenerated: one.wav\n".encode("utf-8")
        process = Mock(returncode=0)
        process.communicate.side_effect = [subprocess.TimeoutExpired(["guard"], .2, output=output[:7]),
                                           subprocess.TimeoutExpired(["guard"], .2, output=output),
                                           (output + b"Generated: two.wav\n", None)]
        process.poll.return_value = 0
        callback = Mock()
        with patch.object(director.subprocess, "Popen", return_value=process), patch.object(director.sys, "platform", "linux"):
            director._run_and_stream(["guard"], cwd=self.root, env={}, on_generated=callback, guarded=True)
        self.assertEqual(callback.call_count, 2)

    def test_spawn_error_restores_signal_handler(self):
        previous = signal.getsignal(signal.SIGINT)
        with patch.object(director.subprocess, "Popen", side_effect=OSError("spawn refused")), patch.object(director.sys, "platform", "linux"):
            with self.assertRaises(OSError):
                director._run_and_stream(["missing"], cwd=self.root, env={}, guarded=True)
        self.assertIs(signal.getsignal(signal.SIGINT), previous)

    def test_guarded_background_thread_is_refused_before_spawn(self):
        with patch.object(director.threading, "current_thread", return_value=object()), patch.object(
            director.sys, "platform", "linux"
        ), patch.object(director.subprocess, "Popen") as spawn:
            with self.assertRaisesRegex(RuntimeError, "Linux 主线程"):
                director._run_and_stream(["guard"], cwd=self.root, env={}, guarded=True)
        spawn.assert_not_called()

    def test_runner_and_guard_flags_disable_bytecode_in_real_cpu_process(self):
        self.configure()
        (self.root / "read_only_fixture.py").write_text("VALUE = 1\n", encoding="utf-8")
        probe = "import sys;assert sys.dont_write_bytecode;sys.path.insert(0,sys.argv[1]);import read_only_fixture"
        config = SimpleNamespace(python=Path(sys.executable), device="cuda:0", language="ZH",
                                 use_bf16=True, use_accel=False, use_torch_compile=False)
        runner = director._build_indextts25_command(config, manifest=self.root / "batch.jsonl",
                   output_dir=self.root, output_prefix="fixture", voice_path=self.root / "voice.mp3", emotion_vector=None)
        guard, _ = adapter.guarded_tts_command(["never-run"], owner=1, task_id="cpu-test")
        for prefix in [runner[:runner.index("--batch-file") - 1], guard[:guard.index(str(self.guard))]]:
            with self.subTest(prefix=prefix):
                result = subprocess.run([*prefix, "-c", probe, str(self.root)], capture_output=True, timeout=10,
                                        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "0"})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse((self.root / "__pycache__").exists())


if __name__ == "__main__":
    unittest.main()
