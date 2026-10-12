"""Configuration checks use filesystem fixtures only; no model or GPU calls."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.app import indextts25_local as local


class RuntimeConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.patch = patch.multiple(local, PROJECT_ROOT=self.root / "app", load_project_env=lambda: None)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        env = {key: value for key, value in os.environ.items() if not key.startswith("INDEXTTS25_")}
        env.update({
            "INDEXTTS25_ROOT": str(self.root / "shared-source"),
            "INDEXTTS25_MODEL_DIR": str(self.root / "shared-models"),
            "INDEXTTS25_PACKAGES_DIR": str(self.root / "venv/lib/site-packages"),
            "INDEXTTS25_EXAMPLES_DIR": str(self.root / "reference-examples"),
            "INDEXTTS25_PYTHON": str(self.root / "venv/bin/python"),
        })
        self.env = patch.dict(os.environ, env, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.config = local.load_indextts25_config()
        for path in [self.config.python, self.config.root / "indextts/infer_v2_5.py",
                     *(self.config.model_dir / name for name in local.REQUIRED_MODEL_FILES)]:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture")
        self.config.python.chmod(0o755)
        for path in [self.config.packages_dir / "whisper", self.config.packages_dir / "tiktoken",
                     *(self.config.model_dir / name for name in local.REQUIRED_MODEL_DIRS)]:
            path.mkdir(parents=True, exist_ok=True)

    def test_unset_python_keeps_portable_default(self):
        del os.environ["INDEXTTS25_PYTHON"]
        config = local.load_indextts25_config()
        self.assertEqual(config.python, local.PROJECT_ROOT / "runtime/python/python.exe")
        self.assertIn("IndexTTS-2.5 Python", config.missing_runtime_resources())
        self.assertFalse(config.python.exists())

    def test_relative_python_uses_same_health_configuration(self):
        self.assertTrue(self.config.ready)
        os.environ["INDEXTTS25_PYTHON"] = "runtime/custom-python"
        config = local.load_indextts25_config()
        self.assertEqual(config.python, local.PROJECT_ROOT / "runtime/custom-python")
        self.assertIn("IndexTTS-2.5 Python", config.missing_runtime_resources())
        self.assertFalse(config.ready)

    def test_venv_launcher_identity_is_not_resolved(self):
        launcher = self.config.python.with_name("venv-launcher")
        try:
            launcher.symlink_to(self.config.python)
        except OSError:
            self.skipTest("This filesystem does not permit creating symlinks")
        os.environ["INDEXTTS25_PYTHON"] = str(launcher)
        config = local.load_indextts25_config()
        self.assertEqual(config.python, launcher)
        self.assertNotEqual(config.python, launcher.resolve())
        self.assertTrue(config.ready)

    def test_uploaded_voice_without_demo_with_complete_model_package(self):
        self.assertFalse(self.config.available_voices())
        # Package completeness is checked independently from the runner's
        # use_qwen_emo=False inference setting; no bundled demo voice is needed.
        self.assertTrue((self.config.model_dir / "qwen0.6bemo4-merge").exists())
        self.assertTrue(self.config.ready)
        voice = local.PROJECT_ROOT / "workspace/editor/user_1/uploads/mother.mp3"
        voice.parent.mkdir(parents=True)
        voice.write_bytes(b"fixture-reference")
        self.assertEqual(local.resolve_voice_reference(self.config, "upload:mother.mp3", user_id=1), voice.resolve())
        for voice_id, owner in [("upload:mother.mp3", 2), ("voice_05.wav", 1)]:
            with self.assertRaises(FileNotFoundError):
                local.resolve_voice_reference(self.config, voice_id, user_id=owner)

    def test_missing_required_model_blocks_readiness(self):
        (self.config.model_dir / "gpt.pth").unlink()
        self.assertEqual(self.config.missing_runtime_resources(), [])
        self.assertIn("2.5 模型文件 gpt.pth", self.config.missing_model_resources())
        self.assertFalse(self.config.ready)

    def test_directory_is_not_interpreter_or_source_file(self):
        for path in [self.config.python, self.config.root / "indextts/infer_v2_5.py"]:
            path.unlink()
            path.mkdir()
        missing = self.config.missing_runtime_resources()
        self.assertIn("IndexTTS-2.5 Python", missing)
        self.assertIn("官方 IndexTTS-2.5 源码", missing)
        self.assertFalse(self.config.ready)

    @unittest.skipIf(os.name == "nt", "POSIX executable permission check")
    def test_nonexecutable_linux_interpreter_is_not_ready(self):
        self.config.python.chmod(0o644)
        self.assertIn("IndexTTS-2.5 Python 不可执行", self.config.missing_runtime_resources())

    def test_isolated_runtime_keeps_caches_and_cwd_within_instance(self):
        os.environ["INDEXTTS25_ISOLATE_RUNNER_CWD"] = "true"
        env = self.config.runtime_environment()
        self.assertEqual(self.config.runner_cwd, self.config.runtime_dir / "runner")
        self.assertTrue(self.config.runner_cwd.is_dir())
        for key in ["HF_HOME", "HF_HUB_CACHE", "TORCH_HOME", "TMPDIR"]:
            self.assertTrue(Path(env[key]).is_relative_to(self.config.runtime_dir))
        for key in ["HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE", "PYTHONDONTWRITEBYTECODE"]:
            self.assertEqual(env[key], "1")
        self.assertFalse((self.config.root / "checkpoints").exists())


if __name__ == "__main__":
    unittest.main()
