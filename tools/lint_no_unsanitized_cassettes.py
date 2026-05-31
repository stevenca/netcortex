#!/usr/bin/env python3
"""Refuse to merge a PR that contains unsanitized cassette content.

This is the CI gate that pairs with ``tools/sanitize_cassette.py``. It walks
``tests/cassettes/`` and runs the sanitizer in ``--verify`` mode on each
``*.yaml`` / ``*.yml`` cassette. Any file that would still be mutated by the
sanitizer is treated as a violation.

Files matching the ignore patterns from .gitignore (``*.unsanitized.*``,
``*.raw.*``, ``_scratch/``) are skipped — those are intentionally never
committed but if one slips in we flag it too.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CASSETTE_DIR = REPO_ROOT / "tests" / "cassettes"
SCRATCH_MARKERS = (".unsanitized.", ".raw.", "/_scratch/")


def main() -> int:
    if not CASSETTE_DIR.exists():
        print("lint_no_unsanitized_cassettes: no cassettes directory — ok")
        return 0

    cassettes: list[Path] = []
    forbidden: list[Path] = []
    for path in CASSETTE_DIR.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        if any(m in rel for m in SCRATCH_MARKERS):
            forbidden.append(path)
            continue
        if path.suffix.lower() in {".yaml", ".yml", ".json"}:
            cassettes.append(path)

    if forbidden:
        sys.stderr.write(
            "error: scratch / unsanitized cassettes must not be committed:\n"
        )
        for f in forbidden:
            sys.stderr.write(f"  {f.relative_to(REPO_ROOT)}\n")
        return 1

    if not cassettes:
        print("lint_no_unsanitized_cassettes: no cassettes to verify — ok")
        return 0

    cmd = [sys.executable, str(REPO_ROOT / "tools" / "sanitize_cassette.py"), "--verify"]
    cmd.extend(str(p) for p in cassettes)
    result = subprocess.run(cmd, check=False)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
