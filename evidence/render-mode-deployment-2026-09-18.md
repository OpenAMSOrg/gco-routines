# Explicit render-mode deployment

Authorized by the user: "you may now deploy and restart".
Deployed to `pi@192.168.1.12` on 2026-09-18 at 06:42 UTC
(2026-09-17 23:42 PDT). Klipper returned ready, PID 6999.

## Scope and preflight

- Installed the six-module extra package, matching `oams_macros.cfg`, and new
  `oams_macros_ordered.cfg` overlay.
- Added `[include oams_macros_ordered.cfg]` immediately after `[include oams.cfg]`
  in the existing printer.cfg. The existing gco-routines include stays first.
- Did not install the optional diagnostic extra/macros or change MCU firmware,
  CAN configuration, dependencies, or tracked Klipper source.
- Preflight: ready, standby, unpaused, cold, heater targets zero, no motion,
  completed previous routines. OAMS current_group was T1. No startup delayed
  G-code with a positive initial_duration was configured.
- The installed macro matched the previously recorded hash. Its diff against
  the replacement was limited to the mode/capability condition and explanatory
  comments; no user tuning was overwritten. Original printer.cfg and changed
  plugin-module hashes were checked again immediately before replacement.

## Target-environment verification before installation

Staged in `/home/pi/gco-routines/.render-mode-stage.jmCrGf`.
Ran with the Pi's actual `/home/pi/klippy-env/bin/python` (Python 3.9.2):

```sh
/home/pi/klippy-env/bin/python /home/pi/gco-routines/.render-mode-stage.jmCrGf/smoke_klipper.py --klipper /home/pi/klipper --extra-parent /home/pi/gco-routines/.render-mode-stage.jmCrGf --macros /home/pi/gco-routines/.render-mode-stage.jmCrGf/oams_macros.cfg
```

Exit 0: repeated OAMS toolchanges, load/clean overlap and deliberate sensor abort
passed with inert hardware handlers. The expected `_OAMS_RAISE` traceback belongs
to this offline abort test, not to a failed live filament operation. No printer
configuration or live device connection was opened by this smoke harness.

Separately parsed the printer's full include tree with the staged overlay and
base macro. Confirmed the extension loads before macro sections and that only
`_TX`, among 57 configured macros, selects ordered rendering. Staged and installed
hashes matched the local tested sources.

## Backup and installation

Recoverable backup:
`/home/pi/gco-routines/backups/render-mode-20260918T064135Z`

Contains the previous `gco_routines/` package, `printer.cfg`, and
`oams_macros.cfg`. The ordered overlay did not exist previously.

Stopped Klipper, moved the old package into the backup, moved the staged package
into `/home/pi/gco-routines/gco_routines`, copied the three configuration files,
and started Klipper. The existing extras symlink was unchanged.

For rollback while idle: stop Klipper; preserve the current files separately;
restore the backup package, printer.cfg and oams_macros.cfg; move the newly added
ordered overlay out of the configuration directory; then start Klipper. Restore
these together so configuration and compiler selection rules agree.

Installed SHA-256:

```text
eed4f224ffda02e4c64f3d743d5c96c84017ee5f8245a8c239fe3af29c70f429  gco_routines/__init__.py
df0e6265eee7dfa72471b1c9d19c5ccc772a19db5ed9c6cf083a4522567fd7d0  gco_routines/driver_api.py
9a9c03bdbb020990d40152d5e7aa6ebf8604709e17b6c26b63d30a73013aacaa  gco_routines/integration.py
cc323558cc4c2608bb2fefa370f43af7378fcbdf8d17e69701b45b27edc182b5  gco_routines/program.py
826b804224a30fc4b21bb12533f4300548655df84de70103170fb7277819f4f4  gco_routines/runtime.py
d934f5db3c5df84a45b4843ff6bd61f287243d395ec4eea4e68e78a7e4a13e08  gco_routines/templates.py
a4334ace40359b95d670657b31f0e5db7cd28dd0c36f5d6cbe7b95f4ffbd617b  oams_macros.cfg
20440b38b986c45ca49cf67f5d636ab245a7a6d62514b897935376cb1d3b62fd  oams_macros_ordered.cfg
47e9cab9cdf152b62250eba7dd62ec582b3b528a563cf12f3c31c75fab233e1a  printer.cfg
```

## Post-restart verification

- `/printer/info`: ready, "Printer is ready"; service active/running.
- Live `configfile.settings`: `_TX.render_mode=ordered`; all 56 other macros
  report `legacy`.
- A first probe preflight stopped before sending G-code because its terminal-only
  routine check rejected the freshly initialized `run_default` placeholder
  (revision 0, default running, command null, no dependencies). Source inspection
  confirmed this is the idle initialization state. The next preflight explicitly
  accepted only that empty initial state or fully terminal runs.
- Sent exactly this no-motion API program:

```gcode
START NAME=deployment_probe
G4 P50
END
WAIT ON=deployment_probe
```

- HTTP result `ok`. Both default and child completed; revision 6, fault null,
  no pending dependencies/errors.
- Position unchanged across the probe; velocities zero. Restart had normally
  reset the reported coordinate state to zeros. No homing or movement was sent.
- T1 remained the current OAMS group, unpaused, standby; extruder 22.74 C and bed
  24.18 C, both targets zero. No load/unload or heating command was sent.
- Klipper HEAD remains `c0c7ef2a5a82f1b60c207fb02274c6536bb952cb`;
  both tracked and staged `git diff --exit-code` checks passed. Existing untracked
  extras remain; the version string's `-dirty` suffix is not a new tracked edit.
- The known `MFRC522 rfid_b` initialization / soft-reset timeout warning remains
  in the startup log. It did not prevent ready; no RFID repair was attempted.

The full 32-case staged printer acceptance suite and physical T1 workflow were
not run as part of this deployment. Local suite before deployment: 347 passed.
