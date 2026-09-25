# Printer acceptance tests

These test the installed concurrent API on actual Klippy, independently of the
filament motors. The public controls remain **START / END / WAIT**. The optional
`_GCO_TEST_*` commands are diagnostic fixtures, not additions to that API.

Nothing in this directory has been installed or run on the printer by creating
this suite. First validate locally, then install during an idle maintenance
window. **Every filament test uses only bay 1 / T1.** Do not substitute other bays.

## 1. Install the optional diagnostics

The production extra must already support the explicit `render_mode` macro
property. Managed fixtures declare `render_mode: ordered`; the shared legacy
helper deliberately does not. Upgrade the extra before installing these configs.
Copy this directory to
`/home/pi/gco-routines/printer_tests` without replacing an existing test copy
blindly. On the Pi, add only this new extras symlink:

```sh
ln -s /home/pi/gco-routines/printer_tests/gco_routines_test.py /home/pi/klipper/klippy/extras/gco_routines_test.py
```

After `[gco_routines]` in the printer configuration, include:

```ini
[include /home/pi/gco-routines/printer_tests/macros.cfg]
# Optional, only for the later supervised stages:
[include /home/pi/gco-routines/printer_tests/hardware.cfg]
```

Restart Klipper only when no job or filament operation is active. No macro
executes at startup. No tracked Klipper file or production macro is modified.
Do not install these fixtures on stock Klipper without the extension: they
deliberately test its semantics and do not offer a serial fallback.

Uninstall by removing the two includes and this exact diagnostic symlink, then
restarting while idle. Keep reports for comparison. Do not remove the production
`gco_routines` extra or its include.

## 2. Automated no-motion API suite

Run from the project directory on the workstation with Python 3.9 or later;
the runner uses only the standard library. If Moonraker requires authentication,
set `MOONRAKER_API_KEY` in the environment; do not put credentials in the URL.

```sh
python3 tools/run_printer_tests.py list
python3 tools/run_printer_tests.py preflight --url http://192.168.1.12:7125
python3 tools/run_printer_tests.py run --url http://192.168.1.12:7125 --report outputs/printer-api-first.json
```

`list` is the default and makes no network calls. `preflight` only reads state.
`run` sends the diagnostic cases; it refuses a missing/incompatible extension,
missing macros, active/paused printing, moving toolhead, active child, or heater
target above zero. All heaters must also be below 50 C. No homing is needed.
Leave the console alone while it runs. Existing report files are never replaced.

To repeat a single test, use a fresh report filename:

```sh
python3 tools/run_printer_tests.py run --url http://192.168.1.12:7125 --case overlap --report outputs/printer-api-overlap-repeat.json
```

The 32 cases cover:

| Area | Cases / observations |
| --- | --- |
| Execution | Sequential default, named/anonymous children, actual overlapping probe intervals, caller-only suspension |
| Dependencies | All-of ordering, child-to-child WAIT, multiple waiters, completed-before-WAIT, repeated WAIT |
| Bare WAIT | START ordering, per-caller collection, self/default exclusion, snapshot excluding later starts |
| Names | Case sensitivity, collection before reuse, separate instance identities, reserved/unknown/duplicate targets |
| Templates | Ordered reply/result reads, private captured locals, params, branches, loops, live printer status after WAIT |
| Helpers | Reentrant legacy helper, no leaked last-subcommand reply, no indirect nested spawn |
| Failures | Complete-source preflight, malformed/unclosed/nested controls, cycles, self-wait, generated controls, undefined fields |
| Failure propagation | Failed caller blocks queued child; failed child blocks caller and another waiting child |
| API/observability | Implicit API end join, unrelated HTTP command serialization, status schema/revisions, driver detail, retained outcomes |

Expected-error cases **must** return the specific expected error and must not
execute their `FORBIDDEN` sentinel. A generic HTTP failure is not a pass. The
runner stops on the first unexpected result or connection problem and does not
retry, restart, clear errors, pause, cancel, or change heater targets. A client
timeout does not prove server execution stopped: inspect before rerunning.

