"""Isolated-launcher regression tests use synthetic code, config and databases."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER_AVAILABLE = (ROOT / "scripts" / "run_isolated.py").is_file()
if LAUNCHER_AVAILABLE:
    SPEC = importlib.util.spec_from_file_location("ocv_isolated_launcher", ROOT / "scripts" / "run_isolated.py")
    launcher = importlib.util.module_from_spec(SPEC)
    sys.modules[SPEC.name] = launcher
    SPEC.loader.exec_module(launcher)


@unittest.skipUnless(LAUNCHER_AVAILABLE, "Optional private isolated launcher is not distributed with the public application")
class IsolatedLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "source"
        self.state = self.root / "state"
        files = {
            "backend/__init__.py": "",
            "backend/app/__init__.py": "",
            "backend/app/main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
            "backend/app/config.py": "from pathlib import Path\nENV_PATH = Path(__file__).resolve().parents[2] / '.env'\n",
            "backend/app/db.py": "import os\nfrom pathlib import Path\nDB_BACKEND = os.environ['DB_BACKEND']\nSQLITE_PATH = Path(os.environ['SQLITE_PATH'])\ndef db_status(): return {'ready': True}\n",
            "backend/app/pipeline.py": "from pathlib import Path\nWORKSPACE_DIR = Path(__file__).resolve().parents[2] / 'workspace'\n",
            "frontend/dist/index.html": "<html>isolated synthetic frontend</html>",
            "frontend/dist/assets/app.js": "export const synthetic = true;",
            "story_agents.py": "# synthetic public source\n",
            ".env": "API_KEY=synthetic-source-secret\nSQLITE_PATH=/old/production.db\n",
            "workspace/video_studio/old/record.json": '{"synthetic": "old user data"}',
            "local-data/old.sqlite3": "synthetic old database",
            "tts_voices/private.wav": "synthetic user media",
        }
        for name in ("video_studio", "comfyui_bridge", "image_studio"):
            files[f"backend/app/{name}.py"] = f"from pathlib import Path\nROOT = Path(__file__).resolve().parents[2] / 'workspace' / '{name}'\n"
        for relative, content in files.items():
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def prepare(self, *, reuse=False):
        source, state = launcher.validate_roots(self.source, self.state, reuse=reuse)
        with launcher.locked_state(state, reuse=reuse):
            instance = launcher.prepare_instance(source, state, reuse=reuse, name="test-review")
            launcher.prepare_database(instance)
            return instance

    def test_snapshot_does_not_copy_configuration_database_or_user_media(self):
        instance = self.prepare()
        self.assertEqual((instance.app / ".env").read_text(encoding="utf-8"), "")
        self.assertFalse((instance.app / "workspace" / "video_studio").exists())
        self.assertFalse((instance.app / "local-data").exists())
        self.assertFalse((instance.app / "tts_voices").exists())
        self.assertTrue((instance.app / "backend" / "app" / "main.py").is_file())
        self.assertTrue((instance.app / "frontend" / "dist" / "assets" / "app.js").is_file())
        with contextlib.closing(sqlite3.connect(instance.database)) as connection:
            self.assertEqual(connection.execute("SELECT instance_id FROM ocv_isolated_identity").fetchall(), [(instance.identity,)])
        self.assertIn("synthetic-source-secret", (self.source / ".env").read_text(encoding="utf-8"))

    def test_reuse_requires_explicit_flag_and_original_state_path(self):
        self.prepare()
        with self.assertRaisesRegex(launcher.IsolationError, "--reuse"):
            launcher.validate_roots(self.source, self.state, reuse=False)
        marker_file = self.state / launcher.MARKER
        marker = json.loads(marker_file.read_text(encoding="utf-8"))
        marker["state_root"] = str(self.root / "different-state")
        marker_file.write_text(json.dumps(marker), encoding="utf-8")
        with self.assertRaisesRegex(launcher.IsolationError, "does not belong"):
            launcher.validate_roots(self.source, self.state, reuse=True)

    def test_unmarked_existing_directory_and_overlapping_roots_are_rejected(self):
        (self.state / "workspace").mkdir(parents=True)
        for reuse in (False, True):
            with self.subTest(reuse=reuse), self.assertRaisesRegex(launcher.IsolationError, "unmarked"):
                launcher.validate_roots(self.source, self.state, reuse=reuse)
        with self.assertRaisesRegex(launcher.IsolationError, "non-overlapping"):
            launcher.validate_roots(self.source, self.source / "state", reuse=False)

    def test_reuse_preserves_instance_data_and_updates_only_managed_code(self):
        stale = self.source / "backend" / "app" / "retired.py"
        stale.write_text("# public old source\n", encoding="utf-8")
        first = self.prepare()
        (first.app / ".env").write_text("API_KEY=synthetic-instance-secret\n", encoding="utf-8")
        asset = first.app / "workspace" / "new-asset.png"
        asset.write_bytes(b"synthetic new asset")
        stale.unlink()
        (self.source / "story_agents.py").write_text("# revised public code\n", encoding="utf-8")
        second = self.prepare(reuse=True)
        self.assertEqual(first.identity, second.identity)
        self.assertEqual(asset.read_bytes(), b"synthetic new asset")
        self.assertIn("synthetic-instance-secret", (second.app / ".env").read_text(encoding="utf-8"))
        self.assertEqual((second.app / "story_agents.py").read_text(encoding="utf-8"), "# revised public code\n")
        self.assertFalse((second.app / "backend" / "app" / "retired.py").exists())

    def test_state_root_and_nested_symlinks_are_rejected(self):
        target = self.root / "outside"
        target.mkdir()
        try:
            self.state.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest("This environment cannot create symbolic links")
        with self.assertRaisesRegex(launcher.IsolationError, "links"):
            launcher.validate_roots(self.source, self.state, reuse=False)
        self.state.unlink()
        instance = self.prepare()
        nested = instance.app / "workspace" / "old-data"
        nested.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(launcher.IsolationError, "links"):
            launcher.validate_roots(self.source, self.state, reuse=True)
        nested.unlink()

    def test_hard_linked_mutable_file_is_rejected(self):
        instance = self.prepare()
        outside = self.root / "shared.json"
        outside.write_text("{}", encoding="utf-8")
        try:
            os.link(outside, instance.app / "workspace" / "linked.json")
        except OSError:
            self.skipTest("This filesystem cannot create hard links")
        with self.assertRaisesRegex(launcher.IsolationError, "hard-linked"):
            launcher.validate_roots(self.source, self.state, reuse=True)

    def test_source_symlink_and_unsafe_manifest_are_rejected(self):
        instance = self.prepare()
        manifest = self.state / launcher.CODE_MANIFEST
        manifest.write_text(json.dumps({"../outside.txt": "digest"}), encoding="utf-8")
        with launcher.locked_state(self.state, reuse=True):
            with self.assertRaisesRegex(launcher.IsolationError, "Unsafe"):
                launcher.prepare_instance(self.source, self.state, reuse=True, name="test-review")
        self.assertFalse((self.root / "outside.txt").exists())
        source_link = self.source / "backend" / "app" / "linked.py"
        try:
            source_link.symlink_to(self.source / "story_agents.py")
        except OSError:
            self.skipTest("This environment cannot create symbolic links")
        with self.assertRaisesRegex(launcher.IsolationError, "links"):
            launcher._source_files(self.source)
        source_link.unlink()

    def test_environment_discards_parent_configuration_and_forces_private_paths(self):
        instance = self.prepare()
        inherited = {"PATH": os.environ.get("PATH", ""), "DB_BACKEND": "mysql", "SQLITE_PATH": "/old/db",
                     "API_KEY": "synthetic-secret", "PYTHONPATH": "/old/app", "PYTHONHOME": "/old/runtime",
                     "HOME": "/old/home", "VISUAL_CHECKPOINT_DIR": "/old/checkpoint",
                     "CLOUD_IMAGE_POOL_SESSION_UPDATE_PATH": "/old/session", "STORY_AGENT_PLAN_PATH": "/old/plan"}
        environment = launcher.isolated_environment(instance, 8878, inherited)
        self.assertEqual(environment["DB_BACKEND"], "sqlite")
        self.assertEqual(Path(environment["SQLITE_PATH"]), instance.database)
        self.assertEqual(environment["PYTHONPATH"], str(instance.app))
        for key in ("API_KEY", "PYTHONHOME", "VISUAL_CHECKPOINT_DIR", "CLOUD_IMAGE_POOL_SESSION_UPDATE_PATH", "STORY_AGENT_PLAN_PATH"):
            self.assertNotIn(key, environment)
        for key in ("HOME", "USERPROFILE", "TMPDIR", "TMP", "TEMP", "HF_HOME", "HF_HUB_CACHE", "TORCH_HOME",
                    "XDG_CACHE_HOME", "NUMBA_CACHE_DIR", "MPLCONFIGDIR", "CUDA_CACHE_PATH", "CLOUD_SESSION_STORE_PATH",
                    "OCV_RENDER_WORKSPACE_DIR", "OCV_RENDER_TEMP_DIR"):
            self.assertTrue(launcher._inside(Path(environment[key]), instance.root), key)
        self.assertEqual(environment["OCV_SESSION_COOKIE_NAME"], instance.cookie_name)
        self.assertNotEqual(environment["SESSION_SECRET"], "synthetic-secret")

    def test_session_cookie_survives_launcher_restart(self):
        instance = self.prepare()
        first = launcher.isolated_environment(instance, 8878)
        restarted = self.prepare(reuse=True)
        second = launcher.isolated_environment(restarted, 8878, {'SESSION_SECRET': 'untrusted-parent'})
        self.assertEqual(first['SESSION_SECRET'], second['SESSION_SECRET'])
        self.assertEqual(first['OCV_SESSION_COOKIE_NAME'], second['OCV_SESSION_COOKIE_NAME'])
        if os.name != 'nt':
            self.assertEqual((instance.root / launcher.SESSION_SECRET_FILE).stat().st_mode & 0o777, 0o600)
        # Actual auth module in two fresh processes: issue then verify the cookie.
        env = {**os.environ, **first, 'PYTHONPATH': str(ROOT)}
        token = subprocess.check_output([sys.executable, '-c',
            'from backend.app.auth import sign_session; print(sign_session(7))'], env=env, text=True).strip()
        env.update(second, PYTHONPATH=str(ROOT), TEST_SESSION_TOKEN=token)
        result = subprocess.check_output([sys.executable, '-c',
            'import os; from backend.app.auth import read_session; assert read_session(os.environ["TEST_SESSION_TOKEN"])["uid"] == 7; print("verified")'], env=env, text=True)
        self.assertEqual(result.strip(), 'verified')

    def test_bad_session_key_fails_without_rotating(self):
        instance = self.prepare()
        path = instance.root / launcher.SESSION_SECRET_FILE
        path.write_text('broken', encoding='ascii')
        with self.assertRaises(launcher.IsolationError):
            launcher.isolated_environment(instance, 8878)
        self.assertEqual(path.read_text(), 'broken')

    def test_session_key_is_not_shared_between_instances(self):
        instance = self.prepare()
        first = launcher.isolated_environment(instance, 8878)
        self.state = self.root / 'other-state'
        other = self.prepare()
        self.assertNotEqual(first['SESSION_SECRET'], launcher.isolated_environment(other, 8877)['SESSION_SECRET'])

    def test_same_state_cannot_be_locked_by_two_launchers(self):
        self.prepare()
        with launcher.locked_state(self.state, reuse=True):
            with self.assertRaisesRegex(launcher.IsolationError, "already in use"):
                with launcher.locked_state(self.state, reuse=True):
                    self.fail("Second state lock was acquired")

    def test_guard_is_only_enabled_by_explicit_existing_administrator_script(self):
        instance = self.prepare()
        guard = self.root / "guard.py"
        guard.write_text("# synthetic guard; never executed\n", encoding="utf-8")
        inherited = {"OCV_COMFYUI_GUARD_SCRIPT": str(guard)}
        self.assertNotIn("OCV_COMFYUI_GUARD_SCRIPT", launcher.isolated_environment(instance, 8878, inherited))
        environment = launcher.isolated_environment(instance, 8878, inherited, guard_script=guard)
        self.assertEqual(environment["OCV_COMFYUI_GUARD_SCRIPT"], str(guard.resolve()))
        for invalid in (Path("relative.py"), self.root / "missing.py", self.root):
            with self.subTest(path=invalid), self.assertRaises(launcher.IsolationError):
                launcher.validate_guard_script(invalid)

    def test_invalid_guard_fails_before_preparing_state(self):
        with contextlib.redirect_stderr(io.StringIO()):
            result = launcher.main(["--source-root", str(self.source), "--state-root", str(self.state),
                                    "--comfyui-guard-script", str(self.root / "missing.py"), "--prepare-only"])
        self.assertEqual(result, 2)
        self.assertFalse(self.state.exists())

    def test_import_paths_keep_selected_virtualenv_but_not_source_application(self):
        instance = self.prepare()
        runtime = self.source / ".venv"
        packages = str(runtime / "lib" / "site-packages")
        standard_library = str(self.root / "python" / "lib")
        paths = launcher.isolated_import_paths(instance.app, self.source,
                                               [str(self.source), str(self.source / "scripts"), packages, standard_library],
                                               interpreter_root=runtime)
        self.assertEqual(paths[0], str(instance.app))
        self.assertIn(packages, paths)
        self.assertIn(standard_library, paths)
        self.assertNotIn(str(self.source), paths)
        self.assertNotIn(str(self.source / "scripts"), paths)

    def test_occupied_port_fails_before_creating_state(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            with contextlib.redirect_stderr(io.StringIO()):
                result = launcher.main(["--source-root", str(self.source), "--state-root", str(self.state),
                                        "--port", str(port), "--prepare-only"])
        self.assertEqual(result, 2)
        self.assertFalse(self.state.exists())

    def test_listener_never_binds_public_interfaces(self):
        with self.assertRaisesRegex(launcher.IsolationError, "127.0.0.1"):
            launcher.reserve_listener("0.0.0.0", 8878)

    def test_foreign_database_is_rejected_even_inside_a_marked_state(self):
        instance = self.prepare()
        instance.database.unlink()
        with contextlib.closing(sqlite3.connect(instance.database)) as connection:
            connection.execute("CREATE TABLE users (id INTEGER)")
            connection.commit()
        with self.assertRaisesRegex(launcher.IsolationError, "without an isolated"):
            launcher.prepare_database(instance)

    def test_database_with_different_identity_is_rejected(self):
        instance = self.prepare()
        with contextlib.closing(sqlite3.connect(instance.database)) as connection:
            connection.execute("UPDATE ocv_isolated_identity SET instance_id = ?", ("0" * 32,))
            connection.commit()
        with self.assertRaisesRegex(launcher.IsolationError, "different instance"):
            launcher.prepare_database(instance)

    def test_queued_and_running_jobs_block_automatic_recovery(self):
        instance = self.prepare()
        with contextlib.closing(sqlite3.connect(instance.database)) as connection:
            connection.execute("CREATE TABLE generation_jobs (status TEXT)")
            connection.execute("INSERT INTO generation_jobs VALUES ('completed')")
            connection.commit()
        launcher.prepare_database(instance)
        for status in ("queued", "running"):
            with contextlib.closing(sqlite3.connect(instance.database)) as connection:
                connection.execute("UPDATE generation_jobs SET status = ?", (status,))
                connection.commit()
            with self.subTest(status=status), self.assertRaisesRegex(launcher.IsolationError, "automatic startup recovery"):
                launcher.prepare_database(instance)

    def test_bootstrap_imports_snapshot_and_mounts_frontend_after_api(self):
        instance = self.prepare()
        program = r'''
import importlib.util, json, os, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location('isolated', sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
root = Path(sys.argv[2])
marker = json.loads((root / module.MARKER).read_text(encoding='utf-8'))
instance = module.Instance(root, root / 'app', root / 'database' / 'ocv.sqlite3', marker['instance_id'], marker['name'])
app = module.load_application(instance, Path(sys.argv[3]), 8878)
import backend.app.main as native
identity = next(route for route in app.routes if route.path == '/api/isolated-instance').endpoint()
print(json.dumps({'module': native.__file__, 'cwd': os.getcwd(), 'cookie': os.environ['OCV_SESSION_COOKIE_NAME'],
                  'last_route': app.routes[-1].path, 'identity': identity}))
'''
        completed = subprocess.run([sys.executable, "-c", program, str(ROOT / "scripts" / "run_isolated.py"),
                                    str(instance.root), str(self.source)], capture_output=True, text=True, check=True,
                                   env=launcher.isolated_environment(instance, 8878))
        report = json.loads(completed.stdout)
        self.assertTrue(launcher._inside(Path(report["module"]), instance.app))
        self.assertEqual(Path(report["cwd"]), instance.app)
        self.assertEqual(report["cookie"], instance.cookie_name)
        self.assertEqual(report["last_route"], "")
        self.assertEqual(report["identity"]["instance_id"], instance.identity)


@unittest.skipUnless(LAUNCHER_AVAILABLE, "Optional private isolated launcher is not distributed with the public application")
class IsolatedCookieTests(unittest.TestCase):
    def cookie(self, value=None):
        program = r'''
import importlib.util, sys, types
database = types.ModuleType('backend.app.db')
database.get_user_by_id = lambda identity: None
sys.modules['backend.app.db'] = database
spec = importlib.util.spec_from_file_location('backend.app.auth', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
print(module.COOKIE_NAME)
'''
        environment = {key: value for key, value in os.environ.items() if key.upper() in launcher.RUNTIME_ENV_KEYS}
        if value is not None:
            environment["OCV_SESSION_COOKIE_NAME"] = value
        return subprocess.run([sys.executable, "-c", program, str(ROOT / "backend" / "app" / "auth.py")],
                              capture_output=True, text=True, env=environment)

    def test_default_cookie_remains_compatible(self):
        result = self.cookie()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "voice_video_session")

    def test_isolated_cookie_is_configurable(self):
        result = self.cookie("ocv_isolated_test")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "ocv_isolated_test")

    def test_invalid_cookie_name_fails_before_serving(self):
        result = self.cookie("unsafe; name")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Invalid OCV_SESSION_COOKIE_NAME", result.stderr)


if __name__ == "__main__":
    unittest.main()
