#!/usr/bin/env python3
"""Record a reproducible manifest for an existing Klipper checkout.

This is used when the compatibility baseline is a deployment-specific Git
history that cannot be fetched from the public Klipper remote.  It is read-only
with respect to the checkout and refuses dirty tracked files or an existing
manifest.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess


FILES = [
    "klippy/gcode.py",
    "klippy/reactor.py",
    "klippy/klippy.py",
    "klippy/extras/gcode_macro.py",
    "klippy/extras/virtual_sdcard.py",
    "klippy/extras/pause_resume.py",
    "klippy/webhooks.py",
    "scripts/klippy-requirements.txt",
    "COPYING",
]


def git(checkout, *args):
    return subprocess.run(
        ["git", *args], cwd=str(checkout), check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.strip()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--repository", required=True,
                        help="Provenance URL or deployment checkout identifier")
    parser.add_argument("--requested-ref", default="HEAD")
    args = parser.parse_args(argv)

    checkout = args.checkout.resolve()
    manifest = args.manifest.resolve()
    if not (checkout / ".git").exists():
        parser.error("checkout is not a Git worktree")
    if manifest.exists():
        parser.error("manifest already exists; refusing to overwrite it")
    dirty = git(checkout, "status", "--porcelain", "--untracked-files=no")
    if dirty:
        parser.error("checkout has tracked changes")
    missing = [name for name in FILES if not (checkout / name).is_file()]
    if missing:
        parser.error("checkout is missing: %s" % ", ".join(missing))

    commit = git(checkout, "rev-parse", "HEAD")
    hashes = {
        name: hashlib.sha256((checkout / name).read_bytes()).hexdigest()
        for name in FILES
    }
    record = {
        "repository": args.repository,
        "requested_ref": args.requested_ref,
        "commit": commit,
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "tracked_worktree_checked_out": True,
        "selected_file_sha256": hashes,
        "validation": (
            "Recorded and hashed an existing clean tracked checkout only; "
            "compatibility tests are recorded separately."
        ),
    }
    manifest.parent.mkdir(parents=True, exist_ok=True)
    with manifest.open("x", encoding="utf-8") as output:
        json.dump(record, output, indent=2)
        output.write("\n")
    print("Recorded %s in %s" % (commit, manifest))


if __name__ == "__main__":
    main()
