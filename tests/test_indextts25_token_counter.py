"""Cross-interpreter token IPC tests; fixtures never import models or Torch."""
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from backend.app import indextts25_token_counter as helper
from backend.app import tts_segmentation as segmentation


class TokenCounterTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.python = self.root / "python"
        self.python.write_bytes(b"interpreter-fixture")
        self.python.chmod(0o755)
        self.vocab = self.root / "multilingual_zh_ja_yue_char_del.tiktoken"
        self.vocab.write_text("YQ== 0\n", encoding="utf-8")
        self.config = SimpleNamespace(python=self.python, model_dir=self.root, packages_dir=self.root / "packages")

    def test_other_interpreter_uses_stdin_and_never_imports_cross_abi_package(self):
        with patch.object(segmentation, "load_encoding", side_effect=AssertionError("cross ABI import")), patch.object(
            segmentation.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=b"7\n")
        ) as run:
            count = segmentation.build_indextts25_token_counter(self.config)
            self.assertEqual(count("秘密旁白"), 7)
            self.assertEqual(count("秘密旁白"), 7)
            run.assert_called_once()
            command = run.call_args.args[0]
            self.assertEqual(command[:3], [str(self.python), "-I", "-B"])
            self.assertEqual(command[3], str(Path(helper.__file__)))
            self.assertNotIn("秘密旁白", command)
            self.assertEqual(run.call_args.kwargs["input"], "秘密旁白".encode("utf-8"))
            self.assertEqual(run.call_args.kwargs["timeout"], 20)

    def test_same_interpreter_keeps_direct_counter(self):
        self.config.python = Path(sys.executable)
        encoding = Mock()
        encoding.encode.return_value = [1, 2]
        with patch.object(segmentation, "_official_encoding", return_value=encoding), patch.object(segmentation.subprocess, "run") as run:
            count = segmentation.build_indextts25_token_counter(self.config)
            self.assertEqual(count("hello"), 2)
            encoding.encode.assert_called_once_with("hello", disallowed_special=())
            run.assert_not_called()

    def test_failed_or_invalid_result_does_not_fallback_or_expose_stderr(self):
        for result in [SimpleNamespace(returncode=1, stdout=b"0", stderr=b"sensitive"),
                       SimpleNamespace(returncode=0, stdout=b"-1"),
                       SimpleNamespace(returncode=0, stdout=b"12\nwarning"),
                       SimpleNamespace(returncode=0, stdout=b"")]:
            with self.subTest(result=result), patch.object(segmentation.subprocess, "run", return_value=result):
                count = segmentation.build_indextts25_token_counter(self.config)
                with self.assertRaisesRegex(RuntimeError, "返回无效结果") as caught:
                    count("private narration")
                self.assertNotIn("sensitive", str(caught.exception))

    def test_timeout_and_missing_interpreter_fail_closed(self):
        with patch.object(segmentation.subprocess, "run", side_effect=subprocess.TimeoutExpired(["fixture"], 20)):
            count = segmentation.build_indextts25_token_counter(self.config)
            with self.assertRaisesRegex(RuntimeError, "未能完成"):
                count("text")
        self.python.unlink()
        with self.assertRaises(FileNotFoundError):
            segmentation.build_indextts25_token_counter(self.config)

    def test_real_cpu_subprocess_obeys_utf8_stdin_and_uses_selected_packages(self):
        # The stub verifies IPC/bootstrap only; it is not an official-tokenizer
        # equivalence or synthesis test. It must never import Torch.
        self.config.packages_dir.mkdir()
        (self.config.packages_dir / "tiktoken.py").write_text(
            "import sys\nassert 'torch' not in sys.modules\nassert sys.dont_write_bytecode\n"
            "class Encoding:\n"
            " def __init__(self, **kwargs):\n"
            "  assert kwargs['mergeable_ranks'] == {b'a': 0}\n"
            " def encode(self, text, disallowed_special):\n"
            "  assert disallowed_special == ()\n"
            "  return list(text)\n", encoding="utf-8")
        result = subprocess.run([sys.executable, "-I", "-B", str(Path(helper.__file__)),
                                 "--vocab", str(self.vocab), "--packages", str(self.config.packages_dir)],
                                input="新的旁白😀".encode("utf-8"), capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        self.assertEqual(result.stdout.strip(), b"5")
        self.assertFalse((self.config.packages_dir / "__pycache__").exists())


if __name__ == "__main__":
    unittest.main()
