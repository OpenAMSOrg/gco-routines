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

Final recorded result after correcting live capability detection: **248 passed
in 4.09s**.

Review regressions additionally cover live macro status refresh, stop-on-failure
inside legacy helpers, cancelled queued template actions, complete API preflight
and serialization, bounded run history, per-block collection limits, helper reply
ownership, private child namespaces, Jinja `with`, and virtual-SD failure cleanup
without a false completion/pause outcome. Actual OAMS macro tests cover repeated
toolchanges, load/clean overlap, standalone unload, sensor and driver failures,
invalid groups, cold extrusion prevention, and nested-toolchange rejection.
They also run the same OAMS macro file against a real stock Klippy harness with
no gco-routines manager or reserved commands registered, proving that it emits a
serial load/clean sequence and rechecks a failed load before permitting extrusion.
The OAMS fixture now uses Klipper's actual configuration status builder: an empty
`[gco_routines]` section is absent from `settings`, but the live plugin object
must still select concurrency. A regression reproduced the original one-routine
serial run and now verifies two routines and overlap. A separate test models a
non-yielding cleaning macro followed by a yielding motion-completion barrier.

The Pi smoke passed repeated toolchanges, load/clean overlap, and a deliberate
sensor abort on Python 3.9.2. Its expected abort logs a Klipper command-error
traceback; the harness verifies no later unload/load commands and exits zero.
See [review evidence and deployed hashes](evidence/review-2026-09-17.md).

The local installer was also run against the target checkout clone, the package
was imported through `extras.gco_routines`, and the symlink was removed again;
tracked Klipper state remained clean.

### Staged printer acceptance suite

`printer_tests/` now supplies an optional hardware-free diagnostic extra,
32 API acceptance cases, structured JSON reporting through
`tools/run_printer_tests.py`, four virtual-SD fixtures, and a supervised lifecycle
checklist. Separate hardware macros cover motion/M400, native M109, and a
**bay 1 / T1 only** load-unload-load sequence; another loaded bay is rejected.
None of these fixtures was installed or run on the physical printer in this
preparation step. Production extra code and upstream Klipper were not changed.

The exact macro/case sources and file fixtures passed in the pinned real-Klippy
environment with inert hardware. Full local result: **315 passed in 34.09s**.
This includes client preflight/report tests, T1 guard/abort tests, expected-error
recovery without restarting, and actual virtual-SD worker execution. Details and
limits are in `evidence/printer-acceptance-2026-09-18.md`.

### Explicit per-macro render mode (initial local verification)

At the user's request, `render_mode: ordered` is now a real macro configuration
property, not a macro variable. Omitted or `legacy` preserves whole-template
rendering; control text and caller mode no longer select incremental execution.
The ordered compiler also accepts macros without START/END/WAIT. Both modes
retain invocation-time ordinary variables; fresh `printer` reads see prior
commands' effects only in ordered mode. The property is not changeable with
SET_GCODE_VARIABLE. Invalid values fail configuration.

The extra alone consumes the property. No tracked Klipper file changed.
Stock Klipper rejects unknown macro properties, so the portable OAMS base
config remains property-free. `config/oams_macros_ordered.cfg` explicitly opts
only `_TX` in; its absence selects serial fallback even with the plugin loaded.
The plugin, base macro update and ordered overlay must be deployed together for
the concurrent workflow. The diagnostic managed macros now declare their mode.

New real-Klippy regressions cover status/action timing versus stock behavior,
both directions of mixed-mode calls, background helpers, ordinary scopes,
configuration option consumption, invalid modes, immutable mode selection,
legacy control rejection, control-free error propagation and ordered restrictions.
The standalone compiler fallback is also tested without the reference frontend.
Full local result: **347 passed in 35.09s** (`.venv/bin/pytest -q`); the focused
render-mode integration file also passed alone (**30 passed in 0.82s**).
Both pinned checkouts passed the updated
inert-device OAMS smoke, including overlap and the expected sensor abort. These
results do not establish physical-printer or Pi Python 3.9 acceptance for this
revision; no printer commands, installation or restart were performed.
All six plugin modules also parsed using Python 3.9's syntax rules; both vendor
checkouts have no tracked diff, and `git diff --check` passed.

