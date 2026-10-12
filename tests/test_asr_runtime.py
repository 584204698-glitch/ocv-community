"""ASR runtime selection must preserve the environment owning the executable."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from backend.app import pipeline


class AsrRuntimeSelectionTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.selected = self.python_file(self.root / "isolated-venv" / "bin" / "python")
        self.base = self.python_file(self.root / "base-python" / "bin" / "python")
        environment = patch.dict(pipeline.os.environ, {"ASR_PYTHON": str(self.selected)}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    @staticmethod
    def python_file(path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("interpreter fixture; never executed", encoding="utf-8")
        return path

    def test_configured_runtime_checks_the_selected_environment_not_its_base(self) -> None:
        # Model the venv alias on every platform, including Windows hosts that
        # do not grant filesystem symlink privileges. Only the selected venv
        # can import the ASR dependencies; its base Python cannot.
        with (
            patch.object(Path, "resolve", return_value=self.base),
            patch.object(pipeline, "_asr_runtime_available", side_effect=lambda path: (
                (True, "") if path == str(self.selected) else (False, "missing ASR dependencies")
            )) as probe,
        ):
            actual = pipeline.resolve_asr_python()
        self.assertEqual(actual, str(self.selected))
        probe.assert_called_once_with(str(self.selected))

    def test_real_interpreter_symlink_is_not_replaced_by_its_target(self) -> None:
        alias = self.root / "symlink-venv" / "bin" / "python"
        alias.parent.mkdir(parents=True)
        try:
            alias.symlink_to(self.base)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"filesystem symlink unavailable: {exc}")
        self.assertEqual(alias.resolve(), self.base)
        with (
            patch.dict(pipeline.os.environ, {"ASR_PYTHON": str(alias)}),
            patch.object(pipeline, "_asr_runtime_available", side_effect=lambda path: (
                (True, "") if path == str(alias) else (False, "missing ASR dependencies")
            )) as probe,
        ):
            actual = pipeline.resolve_asr_python()
        self.assertEqual(actual, str(alias))
        probe.assert_called_once_with(str(alias))

    def test_fallback_checks_distinct_venvs_even_when_they_share_a_base_python(self) -> None:
        prefix = self.root / "runtime"
        fallback = self.python_file(prefix / "envs" / "pytorch2.11" / "bin" / "python")
        with (
            patch.dict(pipeline.os.environ, {"ASR_PYTHON": ""}),
            patch.object(pipeline.sys, "executable", str(self.selected)),
            patch.object(pipeline.sys, "prefix", str(prefix)),
            patch.object(Path, "home", return_value=self.root / "home"),
            patch.object(pipeline, "PROJECT_ROOT", self.root / "app"),
            patch.object(Path, "resolve", return_value=self.base),
            patch.object(pipeline, "_asr_runtime_available", side_effect=lambda path: (
                (True, "") if path == str(fallback) else (False, "missing ASR dependencies")
            )) as probe,
        ):
            actual = pipeline.resolve_asr_python()
        self.assertEqual(actual, str(fallback))
        self.assertEqual(probe.call_args_list, [call(str(self.selected)), call(str(fallback))])

    def test_explicit_runtime_with_missing_dependencies_does_not_fall_back(self) -> None:
        with (
            patch.object(pipeline.sys, "executable", str(self.base)),
            patch.object(pipeline, "_asr_runtime_available", return_value=(False, "missing ctranslate2")) as probe,
            self.assertRaisesRegex(RuntimeError, "ASR_PYTHON.*missing ctranslate2"),
        ):
            pipeline.resolve_asr_python()
        probe.assert_called_once_with(str(self.selected))

    def test_command_lookup_preserves_the_found_environment_path(self) -> None:
        with (
            patch.dict(pipeline.os.environ, {"ASR_PYTHON": "selected-asr-python"}),
            patch.object(pipeline.shutil, "which", return_value=str(self.selected)) as which,
            patch.object(Path, "resolve", return_value=self.base),
            patch.object(pipeline, "_asr_runtime_available", return_value=(True, "")) as probe,
        ):
            actual = pipeline.resolve_asr_python()
        self.assertEqual(actual, str(self.selected))
        which.assert_called_once_with("selected-asr-python")
        probe.assert_called_once_with(str(self.selected))

    def test_missing_configured_executable_is_not_probed(self) -> None:
        with (
            patch.dict(pipeline.os.environ, {"ASR_PYTHON": str(self.root / "missing-python")}),
            patch.object(pipeline, "_asr_runtime_available") as probe,
            self.assertRaisesRegex(RuntimeError, "ASR_PYTHON"),
        ):
            pipeline.resolve_asr_python()
        probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
