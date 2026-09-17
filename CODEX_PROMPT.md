First extract the complete repository ZIP or clone its Git bundle as described
in RECEIVING.md. Work from the directory containing AGENTS.md and pyproject.toml.
Run `python tools/verify_handoff.py --require-git`. Do not proceed from the
standalone handoff document alone; the actual source tree is required.

Implement gco-routines from the attached package as a separately installed Klipper
extra without modifying tracked upstream source files. Read AGENTS.md and
CODEX_HANDOFF.md first. Preserve the exact START/END/WAIT syntax and behavior.

Begin by running the supplied reference tests, then fetch and pin a complete
Klipper checkout with tools/fetch_klipper.py. Implement the extra and integration
tests in the staged order documented in CODEX_HANDOFF.md. The included parser and
semantic model are a tested starting point, not a finished runtime. Do not stop at
another architecture report: build the adapter, ordered macro execution, results,
status and job lifecycle, and verify them with fake devices/real Klippy machinery.

Keep ordinary macros and G-code on their existing paths. No new author-facing
language features. No real printer or hardware actions. Report test commands,
upstream commit, files changed, remaining limitations, and demonstrate that tracked
upstream files were not changed. Read the validation boundaries carefully: CFG
syntax checking alone does not prove names, data, device completion or safety.