Local smoke commands (both exited zero; expected sensor-abort logs are checked):

```bash
.venv/bin/python tools/smoke_klipper.py --klipper vendor/klipper --extra-parent klippy_extra --macros config/oams_macros.cfg
.venv/bin/python tools/smoke_klipper.py --klipper vendor/klipper-upstream --extra-parent klippy_extra --macros config/oams_macros.cfg
```

### Authorized render-mode deployment

After the user authorized deployment and restart, the same sources passed the
inert smoke in the Pi's actual Python 3.9.2 environment, then were installed with
backups and a Klipper service restart. Live configuration confirms `_TX` ordered
and the other 56 macros legacy. Klipper is ready; a no-motion START/G4/END/WAIT
probe completed both routines without fault. T1 remains selected and heater
targets are zero. No physical workflow or full acceptance suite was run.

No tracked Klipper files changed. The pre-existing RFID-B reset warning remains.
Hashes, checks and rollback are recorded in
[deployment evidence](evidence/render-mode-deployment-2026-09-18.md).

## Operational limits

- After the FPS hardware fix, the initial T2 load, T2-to-T3 and T3-to-T2
  toolchanges, same-tool no-op, and standalone unload completed on the printer.
  Live telemetry confirms load/clean overlap, subsequent extrusion, and no
  pauses or routine faults. T0 subsequently became available and its initial
  concurrent load passed. Its next unload returned firmware busy after T0
  sensor-event chatter; the macro correctly blocked the T2 reload and paused.
  The heater was turned off with T0 still in the path. Firmware mislabels hub
  events as inlet events, so the trace does not isolate the affected sensor.
  A user-authorized unload retry retracted to encoder 15 before the low-speed
  monitor stopped it; the hub subsequently cleared and a state refresh reports
  group/spool null, heater off, and pause retained. This was recovery, not a
  clean firmware unload-success response.
  T1, a complete T0 roundtrip, and long-print endurance remain outstanding.
  Previously observed RFID reader B reset errors and the proprietary firmware's
  increasing `tx_retries` counter are separate diagnostics; this test does not
  establish that they are resolved.
- The plugin coordinates host command execution; it does not create independent
  motion planners or hardware ownership. Authors must synchronize shared
  hardware explicitly.
- Buffered motion may outlive command acceptance. Use the relevant existing
  Klipper completion barrier before `END` when physical completion is required.
- Klipper's G-code mutex is released only when the last greenlet of the
  owning run leaves it, so unrelated requests never run while a routine
  command is in flight. The virtual-SD worker's between-line `test()` check
  therefore also waits for an in-flight child command: raw-file default lines
  overlap a child command only when they were already executing when it began
  (commands inside one ordered macro, such as the OAMS `_TX`, are unaffected).
- Pause rejects new routines and suspends each admitted child at its next own
  command boundary until RESUME/CLEAR_PAUSE (cancel/reset/shutdown cancel it).
  Commands already executing complete. Children are not suspended while a
  routine of the same run waits inside the G-code mutex, because Klipper could
  not accept RESUME until that wait ends.
- Known pre-existing limitation (reproduced unchanged on the previous
  revision): an external PAUSE accepted while the virtual-SD worker performs
  the implicit end-of-file join, or a PAUSE issued by a child during that
  join, blocks in Klipper's `do_pause()` waiting for the worker, which waits
  for the child. The API `pause_resume/cancel` endpoint, shutdown or `M112`
  recover; console `CANCEL_PRINT` cannot enter. End files with an explicit
  `WAIT` to avoid it until the join is made a mutex-holding SD command.
- `get_status()` reports a running virtual-SD/file print even while a
  transient API-script or ordered-macro run executes; after the print ends it
  stays the reported run until the next run starts.
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
