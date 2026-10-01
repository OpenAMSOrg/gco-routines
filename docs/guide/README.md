# gco-routines user guide

gco-routines is an extras-only Klipper extension. It lets one G-code routine
run in the background while the main routine keeps going, and it lets a macro
opt in to running its commands in the order it renders them.

## Why it exists

Stock Klipper renders a whole macro into a list of commands, then runs the
list. Every `{printer...}` lookup is evaluated before the first command runs,
and nothing else can happen inside the macro while a command is busy. That
makes it impossible to overlap two jobs, such as loading filament and cleaning
the nozzle.

Two toolchange steps that never touch the same hardware are the obvious
candidate. They are also, until now, strictly one after the other.

## What it adds

| Piece | What it does |
|---|---|
| `START` / `END` | Run the lines between them as a background routine. |
| `WAIT` | Pause the caller until background routines finish. |
| `render_mode: ordered` | Render and run a macro command by command, so later lines see the effects of earlier ones. |

Three literal commands and one configuration property. Nothing else about your
G-code or Jinja changes.

OpenAMS uses this to clean the nozzle while the AMS loads filament. The load
and the cleaning moves overlap, which shortens each toolchange.

## What it does not do

- It patches no Klipper source and no MCU firmware.
- It does not make hardware faster or safer. Background routines share the same
  printer, so you must keep them from fighting over one device.
- It does not change any macro that has not opted in.
- It supplies no hardware commands. `CLEAN_NOZZLE`, `T0` and everything else
  come from your own macros.

## Requirements

Python 3.9 or newer, a Klipper checkout the extension recognizes, and SSH
access to it. [install.md](install.md) has the full table and the baseline
check.

## The guide

| Page | Read it to |
|---|---|
| [install.md](install.md) | Install, check the baseline, update and remove the extension. |
| [language.md](language.md) | Write `START`, `END` and `WAIT`; use ordered macros. |
| [openams.md](openams.md) | Turn on concurrent toolchanges in OpenAMS, and learn when they run. |
| [troubleshooting.md](troubleshooting.md) | Match an error message to a fix. |

Read them in that order the first time. It saves reading a macro that cannot
work before finding out why.

## Gotchas

- The extension is a symlink into a checkout you keep. Delete or move that
  checkout and Klipper stops at startup.
- A macro that renders `START`, `END` or `WAIT` without `render_mode: ordered`
  is rejected. The controls do not opt the macro in by themselves.
- If the extension cannot load, Klipper stops and says why, rather than
  printing something halfway through a print.

## Where next

- [Language specification, draft 0.2](../language-v0.2.md)
- [Behavior by example](../behavior-v0.1.md)
- [Sample configuration file](../../examples/gco_routines.cfg)
- [Project README](../../README.md), including macro results and lifecycle rules
- [Staged printer test suite](../../printer_tests/README.md)

Source: [OpenAMSOrg/gco-routines](https://github.com/OpenAMSOrg/gco-routines).