The JSON report retains initial state, per-case snapshots, structured probe
events, expected errors, and the first failure. It is not based on console-text
scraping. Events record command-handler begin/end ordering, not MCU movement.
Already admitted native waits may finish after a run faults; the relevant
assertion is that subsequent commands are not admitted. No wall-clock speedup
threshold is used as a substitute for observed overlap.

## 3. Virtual-SD/file boundaries

These are separate supervised checks: your virtual-SD error cleanup can move
hardware or change heaters even though the fixture contains no such commands.
Inspect `virtual_sdcard.on_error_gcode` first. Upload the four `.gcode` files
under `files/` to a dedicated test folder using the normal printer UI. Do not
start a production file, and do not paste these files line-by-line as separate
HTTP requests—complete API programs and virtual-SD streams have different scope.

Before each file, wait for `gco_routines_test.active=0`, then issue
`_GCO_TEST_RESET`. Observe `gco_routines`, `gco_routines_test`, `print_stats`, and
`virtual_sdcard` through Moonraker object queries/subscriptions. A filename ending
in `rejected_...` is deliberately invalid, not a broken installation.

| File | Required result |
| --- | --- |
| `eof_join.gcode` | `sd_foreground` finishes before `sd_tail`; print must NOT complete until `sd_tail:end`; then all routines complete with no fault |
| `child_failure.gcode` | Expected diagnostic failure; neither FORBIDDEN sentinel executes; no successful print completion; error/cancel state depends on configured cleanup |
| `rejected_unclosed.gcode` | Rejected before any probe executes; must not begin a print or execute its prefix |
| `lifecycle.gcode` without intervention | Both tail markers appear only after `lifecycle_pending:end`, and file completes |

Run the valid EOF file once via the normal `SDCARD_PRINT_FILE` path, then once
via `M23 <uploaded-relative-filename>` followed by `M24`. Confirm both ingress
paths honor the same final join. Save observations separately from the automatic
HTTP suite; it does not claim to have run these files.

## 4. Pause, cancel, reset, emergency stop, reconnect

Use a **fresh `lifecycle.gcode` run for each row**. It provides a ten-second
pending child. Act only once status shows `lifecycle_pending` active and the
default routine waiting. Run no other test concurrently.

These controls invoke the printer's existing macros. On this printer PAUSE can
park/retract even when already paused. Review those macros first, clear the bed,
home if their park motion requires it, and ensure the filament path is empty
before cold lifecycle tests. Never use these tests during feeding or a real print.

| Action while pending | Required observation |
| --- | --- |
| Pause | No new routine may start; a new spawning macro must reject while paused. An already admitted probe command may finish; the child then suspends at its next own command boundary (status `waiting`, detail `suspended: paused`) instead of faulting the run |
| Resume after pause | Follow the existing recovery policy. After RESUME/CLEAR_PAUSE the suspended child continues and the old run can complete; a **new** `GCO_TEST_RESULTS` invocation can run. After CANCEL_PRINT the suspended child ends cancelled and no stale old tail may revive |
| Cancel through printer UI/API | Waiting caller unwinds; child/parent tail markers absent; no false file completion. Wait past the original ten seconds and check again |
| Reset virtual SD / cancel-and-new-file | Old instance IDs/tails never become the new file's work; run the EOF fixture again |
| RESTART / FIRMWARE_RESTART | Intentionally disrupts Klipper. After ready, no old run survives; rehome before motion and rerun no-motion preflight |
| Emergency-stop button/API | Shutdown is independent of the pending WAIT; no later tail. Recover deliberately, never via automatic runner retries |
| Disconnect/reconnect status client only | Execution continues; resubscribe and get current state plus retained completed results; do not expect every intermediate revision |

For a Moonraker WebSocket client, the subscription request is:

```json
{"jsonrpc":"2.0","method":"printer.objects.subscribe","params":{"objects":{"gco_routines":null,"gco_routines_test":null,"print_stats":null}},"id":1}
```

