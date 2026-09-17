#!/usr/bin/env python3
"""Verify this baseline handoff before implementation. No third-party modules.

Run from any directory. This checks required files, hashes, and (optionally)
a complete clean Git checkout. Hash verification is expected to fail after
intentional implementation edits; this is an intake check, not an ongoing test.
"""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys

REQUIRED_FILES = (
    "AGENTS.md", "CODEX_HANDOFF.md", "CODEX_PROMPT.md", "README.md",
    "RECEIVING.md", "DELIVERY.md", "pyproject.toml",
    "docs/behavior-v0.1.md", "docs/language-v0.2.md",
    "gcoroutines/frontend.py", "gcoroutines/semantics.py",
    "gcoroutines/__main__.py", "grammar/gco-routines.ebnf",
    "grammar/gco-routines.lark", "grammar/control-lines.lark",
    "grammar/mixed-source.ebnf", "tools/fetch_klipper.py",
    "tools/verify_handoff.py", "schemas/status.schema.json",
    "tests/test_frontend.py", "tests/test_semantics.py",
    "examples/toolchange.gcode",
)
REQUIRED_DIRS = ("docs", "gcoroutines", "grammar", "tests", "examples", "tools", "schemas")


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args],
        text=True, capture_output=True, check=True, timeout=30,
    ).stdout.strip()


def verify(root, require_git=False):
    root = root.resolve()
    errors = []
    for name in REQUIRED_FILES:
        path = root / name
        if not path.is_file() or path.stat().st_size == 0:
            errors.append("Missing or empty required file: " + name)
    for name in REQUIRED_DIRS:
        path = root / name
        if not path.is_dir() or not any(p.is_file() for p in path.rglob("*")):
            errors.append("Missing or empty required directory: " + name)
    manifest_path = root / "MANIFEST.sha256.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or not manifest:
            raise ValueError("manifest must be a nonempty path-to-SHA256 object")
    except (OSError, ValueError) as exc:
        return errors + ["Cannot load manifest: " + str(exc)], 0, None
    for name in REQUIRED_FILES:
        if name not in manifest:
            errors.append("Required file not covered by manifest: " + name)
    checked = 0
    for name, digest in manifest.items():
        if not isinstance(name, str):
            errors.append("Manifest path must be a string")
            continue
        path_parts = PurePosixPath(name)
        if (path_parts.is_absolute() or ".." in path_parts.parts
                or "\\" in name or ".git" in path_parts.parts):
            errors.append("Unsafe manifest path: " + repr(name))
            continue
        path = root / name
        try:
            path.resolve().relative_to(root)
        except ValueError:
            errors.append("Path escapes repository: " + name)
            continue
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            errors.append("Invalid SHA256 for: " + name)
            continue
        if not path.is_file():
            errors.append("Manifest file missing: " + name)
            continue
        if path.is_symlink():
            errors.append("Baseline unexpectedly contains symlink: " + name)
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            errors.append("SHA256 mismatch: " + name)
            continue
        checked += 1
    commit = None
    if require_git:
        if not (root / ".git").exists():
            errors.append("Git metadata absent: use the complete archive or clone the bundle")
        elif not shutil.which("git"):
            errors.append("git executable not available")
        else:
            try:
                top = Path(git(root, "rev-parse", "--show-toplevel")).resolve()
                if top != root:
                    errors.append("Git root is not this handoff directory")
                if git(root, "rev-parse", "--is-shallow-repository") != "false":
                    errors.append("Handoff repository must not be shallow")
                git(root, "fsck", "--full", "--no-reflogs")
                commit = git(root, "rev-parse", "--verify", "HEAD")
                tracked = set(git(root, "ls-files").splitlines())
                missing = sorted((set(manifest) | {"MANIFEST.sha256.json"}) - tracked)
                if missing:
                    errors.append("Payload paths not tracked by Git: " + ", ".join(missing))
                if git(root, "status", "--porcelain", "--untracked-files=all"):
                    errors.append("Checkout is not clean; verify before implementation edits")
            except (OSError, subprocess.SubprocessError) as exc:
                detail = getattr(exc, "stderr", None) or str(exc)
                errors.append("Git verification failed: " + detail.strip())
    return errors, checked, commit


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--require-git", action="store_true")
    args = parser.parse_args(argv)
    errors, checked, commit = verify(args.root, args.require_git)
    if errors:
        for error in errors:
            print("FAIL: " + error, file=sys.stderr)
        return 1
    print("PASS: required handoff paths are present")
    print("PASS: {} payload SHA256 hashes match".format(checked))
    if commit:
        print("PASS: complete clean Git repository; HEAD=" + commit)
    print("This verifies the reference handoff, not an implemented Klipper extra.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
