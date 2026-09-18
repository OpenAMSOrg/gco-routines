# gco-routines for Klipper

Source repository: [OpenAMSOrg/gco-routines](https://github.com/OpenAMSOrg/gco-routines).

Original project code is [MIT licensed](LICENSE). Retained third-party audit
excerpts have their own licenses; see [NOTICE.md](NOTICE.md).

`gco-routines` is an extras-only Klipper extension that adds three literal
control instructions without changing tracked Klipper source or MCU firmware:

```gcode
START NAME=filament_change
    OAMSM_LOAD_FILAMENT GROUP=T0
END

CLEAN_NOZZLE
WAIT ON=filament_change
```

The default routine continues after `END`; the child runs cooperatively on
Klipper's reactor. `WAIT` suspends only its caller. A bare `WAIT` joins all of
that caller's outstanding children in `START` order.

## Current validation status

The implementation is validated locally against:

- the target Pi checkout at commit
  `c0c7ef2a5a82f1b60c207fb02274c6536bb952cb`;
- current upstream pin
  `ad425fc22e01ca05db4852a81dfa9dab17373ff8`;
- real `SelectReactor` integration tests running locally on
  Python 3.12/Jinja 3.1.6/greenlet 3.3.2: **347 tests passed**;
- the Pi's actual Python 3.9.2/Jinja 3.1.6/greenlet 2.0.2 environment,
  using real Klippy and the OAMS macro file with inert hardware handlers.

The explicit `render_mode` revision described below is now deployed after user
authorization. It passed the Pi's Python 3.9 inert-device smoke and a live
no-motion START/WAIT probe after restart; Klipper is ready with `_TX` ordered and
56 other macros legacy. See [deployment and rollback details](evidence/render-mode-deployment-2026-09-18.md).
The physical workflow results below refer to the earlier deployed revision;
the full staged printer acceptance suite has not yet been run on the printer.

The extra and single-FPS macros are installed on the Pi following explicit
deployment authorization. The OAMS MCU is now connected and Klipper is ready;
a live no-motion `START`/`WAIT` probe and the cold/unhomed toolchange rejection
both passed without changing printer or OAMS state. After the FPS hardware fix,
live testing completed initial T2 loading, T2-to-T3 and T3-to-T2 toolchanges,
same-tool no-op handling, and standalone unload. Telemetry confirms concurrent
loading/nozzle cleaning and subsequent toolhead extrusion without pauses or run
faults. Once T0 became available, its initial concurrent load also passed, but
the subsequent T0 unload was rejected as firmware busy after T0 sensor-event
chatter. The macro prevented T2 loading and paused; the heater was turned off.
An authorized retry retracted the path but hit the low-speed monitor; the hub
subsequently cleared and a state refresh now reports unloaded, still paused.
T1 and long-print endurance remain untested. See the review report for the T0
trace and firmware event-labeling caveat.
The attempted upstream merge was performed only in
a temporary local clone and was withheld because it conflicts with the Pi's CAN
changes in `klippy/msgproto.py`.

Review findings, verification, and rollback locations are recorded in
[the review report](evidence/review-2026-09-17.md).

## Installation layout

The plugin is the self-contained package in `klippy_extra/gco_routines`. The
installer creates one untracked symlink under `klippy/extras` and never edits a
tracked Klipper file:

```bash
git clone https://github.com/OpenAMSOrg/gco-routines.git
cd gco-routines
python tools/install_gco_routines.py --klipper /path/to/klipper
```

The [OpenAMS installer](https://github.com/OpenAMSOrg/klipper_openams) installs
this dependency automatically; it does not enable the extension or overwrite
existing macros. Activation remains an explicit configuration step after checking
the supported Klipper baseline and adapting macros to the printer.

Load this section before any `[gcode_macro ...]` section that uses literal
`START`, `END`, or `WAIT`:

```ini
[gco_routines]
```

The extension deliberately fails closed if the three command names already
exist, the Klipper dispatcher is not one of the validated baselines, or managed
macros were loaded before the extension could retain and validate their source.

Uninstall the development symlink with:

```bash
python tools/install_gco_routines.py --klipper /path/to/klipper --uninstall
```

Restarting Klipper and editing `printer.cfg` are deployment operations and are
not performed by the installer.

## Managed macros and results

A macro opts into incremental Jinja evaluation/command execution with the real
configuration property `render_mode: ordered`. The default is `legacy`: render
the entire template first, then execute its G-code, as stock Klipper does.
The property is not a macro variable and cannot be changed by SET_GCODE_VARIABLE.
Called macros always keep their own mode; the caller's mode is never inherited.

Ordered mode also works without concurrency controls:

```ini
[gcode_macro TIMING_EXAMPLE]
render_mode: ordered
variable_value: 0
gcode:
    SET_GCODE_VARIABLE MACRO=TIMING_EXAMPLE VARIABLE=value VALUE=1
    M117 Value {printer['gcode_macro TIMING_EXAMPLE'].value}
```

Here the status lookup sees the updated value. With the property omitted or set
to `legacy`, it sees the value at render time. Explicitly saved Jinja locals
remain snapshots, and ordered execution does not wait for buffered motion to
finish: use M400 where physical completion is required.

Concurrent macros use the same property:

```ini
[gcode_macro PREPARE_TOOL]
render_mode: ordered
gcode:
    START NAME=load
        LOAD_TRANSPORT
        {% set result.lane = reply.lane %}
    END

    CLEAN_NOZZLE
    WAIT ON=load
    M117 Loaded lane {waited[0].lane}
```

`LOAD_TRANSPORT` above represents a cooperating device command, not a command
provided by this plugin. Managed Jinja statements (`{% ... %}`) must occupy
separate physical lines from G-code/output expressions. Inline value expressions
such as `G1 X{params.X}` work normally. Output-producing Jinja filter/call blocks
are rejected in managed mode; legacy templates are unchanged.

Migration from the earlier plugin revision: add `render_mode: ordered` to each
macro intended to use START/END/WAIT. Their presence no longer changes rendering
mode automatically. Legacy output that emits these controls is rejected before
dispatch, with a configuration hint. Raw file/API controls are unchanged.
Stock Klipper does not recognize this new property (even `render_mode: legacy`);
omit it on installations without the extension.

`reply` is the structured response from the current ordinary command. A driver
can provide it from its command handler:

```python
manager = printer.lookup_object("gco_routines")
manager.driver_api.set_reply(gcmd, {"lane": 0, "loaded_mm": 684.5})
manager.driver_api.set_detail(gcmd, device="mmu", reason="loading")
```

`result` is a fresh per-routine namespace. Successful results are copied and
frozen as plain data; `waited` is the ordered result sequence from the most
recent successful `WAIT`. Missing managed fields are strict unless the existing
Jinja `default` filter is used explicitly.

## Ingress and lifecycle behavior

- Complete API scripts and virtual-SD files may contain raw control blocks.
- Virtual SD preflights the complete file through `_load_file`, covering both
  `M23`/`M24` and `SDCARD_PRINT_FILE`, and performs an implicit join before EOF
  can be reported as successful.
- Generated controls from legacy macros or rendered expressions are rejected.
- Interactive pseudo-TTY control blocks and line-number-framed controls are
  rejected because their complete source boundary is unavailable.
- Pause prevents new child admission. Cancel, reset, shutdown, and disconnect
  invalidate active runs and wake suspended waits.
- Unrelated external requests retain normal Klipper mutex serialization; only
  routines belonging to the lock owner's run receive cooperative admission.
- Failed/cancelled runs cannot issue subsequent ordinary commands, including
  commands in legacy helpers. Known pause/cancel/emergency handlers and configured
  virtual-SD error cleanup retain a scoped recovery path (no routine controls).

Software completion is not proof that buffered motion has physically stopped.
Use the existing Klipper barrier required by the underlying command (for
example, `M400`) before `END` when physical completion matters. Concurrent
commands still share the same printer hardware and must be synchronized by the
author.

## Single-FPS OpenAMS macros

`config/oams_macros.cfg` preserves `T0`–`T3` and standalone
`SAFE_UNLOAD_FILAMENT`. A toolchange completes cutting/toolhead retraction and
OAMS unloading first, then overlaps only OAMS loading with `CLEAN_NOZZLE`.
`M400` yields while the queued cleaning moves execute, and `WAIT` joins the
OAMS child before sensor validation, final extrusion, and position restoration.
Driver state is checked because the current OAMS driver reports some failures
without raising an exception.

To opt `_TX` into concurrency, copy both supplied config files and use:

```ini
[gco_routines]
[include oams_macros.cfg]
[include oams_macros_ordered.cfg]
```

The overlay adds `render_mode: ordered` to `_TX`; all other helpers stay legacy.
On stock Klipper, include **only `oams_macros.cfg`**, without the extension or
overlay. This base file is portable to unmodified upstream Klipper. It checks
both the live extension object (`'gco_routines' in printer`) and `_TX`'s configured
mode before rendering controls. Do not use `configfile.settings` to detect the
extension itself: Klipper omits empty sections. If either the extra or the
ordered opt-in is absent, Klipper never receives `START`, `END`, or `WAIT` and
runs OAMS loading followed by nozzle cleaning in ordinary serial order. Separate
continuation macros preserve fresh unload, load, inlet, and outlet checks despite
stock Klipper's render-entire-macro behavior. This fallback applies to the supplied
macro workflow; raw G-code files containing control instructions still require a
sender-side capability check because an absent receiver extension cannot consume
unknown commands.

Call `T0`–`T3` from the default routine: these macros own their background child
and cannot be placed inside another `START`. The printer must already be homed,
hot enough to extrude, and unpaused. Optional inlet/outlet checks remain disabled
until their corresponding switches are configured. Software tests do not validate
cutter geometry, filament tuning, or physical transport success.

The macro's default minimum toolchange temperature is 170 C, independently of
Klipper's `min_extrude_temp`. Adjust
`variable_minimum_extrude_temperature` only for a material with a known safe
cut/retract temperature.

## Development and verification

For live acceptance, see [the staged printer test suite](printer_tests/README.md):
32 opt-in no-motion API cases, virtual-SD/lifecycle fixtures, and separate
supervised motion/heater checks. Filament tests are restricted to **bay 1 / T1**.
The diagnostic extra is separate from production and is not installed by default.

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
python -m pytest -q tests --ignore=tests/integration
python -m gcoroutines examples/toolchange.gcode --closed-world --json
python -m gcoroutines examples/results.jinja --template --json
```

The command above runs the checkout-independent tests. The complete
`python -m pytest -q` suite additionally needs the two pinned, clean Klipper
checkouts at `vendor/klipper` and `vendor/klipper-upstream`. Their commit/hash
manifests are in `evidence/klipper-target-pin.json` and
`evidence/klipper-upstream-pin.json`; the target includes local CAN changes and
is not assumed publicly fetchable. Do not substitute another checkout and claim
the same baseline. The public upstream pin can be fetched with
`tools/fetch_klipper.py` (use a new output manifest, not an existing evidence file).
`tools/smoke_klipper.py` can then exercise that checkout with inert devices.

The reference parser remains in `gcoroutines/`; the deployable extra does not
depend on that package. Detailed pins and implementation evidence are in
`evidence/` and `IMPLEMENTATION_REPORT.md`.