Disconnect the observer, not Klipper or the MCU. After reconnecting, subscribe
again. Check stable routine IDs within the run, nondecreasing revisions, and
completed results. Snapshots may coalesce transitions; they are not a journal.
For error/lifecycle checks, a missing new tail marker alone is insufficient if
the intended child never started: preserve the preceding active-state snapshot.

## 5. Supervised native-device checks

These require the optional `hardware.cfg`. None is called by the automated
runner. They refuse an active print and require `CONFIRM=1`; inspect physical
clearance yourself. Do not put these macros inside another START block.

| Command | Preparation and pass condition |
| --- | --- |
| `GCO_TEST_MOTION CONFIRM=1` | XYZ homed, Z at least 10 mm, clear X path. Moves +10/-10 mm at 20 mm/s, no extrusion. Observe nonzero `motion_report.live_velocity` while `motion_peer` is active. `motion_done` follows M400 and precedes peer completion; position/mode restored |
| `GCO_TEST_HEATER CONFIRM=1` | No filament operation, nozzle below 40 C and target 0. Heats only to 50 C. `heater_peer` executes while M109 waits; `heater_done` precedes PASS; target returns to 0 |
| `GCO_TEST_FILAMENT CONFIRM=1` | **Bay 1 / T1 only**, first-stage motor functional, spool engaged, no other bay loaded. Prepare normal material-safe temperature and homing. Runs T1 → SAFE_UNLOAD_FILAMENT → T1. Confirm load/clean overlap, final feed, clean hub release on unload, no pause/fault, final group T1 |

The T1 fixture rejects any other initially loaded group before transport, so it
cannot silently unload another bay. If initially T1 is loaded, the first T1 is
the expected no-op. Call T1 once more after success to verify the same-tool
no-op separately. A final `SAFE_UNLOAD_FILAMENT` is optional supervised cleanup
**only while the loaded group is T1**, followed by heater-off when finished.
The test leaves T1 loaded and its test temperature unchanged on success.

M400 is a real planner completion barrier. Two children both using G1 do not
obtain separate motion planners; that is not the API contract. The heater test
uses M109 rather than a simulated wait, while the probe supplies independent
foreground work. Do not infer a successful physical load solely from the API
return; check motor movement, sensors and actual filament behavior.

If a hardware test errors, stop and inspect; later cleanup lines may not execute.
Use the normal heater-off/emergency controls as needed. After an interrupted
motion test, `RESTORE_GCODE_STATE NAME=gco_test_motion MOVE=0` restores modes
only if that test reached SAVE_GCODE_STATE. It does not move or rehome the printer.
Never use error clearing or RESUME as proof that filament is physically clear.

## Coverage limits and acceptance record

| Contract area | Where verified |
| --- | --- |
| Runtime, templates, replies/results, waits, failures, complete API ingress | Automated printer suite plus real-Klippy CI of these exact fixtures |
| Virtual SD EOF and malformed file admission | Shipped file fixtures + existing real virtual-SD CI |
| Pause/cancel/reset/shutdown + reconnect/subscription | Supervised checklist above + existing lifecycle CI; not counted as automatic printer passes |
| Motion/planner, native heater wait, actual filament transport | Optional supervised hardware tests; filament restricted to T1 |
| Startup collisions, incompatible adapter rejection, invalid macro config, resource exhaustion, unsafe types, stock fallback, pseudo-TTY rejection | Existing isolated tests only; deliberately not fault-injected into the working printer |

Record date, Klipper revision, plugin/macro hashes, test name, PASS/FAIL/NOT RUN,
observed routine IDs/results, any physical observations, and final heater/pause/
loaded-group state. “Full API verified” requires completing the relevant manual
rows too; a green automatic report is not four-lane or endurance validation.

Local verification (never opens a printer connection):

```sh
.venv/bin/python -m pytest -q tests/integration/test_printer_acceptance.py tests/test_printer_test_runner.py
.venv/bin/python -m pytest -q
```
