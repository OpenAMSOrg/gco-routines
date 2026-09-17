# gco-routines for Klipper

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
  Python 3.12/Jinja 3.1.6/greenlet 3.3.2: **246 tests passed**;
- the Pi's actual Python 3.9.2/Jinja 3.1.6/greenlet 2.0.2 environment,
  using real Klippy and the OAMS macro file with inert hardware handlers.

The extra and single-FPS macros are installed on the Pi following explicit
deployment authorization. The OAMS MCU is now connected and Klipper is ready;
a live no-motion `START`/`WAIT` probe and the cold/unhomed toolchange rejection
both passed without changing printer or OAMS state. Physical loading, cutting,
and motion remain untested. The attempted upstream merge was performed only in
a temporary local clone and was withheld because it conflicts with the Pi's CAN
changes in `klippy/msgproto.py`.

Review findings, verification, and rollback locations are recorded in
[the review report](evidence/review-2026-09-17.md).

## Installation layout

The plugin is the self-contained package in `klippy_extra/gco_routines`. The
installer creates one untracked symlink under `klippy/extras` and never edits a
tracked Klipper file:

```bash
python tools/install_gco_routines.py --klipper /path/to/klipper
```

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

A macro opts into ordered, incremental Jinja execution only when it contains a
literal control line. Legacy macros retain Klipper's render-entire-template
behavior.

```ini
[gcode_macro PREPARE_TOOL]
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
`WAIT` precedes sensor validation, final extrusion, and position restoration.
Driver state is checked because the current OAMS driver reports some failures
without raising an exception.

The same macro file is portable to an unmodified upstream Klipper. It tests for
the configured `[gco_routines]` object before rendering control instructions. If
the extra is absent, stock Klipper never receives `START`, `END`, or `WAIT` and
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

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
python -m pytest -q
python -m gcoroutines examples/toolchange.gcode --closed-world --json
python -m gcoroutines examples/results.jinja --template --json
```

The reference parser remains in `gcoroutines/`; the deployable extra does not
depend on that package. Detailed pins and implementation evidence are in
`evidence/` and `IMPLEMENTATION_REPORT.md`.
