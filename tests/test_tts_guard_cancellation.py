"""Linux CPU cancellation fixtures; never touch a deployed guard or GPU.

This complements the optional exact-external-guard contract tests. The local
fixture deliberately widens the supervisor's child-spawn/handler gap and
writes more than a pipe capacity so cancellation cannot rely on quiet output.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


FIXTURE_GUARD = r'''
import fcntl, json, os, pathlib, signal, subprocess, sys, time
root, mode = pathlib.Path(sys.argv[1]), sys.argv[2]
assert sys.dont_write_bytecode
with (root / 'fixture.lock').open('a+') as lock:
 fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
 child = subprocess.Popen([sys.executable, '-B', '-c', 'import time;time.sleep(1.2)'], start_new_session=True)
 (root / 'spawned').write_text(str(child.pid))
 if mode == 'startup':
  sys.stdout.write('x' * 262144 + '\n')
  sys.stdout.flush()
  (root / 'pipe-drained').write_text('yes')
  time.sleep(.4)
 def forward(signum, frame):
  if child.poll() is None:
   try: os.killpg(child.pid, signum)
   except ProcessLookupError: pass
 signal.signal(signal.SIGTERM, forward)
 signal.signal(signal.SIGINT, forward)
 (root / 'armed').write_text('yes')
 code = child.wait()
 (root / 'state.json').write_text(json.dumps({'status':'RELEASED','return_code':code,'child':child.pid}))
 fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
 raise SystemExit(code)
'''

CONTROLLER = r'''
import os, pathlib, sys
sys.path.insert(0, sys.argv[1])
import module1_agent_director as director
root, mode = pathlib.Path(sys.argv[2]), sys.argv[3]
if mode == 'unreadable':
 director._guard_accepts_sigterm = lambda process: False
try:
 director._run_and_stream([sys.executable, '-B', str(root/'fixture_guard.py'), str(root), mode], cwd=root, env=os.environ.copy(), guarded=True)
except KeyboardInterrupt:
 print('CANCELLED_AFTER_RELEASE', flush=True)
 raise SystemExit(42)
'''


@unittest.skipUnless(sys.platform == "linux", "Linux /proc and POSIX signal fixture")
class CancellationProcessTests(unittest.TestCase):
    def exercise(self, mode, *, double_interrupt=False):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "fixture_guard.py").write_text(FIXTURE_GUARD, encoding="utf-8")
            source = Path(__file__).resolve().parents[1]
            process = subprocess.Popen([sys.executable, "-B", "-c", CONTROLLER, str(source), str(root), mode],
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                       start_new_session=True)
            try:
                marker = root / ("spawned" if mode == "startup" else "armed")
                deadline = time.monotonic() + 8
                while not marker.is_file() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(marker.is_file(), "CPU fixture did not start")
                process.send_signal(signal.SIGINT)
                if double_interrupt:
                    time.sleep(.05)
                    process.send_signal(signal.SIGINT)
                output, _ = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 42, output)
                self.assertIn("CANCELLED_AFTER_RELEASE", output)
                state = json.loads((root / "state.json").read_text())
                self.assertEqual(state["status"], "RELEASED")
                self.assertEqual(state["return_code"], 0 if mode == "unreadable" else -signal.SIGTERM)
                with self.assertRaises(ProcessLookupError):
                    os.kill(state["child"], 0)
                if mode == "startup":
                    self.assertTrue((root / "pipe-drained").is_file())
                self.assertFalse((root / "__pycache__").exists())
            finally:
                if process.poll() is None:
                    # This is only our short-lived CPU fixture. Do not kill a
                    # guard: let its child finish naturally and drain output.
                    process.communicate(timeout=10)

    def test_cancel_before_handlers_with_large_pipe_output_and_double_sigint(self):
        self.exercise("startup", double_interrupt=True)

    def test_cancel_armed_guard_releases_after_own_child_exit(self):
        self.exercise("armed")

    def test_unreadable_proc_waits_for_natural_child_completion(self):
        self.exercise("unreadable", double_interrupt=True)


if __name__ == "__main__":
    unittest.main()
