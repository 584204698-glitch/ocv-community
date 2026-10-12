"""Optional POSIX contract tests for the exact administrator-installed guard.

Set TEST_EXTERNAL_TTS_GUARD_SCRIPT and TEST_EXTERNAL_TTS_GUARD_SHA256 to the
reviewed deployment. All state/locks are replaced with temporary paths and
resource checks are stubbed before acquire; only harmless CPU children run.
These tests do not grant GPU admission or modify the installed guard.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


@unittest.skipUnless(os.name == "posix" and os.environ.get("TEST_EXTERNAL_TTS_GUARD_SCRIPT"),
                     "Requires explicitly configured reviewed external POSIX guard")
class ExternalGuardContractTests(unittest.TestCase):
    def setUp(self):
        path = Path(os.environ["TEST_EXTERNAL_TTS_GUARD_SCRIPT"])
        expected = os.environ.get("TEST_EXTERNAL_TTS_GUARD_SHA256", "")
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), expected,
                         "External guard must match the independently reviewed SHA")
        spec = importlib.util.spec_from_file_location("external_tts_guard_fixture", path)
        self.guard = importlib.util.module_from_spec(spec)
        # Compile the reviewed bytes directly: this must stay read-only even
        # when a caller forgot to start the test interpreter with -B.
        exec(compile(path.read_bytes(), str(path), "exec"), self.guard.__dict__)
        self.script = path
        self.expected = expected
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.guard.LOCK = self.root / "gpu-test.lock"
        self.guard.STATE = self.root / "state.json"
        self.guard.EVENTS = self.root / "events.jsonl"
        # No live queue, nvidia-smi or host-resource query is performed.
        self.guard.resource_preflight = lambda args: (None, {"fixture": "cpu-only"})

    def args(self, command):
        return SimpleNamespace(owner="test-only", task_id="test-contract", command=command)

    def test_lock_contention_rejects_without_spawning_child_or_writing_state(self):
        import fcntl
        with self.guard.LOCK.open("a+") as holder:
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(self.guard.subprocess, "Popen") as spawn:
                self.assertEqual(self.guard.acquire(self.args([sys.executable, "-B", "-c", "pass"])), 3)
                spawn.assert_not_called()
            self.assertFalse(self.guard.STATE.exists())
            fcntl.flock(holder.fileno(), fcntl.LOCK_UN)

    def test_success_and_failed_cpu_child_both_release_lock(self):
        import fcntl
        for code in [0, 7]:
            with self.subTest(exit_code=code):
                result = self.guard.acquire(self.args([sys.executable, "-B", "-c", f"raise SystemExit({code})"]))
                self.assertEqual(result, code)
                state = json.loads(self.guard.STATE.read_text(encoding="utf-8"))
                self.assertEqual(state["status"], "RELEASED")
                self.assertEqual(state["return_code"], code)
                with self.guard.LOCK.open("a+") as next_owner:
                    fcntl.flock(next_owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(next_owner.fileno(), fcntl.LOCK_UN)

    def test_sigterm_waits_for_own_cpu_child_and_records_release(self):
        # Run the installed source against temporary state, with a local CPU
        # sleeping child. ARMED is printed only after handlers are installed.
        wrapper = '''import hashlib, importlib.util, pathlib, signal, sys
from types import SimpleNamespace
path, expected, directory = sys.argv[1:]
assert hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest() == expected
spec = importlib.util.spec_from_file_location("guard_under_test", path)
guard = importlib.util.module_from_spec(spec)
exec(compile(pathlib.Path(path).read_bytes(), path, "exec"), guard.__dict__)
root = pathlib.Path(directory)
guard.LOCK, guard.STATE, guard.EVENTS = root/"gpu-test.lock", root/"state.json", root/"events.jsonl"
guard.resource_preflight = lambda args: (None, {"fixture":"cpu-only"})
install = signal.signal
def install_and_mark(signum, handler):
 result = install(signum, handler)
 if signum == signal.SIGINT and callable(handler) and getattr(handler, "__name__", "") == "forward":
  print("ARMED", flush=True)
 return result
signal.signal = install_and_mark
raise SystemExit(guard.acquire(SimpleNamespace(owner="test-only", task_id="cancel-contract", command=[sys.executable,"-B","-c","import time;time.sleep(10)"])))
'''
        process = subprocess.Popen([sys.executable, "-B", "-c", wrapper, str(self.script), self.expected, str(self.root)],
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
        try:
            lines = []
            for line in process.stdout:
                lines.append(line)
                if line.strip() == "ARMED":
                    break
            self.assertTrue(any(line.strip() == "ARMED" for line in lines), "".join(lines))
            process.terminate()
            process.communicate(timeout=15)
            state = json.loads(self.guard.STATE.read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "RELEASED")
            self.assertEqual(state["return_code"], -15)
        finally:
            if process.poll() is None:
                # Only this CPU fixture; no GPU is involved and no shared path
                # is used. Let the 10-second child complete naturally.
                process.communicate(timeout=15)


if __name__ == "__main__":
    unittest.main()
