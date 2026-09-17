#!/usr/bin/env python3
"""Install or uninstall gco-routines as one Klipper extras symlink.

The installer never edits a tracked Klipper file and refuses to replace an
existing path.  Keep this repository available while Klipper is running; the
installed path is a symlink back to ``klippy_extra/gco_routines``.
"""

import argparse
from pathlib import Path
import subprocess


def tracked_diff(checkout):
    result = subprocess.run(
        ["git", "diff", "--quiet", "--ignore-submodules", "--"],
        cwd=str(checkout),
    )
    if result.returncode not in (0, 1):
        raise RuntimeError("unable to inspect Klipper tracked-file state")
    return result.returncode


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--klipper", required=True, type=Path,
                        help="Klipper checkout containing klippy/extras")
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args(argv)

    checkout = args.klipper.expanduser().resolve()
    extras = checkout / "klippy" / "extras"
    if not (extras / "gcode_macro.py").is_file():
        parser.error("--klipper does not look like a complete Klipper checkout")
    source = (Path(__file__).resolve().parents[1]
              / "klippy_extra" / "gco_routines")
    if not (source / "__init__.py").is_file():
        parser.error("gco-routines extra package is incomplete")
    destination = extras / "gco_routines"
    before = tracked_diff(checkout)

    if args.uninstall:
        if not destination.is_symlink():
            parser.error("refusing to remove a path that is not our symlink")
        if destination.resolve() != source:
            parser.error("refusing to remove a symlink with a different target")
        destination.unlink()
        action = "Removed"
    else:
        if destination.exists() or destination.is_symlink():
            if destination.is_symlink() and destination.resolve() == source:
                print("Already installed: %s -> %s" % (destination, source))
                return 0
            parser.error("destination already exists; refusing to overwrite it")
        destination.symlink_to(source, target_is_directory=True)
        action = "Installed"

    after = tracked_diff(checkout)
    if before != after:
        raise RuntimeError("tracked Klipper state changed unexpectedly")
    print("%s: %s%s" % (
        action, destination,
        " -> %s" % source if not args.uninstall else "",
    ))
    print("Tracked Klipper files were not modified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
