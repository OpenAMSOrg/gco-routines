# Receiving the complete gco-routines repository

This file addresses a handoff that received documents but did not see the working
source tree. Attach and extract the **whole repository**, not just CODEX_HANDOFF.md.
The separately linked Markdown documents do not contain the source files.

## Preferred: extract the complete ZIP

The replacement ZIP has **no enclosing directory**: AGENTS.md, docs/, source,
tests/, and .git/ are at its root. Extract into a new empty directory:

```bash
mkdir gco-routines
unzip /path/to/gco-routines-codex-work-complete.zip -d gco-routines
cd gco-routines
python tools/verify_handoff.py --require-git
git status --short
```

Do not copy with `cp *` or another operation that omits dotfiles. This delivery
includes a real .git/ directory; preserve it.

## Alternative: clone the Git bundle

A Git bundle is supplied separately for workspaces that strip archive dotfiles.
It contains the complete local handoff commit and all tracked source files:

```bash
git clone /path/to/gco-routines-codex-work.bundle gco-routines
cd gco-routines
python tools/verify_handoff.py --require-git
git status --short
```

Choose either the ZIP or the bundle. They contain the same tracked tree.
This is a NEW handoff baseline repository, not historical project Git metadata
and not the upstream Klipper repository. No upstream SHA has been invented.

## Run the baseline, then implement

Run this in a separate development environment, not a live printer environment:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
python -m pytest -q
python -m gcoroutines examples/toolchange.gcode --closed-world --json
python -m gcoroutines examples/results.jinja --template --json
python tools/demo_state.py
```

Existing environments with the declared dependencies can run the checks directly.
The checksum verifier uses only Python's standard library. Run it on intake;
intentional implementation changes will subsequently change the baseline hashes.

Read AGENTS.md and CODEX_HANDOFF.md and carry out the staged implementation.
The unchanged source is a frontend and semantic reference, NOT an installable
Klipper runtime. tools/fetch_klipper.py is present; fetching an upstream checkout
is still a separate task requiring network access.

## Missing-file recovery checklist

- Confirm you extracted the archive or cloned the bundle, not only opened a document.
- Work in the directory that contains AGENTS.md and pyproject.toml.
- Run tools/verify_handoff.py --require-git before reporting missing files.
- Preserve the diagnostic output if verification fails.
