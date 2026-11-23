#!/usr/bin/env python3
"""Fail when a tracked file names something this public repository must not name.

The names themselves are not written here: each is kept as the sha256 of its
lowercase form, and every word of every tracked text file is hashed and looked
up. To add a name, append `hashlib.sha256(b"<name>").hexdigest()`.

    python3 scripts/check_names.py
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

BANNED = {
    "ebb6df6b04f878331a941ace5d0fd2aeb95ee64b4e444b4f7407b37618a23eb7",
    "38733205ec6010a167aa2c9b08b0a6bc49cc80f630f6b7644c14a7ba06c26e0e",
    "7cc9c6e18b587acd33630d23f6a27bb66d132538d407056d3a00ef2c7834f563",
    "83859c53ec7fe6c5c26c6f48743af669c1711a5e03f208de48e62fe17f865b8c",
}

SKIP_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico", ".woff", ".woff2", ".lock")
SKIP_NAMES = {"package-lock.json"}

WORD = re.compile(r"[a-z0-9]+")


def tracked() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout
    return [ROOT / p for p in out.split("\0") if p]


def main() -> int:
    hits: list[str] = []
    for path in tracked():
        if path.suffix in SKIP_SUFFIXES or path.name in SKIP_NAMES or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for word in set(WORD.findall(line.lower())):
                if hashlib.sha256(word.encode()).hexdigest() in BANNED:
                    hits.append(f"{path.relative_to(ROOT)}:{number}")
    for hit in sorted(set(hits)):
        print(f"{hit}: a name this repository does not carry")
    if hits:
        return 1
    print("names: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
