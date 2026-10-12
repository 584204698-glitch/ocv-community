"""CPU-only token counter executed by the configured IndexTTS interpreter."""
from __future__ import annotations

import argparse
import base64
from pathlib import Path
import sys


TOKEN_PATTERN = r"'s|'t|'re|'ve|'m|'ll|'d| ?\p{L}+| ?\p{N}+| ?[^\s\p{L}\p{N}]+|\s+(?!\S)|\s+"


def load_encoding(vocab_path: Path, packages_dir: Path):
    # This process uses the TTS Python ABI. No Torch/model import is needed.
    if str(packages_dir) not in sys.path:
        sys.path.insert(0, str(packages_dir))
    import tiktoken

    ranks = {}
    with vocab_path.open("rb") as source:
        for raw in source:
            if raw.strip():
                token, rank = raw.split()
                ranks[base64.b64decode(token)] = int(rank)
    return tiktoken.Encoding(name=vocab_path.stem, pat_str=TOKEN_PATTERN,
                             mergeable_ranks=ranks, special_tokens={})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vocab", required=True)
    parser.add_argument("--packages", required=True)
    args = parser.parse_args()
    encoding = load_encoding(Path(args.vocab), Path(args.packages))
    text = sys.stdin.buffer.read().decode("utf-8")
    print(len(encoding.encode(text, disallowed_special=())))


if __name__ == "__main__":
    main()
