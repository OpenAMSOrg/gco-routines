# gco-routines implementation report

Date: 2026-09-17

## Outcome

The working implementation is a self-contained Klipper extra in
`klippy_extra/gco_routines`. It registers `START`, `END`, and `WAIT` and uses
narrow runtime wrappers; no tracked Klipper file is patched.

After initial read-only development, the user explicitly authorized deployment.
The extra and single-FPS `oams_macros.cfg` are now installed on the target Pi;
the review corrections were deployed with backups and a Klipper service restart.
No motion, heater, or OAMS transport commands were requested during review.
The OAMS CAN MCU later returned, Klipper reached ready, and no-motion live
scheduler and rejection checks passed. Physical workflow validation remains
outstanding.

## Baselines

| Baseline | Commit | Purpose |
|---|---|---|
| Target `pi@192.168.1.12:~/klipper` | `c0c7ef2a5a82f1b60c207fb02274c6536bb952cb` | Primary behavioral integration checkout |
| Official Klipper upstream | `ad425fc22e01ca05db4852a81dfa9dab17373ff8` | Compatibility and regression checkout |

The target reports Python 3.9.2, Jinja 3.1.6, and greenlet 2.0.2. Local real
reactor tests ran with Python 3.12.3, Jinja 3.1.6, and greenlet 3.3.2. Every
plugin module also parses with Python 3.9 grammar. `tools/smoke_klipper.py` now
also passes in the Pi's exact environment, running real Klippy and the actual
macro source against inert handlers without opening the printer configuration,
MCUs, or printer API. No target dependencies were installed or upgraded.

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

Final recorded result after live follow-up: **243 passed in 2.51s**.

Review regressions additionally cover live macro status refresh, stop-on-failure
inside legacy helpers, cancelled queued template actions, complete API preflight
and serialization, bounded run history, per-block collection limits, helper reply
ownership, private child namespaces, Jinja `with`, and virtual-SD failure cleanup
without a false completion/pause outcome. Actual OAMS macro tests cover repeated
toolchanges, load/clean overlap, standalone unload, sensor and driver failures,
invalid groups, cold extrusion prevention, and nested-toolchange rejection.

The Pi smoke passed repeated toolchanges, load/clean overlap, and a deliberate
sensor abort on Python 3.9.2. Its expected abort logs a Klipper command-error
traceback; the harness verifies no later unload/load commands and exits zero.
See [review evidence and deployed hashes](evidence/review-2026-09-17.md).

The local installer was also run against the target checkout clone, the package
was imported through `extras.gco_routines`, and the symlink was removed again;
tracked Klipper state remained clean.

## Operational limits

- Physical workflows were not exercised. `oams_mcu1` (CAN UUID
  `66e4a3d0cd57`) is connected and producing telemetry, but RFID reader B fails
  its SPI reset and the proprietary firmware's `tx_retries` counter increases
  rapidly despite zero RX/TX errors. Supervised physical validation remains
  outstanding.
- The plugin coordinates host command execution; it does not create independent
  motion planners or hardware ownership. Authors must synchronize shared
  hardware explicitly.
- Buffered motion may outlive command acceptance. Use the relevant existing
  Klipper completion barrier before `END` when physical completion is required.
- Managed macro sections must load after `[gco_routines]`; the plugin fails
  closed when it cannot retain their original source.
- Managed Jinja statements must be on separate physical lines from output;
  output-producing filter/call blocks are rejected instead of silently dropping
  or fragmenting commands. Legacy Jinja behavior remains unchanged.
- The supplied OpenAMS `T0`–`T3` macros spawn their own loading routine and must
  run in the default routine, not inside an outer `START` block.
- Routine records remain bounded to 1024 per run. Up to 64 recent runs are
  retained, with only terminal, unbound runs eligible for eviction. Physical
  device operations already accepted are subject to the driver's cancellation
  behavior, not forcibly interrupted by the software scheduler.
- Compatibility is intentionally pinned. A future Klipper `gcode.py` hash is
  rejected until its private seams are reviewed and added deliberately.
