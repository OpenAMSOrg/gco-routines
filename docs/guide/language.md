# Language

## What it is

Three literal commands, `START`, `END` and `WAIT`, and one macro option,
`render_mode`. Everything else in your G-code and Jinja stays as it is, which
is the whole sales pitch. This page is the short version. The
[full specification](../language-v0.2.md) and
[behavior by example](../behavior-v0.1.md) have the detail.

## What it adds

| Addition | Meaning |
|---|---|
| `START [NAME=id]` ... `END` | The lines between them run as a separate routine on Klipper's reactor. The caller continues after `END`. |
| `WAIT [ON=a,b]` | Suspend only the caller until the named routines finish. With no `ON`, join all of the caller's outstanding routines, in `START` order. |
| `render_mode: ordered` | In a `[gcode_macro]`, render and run one command at a time instead of rendering everything first. |

## Requirements

[`[gco_routines]`](install.md) must be loaded before any macro that uses these
commands. A macro that contains `START`, `END` or `WAIT` must also set
`render_mode: ordered`.

## Install

See [install.md](install.md).

## Configure the essentials

```ini
[gco_routines]

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

`LOAD_TRANSPORT` stands for any command your hardware provides. The plugin
supplies none.

### Rules for the three commands

| Rule | Detail |
|---|---|
| Names | `[A-Za-z_][A-Za-z0-9_]*`, case-sensitive. `default` is reserved. |
| Literal arguments | `NAME` and `ON` are not templates. `NAME={params.X}` is rejected. |
| `ON` lists | Comma-separated, no spaces, quotes or brackets: `ON=a,b`. |
| One line each | A control occupies a complete physical line. A trailing `;` comment is fine. |
| No nesting | A `START` inside a `START` is rejected. A macro that owns a routine cannot be called from inside another routine. |
| Whole blocks | `START` and `END` must sit in the same Jinja branch or loop body. |
| Name reuse | A name is free again once its routine succeeded and the starter collected it with `WAIT`. |
| Pause | While paused, a new `START` is refused. A running routine stops at its next command boundary and continues on `RESUME`. |

### Results

| Name | Where | Meaning |
|---|---|---|
| `reply` | Inside a routine | Structured reply of the last command, if the device driver provided one. |
| `result` | Inside a routine | Private namespace: `{% set result.lane = reply.lane %}`. |
| `waited` | After `WAIT` | Result list from the latest successful `WAIT`. |

Missing fields are an error unless you use the Jinja `default` filter.

### Ordered rendering

In `legacy` mode (the default), Klipper renders the whole macro first. In
`ordered` mode, each command runs before the next line renders:

```ini
[gcode_macro TIMING_EXAMPLE]
render_mode: ordered
variable_value: 0
gcode:
    SET_GCODE_VARIABLE MACRO=TIMING_EXAMPLE VARIABLE=value VALUE=1
    M117 Value {printer['gcode_macro TIMING_EXAMPLE'].value}
```

Ordered mode shows `1`. Legacy mode shows `0`.

A macro that renders `START`, `END` or `WAIT` needs `render_mode: ordered`.
Without it, Klipper rejects the macro with `E_GENERATED_CONTROL` before it
dispatches anything. The bundled
[example configuration](../../examples/gco_routines.cfg) follows this rule.

- `render_mode` takes `legacy` or `ordered`. It is fixed at config load.
  `SET_GCODE_VARIABLE` cannot change it.
- A called macro keeps its own mode. It never inherits the caller's.
- Ordered mode runs at command boundaries. It does not wait for buffered motion.
  Use `M400` where you need the move to finish.
- A saved Jinja local is a snapshot. Look up `printer` again after a command.

### What is allowed where

| Place | Controls allowed? |
|---|---|
| `render_mode: ordered` macro | Yes |
| `render_mode: legacy` macro | No. Rendered output containing a control is rejected. |
| Virtual SD (print) file | Yes. The file is checked before it runs, and an implicit join happens at the end. |
| Complete API script | Yes |
| Console, one line at a time | No |
| Macro text that includes, imports or extends a template | No, in ordered macros |
| `{% call %}` or `{% filter %}` blocks | No, in ordered macros |
| A Jinja statement and a G-code line on the same line | No, in ordered macros |

## Check that it works

See the probe macro in [install.md](install.md#check-that-it-works).
`printer["gco_routines"]` reports `routines` with each `state`, `command`,
`waiting_on` and `error`, which is the place to look while a routine runs.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `E_GENERATED_CONTROL` | A legacy macro contains the controls | Add `render_mode: ordered` |
| `E_START_SYNTAX`, `E_END_SYNTAX`, `E_WAIT_SYNTAX` | Malformed control line | Match the rules above |
| `Managed Jinja statements must occupy separate command lines` | Mixed Jinja and G-code on one line | Split the line |

All messages are listed in [troubleshooting.md](troubleshooting.md).

## Update and uninstall

Nothing to update in the language itself. Removing the extension means
removing `render_mode` and all controls. See [install.md](install.md#update-and-uninstall).

## Gotchas

- Stock Klipper rejects the `render_mode` option, even `legacy`. Without the
  extension, leave it out.
- Routines share one printer. Two routines driving the same heater or extruder
  will interleave. The extension orders commands, not hardware.
- Software completion is not a physical stop. Put `M400` before `END` when it matters.
- Ordered mode is opt-in per macro. It never turns on by itself, even if the text contains `START`.

## Where next

[openams.md](openams.md) for a working example, or the
[full specification](../language-v0.2.md).
