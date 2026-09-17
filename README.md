# Gco-routines — Codex Work handoff

**Draft 0.2 · 2026-09-17 · developer package, not an installable printer extra.**

The goal is a separately installed Klipper extra, with no edits to tracked upstream
files, that adds three native G-code instructions. The same small language frontend
should be usable by a slicer or editor without running Klipper.

```gcode
START NAME=filament_change
    T0
END

START NAME=nozzle_heating
    M109 S220
END

CLEAN_NOZZLE
WAIT ON=filament_change,nozzle_heating
```

No names are necessary when `WAIT` selects all outstanding background routines.
Any routine may wait on another. G-code owns synchronization; Jinja only supplies
ordinary expressions, `result`, `reply` and `waited`. Existing commands and their
physical completion semantics remain unchanged. Shared hardware remains shared.

## Receiving this corrected delivery

Read `RECEIVING.md` for complete ZIP extraction or Git-bundle cloning. Verify the
actual source tree with `python tools/verify_handoff.py --require-git` before
starting. The new Git history is a local handoff baseline, not upstream Klipper.

## Start here

Read `CODEX_HANDOFF.md`, then `AGENTS.md`. The former is the implementation task;
the latter records constraints that must survive later agent turns. `docs/behavior-v0.1.md`
is the original behavioral specification. `docs/language-v0.2.md` adds the formal
language and clarifies validation/deployment without expanding the command set.

```bash
# From this directory. These commands operate offline when dependencies exist.
python -m gcoroutines examples/toolchange.gcode --closed-world
python -m gcoroutines examples/results.jinja --template --json
python -m pytest -q
python tools/demo_state.py
```

For a fresh development environment:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
```

The plain frontend and semantic reference use the Python standard library.
Template syntax validation uses Jinja; grammar cross-check tests use Lark.
Installing these development tools does **not** install a Klipper extra.

## What is implemented here

| Item | Status |
|---|---|
| EBNF plus executable Lark grammar | Included; syntax is separate from semantic checks |
| Offline parser/validator with line/column diagnostics and JSON source AST | Implemented reference frontend |
| Jinja syntax and literal control-boundary checks before rendering | Implemented; never calls template helpers |
| Generated-control admission guard | Implemented reference function; not wired into Klipper |
| Run-local names, wait graph, retained immutable results, snapshots | Executable transition reference; not a scheduler |
| Passing tests and reproducible examples | See `evidence/verification.md` |
| Actual Klipper dispatcher/macro/virtual-SD adapters | **Not implemented; the Codex task** |
| Resumable ordered-Jinja compiler | **Not implemented; the Codex task** |
| Complete/pinned Klipper checkout or hardware validation | **Not obtained in this environment** |

The context-free addition is deliberately small. Ordinary G-code is an opaque host
language terminal, not a claim that this parser validates every existing command.
Because the initial blocks are flat, this sublanguage is even regular; a CFG is a
precise interchange specification, not a promise of arbitrary-program verification.

Syntax validation does not prove that a name exists in every branch, a device will
finish, or shared motion is safe. Those have distinct semantic/runtime checks.
Generated controls cannot bypass the source grammar. Jinja result types can be
checked when schemas exist; missing live fields are still runtime errors.

## Package map

- `grammar/`: raw and mixed-source EBNF; executable whole-program/control grammars.
- `gcoroutines/`: frontend, CLI, admission guard, transition reference.
- `tests/`, `examples/`, `schemas/`: conformance checks, commented programs, snapshot contract.
- `docs/`: language contract, behavior, implementation audit and acceptance gates.
- `tools/`: state demo and safe upstream fetch/pin utility.
- `evidence/`: actual test output, environment, prior probes, and failed fetch record.

There is intentionally no deceptive `load_config()` stub to install on a printer.
The prior audit excerpts are labeled transcriptions, not a downloaded checkout.
The upstream fetch helper records a real commit SHA when run in a network-enabled
workspace; no fabricated SHA is provided here.

## Handoff

Attach this directory/archive to the Codex workspace and use `CODEX_PROMPT.md` as
the task text. No Codex task was submitted from this conversation: direct Codex
Work submission was not exposed by the available connectors.
