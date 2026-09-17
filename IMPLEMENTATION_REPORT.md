# gco-routines implementation report

Date: 2026-09-17

## Outcome

The working implementation is a self-contained Klipper extra in
`klippy_extra/gco_routines`. It registers `START`, `END`, and `WAIT` and uses
narrow runtime wrappers; no tracked Klipper file is patched.

The target Pi was inspected read-only. Nothing was installed, no service was
restarted, and no printer command was sent.

## Baselines

| Baseline | Commit | Purpose |
|---|---|---|
| Target `pi@192.168.1.12:~/klipper` | `c0c7ef2a5a82f1b60c207fb02274c6536bb952cb` | Primary behavioral integration checkout |
| Official Klipper upstream | `ad425fc22e01ca05db4852a81dfa9dab17373ff8` | Compatibility and regression checkout |

The target reports Python 3.9.2, Jinja 3.1.6, and greenlet 2.0.2. Local real
reactor tests ran with Python 3.12.3, Jinja 3.1.6, and greenlet 3.3.2. Every
plugin module also parses with Python 3.9 grammar. The exact target environment
has not executed the plugin because project safety rules prohibit printer-side
installation during development.

A local trial merge of current upstream into the target branch conflicted in
`klippy/msgproto.py`, where the target carries CAN-related changes. The merge
was not applied or deployed. Evidence is recorded in
`evidence/target-environment.json`.

Both checkouts match the commits and selected SHA-256 hashes in:

- `evidence/klipper-target-pin.json`
- `evidence/klipper-upstream-pin.json`

`git diff --exit-code` passes in each checkout.

## Plugin modules

```text
klippy_extra/gco_routines/
├── __init__.py      manager, commands, run context, driver API ownership
├── driver_api.py    structured reply and observable-detail API
├── integration.py   fail-closed Klipper compatibility and lifecycle wrappers
├── program.py       strict raw-source parser and source-isolated block collector
├── runtime.py       cooperative run/routine/wait graph and immutable results
└── templates.py     incremental ordered Jinja compiler and live bindings
```

The discarded prototype `lock.py` and `ordered_macro.py` implementations were
removed so there is one lock adapter and one macro compiler.

## Integration design

The adapter validates the live `GCodeDispatch` signature and complete source
hash against the two pinned baselines before enabling itself. It then wraps:

- `gcode._process_commands`, `run_script`, and `run_script_from_command` for
  source-aware, line-ordered dispatch and generated-control rejection;
- Klipper's existing reactor mutex with a depth-counted, run-aware admission
  layer that preserves unrelated request serialization;
- `gcode_macro.load_template` and `GCodeMacro.cmd` before macro construction,
  preserving legacy macro rendering while managed macros execute incrementally;
- `virtual_sdcard._load_file`, so both `M23` and `SDCARD_PRINT_FILE` preflight
  and share EOF joining;
- pause/resume/cancel/reset/shutdown/disconnect lifecycle seams.

No OS thread executes G-code. Children run only through Klipper reactor
callbacks and completions. Greenlet bindings are removed in `finally` blocks so
reactor greenlet reuse cannot inherit a terminated routine identity.

## Verified behavior

The suite covers:

- multiline API and line-at-a-time virtual-SD ordering;
- cooperative default/child overlap and child-to-child waits;
- retained results, multiple waiters, cycles, failure, cancellation, and name
  reuse;
- external request serialization while managed children are admitted;
- `M23` and `SDCARD_PRINT_FILE` source preflight plus implicit EOF join;
- ordered Jinja scopes, loop variables, START-time local snapshots, live
  `reply`, `waited`, and status boundaries;
- real macro source capture, handler registration, and per-routine legacy helper
  reentrancy;
- StrictUndefined behavior with explicit `default` support;
- generated/transport control rejection and pseudo-TTY policy;
- pause admission, shutdown/reset cancellation, collision checks, status schema,
  resource bounds, installer round-trip, and immutable Klipper pins;
- a fresh-process behavioral smoke test against the current upstream checkout.

Verification command:

```bash
.venv/bin/pytest -q
```

Final recorded result: **212 passed**.

The local installer was also run against the target checkout clone, the package
was imported through `extras.gco_routines`, and the symlink was removed again;
tracked Klipper state remained clean.

## Operational limits

- Physical devices were not actuated. Real-printer validation remains a
  deployment step.
- The plugin coordinates host command execution; it does not create independent
  motion planners or hardware ownership. Authors must synchronize shared
  hardware explicitly.
- Buffered motion may outlive command acceptance. Use the relevant existing
  Klipper completion barrier before `END` when physical completion is required.
- Managed macro sections must load after `[gco_routines]`; the plugin fails
  closed when it cannot retain their original source.
- Compatibility is intentionally pinned. A future Klipper `gcode.py` hash is
  rejected until its private seams are reviewed and added deliberately.
