# Troubleshooting

## What it is

A lookup table from the messages the extension and its installer print to
their causes. Search this page for the text you see.

## Where messages appear

| When | Where you see it |
|---|---|
| Startup (config load) | Klipper's error screen, `klippy.log`, Mainsail or Fluidd banner. Klipper is in the shutdown state. |
| Running a macro, file or API script | Console error; a print file pauses or fails like any G-code error |
| Installing | Terminal output of the installer |

Read the log with `tail -n 50 ~/printer_data/logs/klippy.log` (your path may differ).

## Startup and configuration

| Symptom | Likely cause | Fix |
|---|---|---|
| `Unsupported Klipper GCodeDispatch baseline` (may end with `, missing=...`) | `klippy/gcode.py` is not a tested version, or a Klipper internal changed | Compare `sha256sum klippy/gcode.py` with [install.md](install.md#supported-klipper-baselines); use a tested Klipper |
| `Reserved gco-routine command 'START' collides with an existing registration` (also `END`, `WAIT`) | Another extra or macro defines that command | Rename or remove it. The extension fails closed instead of replacing it |
| `[gco_routines] must be loaded before [gcode_macro NAME] so literal controls can be validated` | A macro loaded before `[gco_routines]` | Put `[gco_routines]` above every macro section and macro include |
| `Option 'render_mode' in section 'gcode_macro NAME' must be 'legacy' or 'ordered'` | Typo in the value | Use one of the two values |
| `Option 'render_mode' is not valid in section 'gcode_macro NAME'` | The extension is not loaded, so stock Klipper rejects the option | Add `[gco_routines]` or remove `render_mode` |
| `gcode_macro NAME:gcode: Macro syntax error [CODE] at line N: ...` | A control line is malformed in an ordered macro | See the code table below |
| `...: Managed Jinja statements must occupy separate command lines (line N)` | `{% ... %}` shares a line with G-code | Put the statement on its own line |
| `E_TEMPLATE_COMPOSITION: managed templates may not include, import or extend templates` | `{% include %}`, `{% import %}` or `{% extends %}` in an ordered macro | Inline the text, or keep the macro in legacy mode |
| `Managed templates do not support output-producing filter/call blocks` | `{% filter %}` or `{% call %}` in an ordered macro | Replace it with plain Jinja |
| `START at line N has no matching END` | Missing `END` | Add it |

## Syntax codes

| Code | Meaning |
|---|---|
| `E_START_SYNTAX` | Expected `START` or `START NAME=identifier`; names must be literal |
| `E_END_SYNTAX` | `END` takes no arguments |
| `E_WAIT_SYNTAX` | Expected `WAIT` or `WAIT ON=a,b` (no brackets, spaces or empty names) |
| `E_RESERVED_NAME` | `default` is reserved |
| `E_UNMATCHED_END` | `END` with no `START` |
| `E_NESTED_START` | A `START` inside a `START` |
| `E_UNCLOSED_START` | `START` reaches the end of the source without `END` |
| `E_TEMPLATE_REGION` | `START` and `END` are in different Jinja branches or bodies |
| `E_LITERAL_CONTROL` | A control is not on a complete literal source line |
| `E_CAPTURED_CONTROL` | A control is generated through captured or template-macro text |
| `E_JINJA_SYNTAX` | The Jinja itself does not parse |
| `E_SOURCE_LIMIT` | A `START` block exceeds 100,000 lines or 2 MB, or too many sources are open |

## Running

| Symptom | Likely cause | Fix |
|---|---|---|
| `E_GENERATED_CONTROL: legacy/internal rendering may not generate START, END, or WAIT; macros using literal controls must declare render_mode: ordered` | A legacy macro contains or emits a control | Add `render_mode: ordered` to that macro |
| `gco-routines requires a complete file/API source; pseudo-TTY or generated control input is unsupported` | A control typed alone into a terminal or console | Put the controls in an ordered macro or a file |
| `E_TRANSPORT: decode transport framing before using gco-routines controls` | Line-numbered, checksummed input (`N12 START`) | Send unframed G-code |
| `Recovery scripts cannot start managed routines` (or `Recovery macros ...`) | A pause, cancel or error handler tries to use `START` | Keep recovery macros free of controls |
| `Routine admission is paused` | `START` while the printer is paused | Resume first |
| `Nested spawning is not permitted in v0.2; macro NAME owns background routines` | A macro with its own routines is called inside a routine | Call it from the default routine |
| `Nested spawning is not permitted in v0.2` | `START` inside a routine | Restructure into flat routines |
| `Name is in use; starter must successfully collect completed instance` | Reusing a name before `WAIT` collected it | `WAIT ON=name` first, or use another name |
| `Unknown routine name in WAIT ON: NAME` | Misspelled name, or `WAIT` before `START` ran | Check the name and the order |
| `Self-wait or circular dependency` | A routine waits for itself or in a cycle | Remove the cycle |
| `Dependency did not succeed` | A routine you waited on failed | Read its `error` in the `gco_routines` status |
| `Macro NAME called recursively within routine ID` | A macro calls itself in one routine | Break the recursion |
| `Run faulted: ...` or `Run faulted before completion: ...` | A routine's command failed | Fix the cause in the quoted text; the run cannot issue more commands |
| `Cannot dispatch command: run is cancelled or faulted` | Work continued after a cancel or fault | Start a new job |
| `Virtual SD path escapes configured root` | A file path leaves the virtual SD folder | Use a path inside it |

## Update and uninstall

Installer messages are in [install.md](install.md#troubleshooting).

## Gotchas

- A routine that fails stops its run. The error is in
  `printer["gco_routines"].routines[*].error` and `fault`.
- Messages are quoted from the code and may change between versions.

## Where next

[language.md](language.md), [install.md](install.md), or open an issue at
[OpenAMSOrg/gco-routines](https://github.com/OpenAMSOrg/gco-routines).
