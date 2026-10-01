# Install

## What it is

The extension is one Python package, `klippy_extra/gco_routines`. Installing it
means creating one symlink under Klipper's `klippy/extras` directory. The
installer edits no tracked Klipper file.

## Requirements

| Item | Requirement |
|---|---|
| Python | 3.9 or newer, in the environment that runs Klipper |
| Klipper | A tested baseline (see below) |
| Python packages | `greenlet` and `jinja2`, which Klipper already needs |
| Access | SSH to the host, and a Klipper checkout you can write to |
| Idle printer | Restart Klipper only while nothing is printing |

### Supported Klipper baselines

The extension replaces some behavior inside Klipper's G-code dispatcher, so it
accepts only dispatchers it has been tested against. It identifies them by the
SHA-256 of `klippy/gcode.py`. Check yours:

```bash
sha256sum ~/klipper/klippy/gcode.py
```

The hash must be one of these:

| Hash | Source |
|---|---|
| `a2bcd6949b4263f608eaa71ba1cdfb713553542b7b90de739241b624f06415ae` | Upstream Klipper, commit `ad425fc22e01ca05db4852a81dfa9dab17373ff8` |
| `7cd92950767a06c9778d540360fe3ce23e51ec1a6b0423c3defc96d228bf96cc` | A Klipper checkout with local CAN changes |

If your hash differs, Klipper stops at startup with a configuration error
instead of running untested code. See [troubleshooting](troubleshooting.md).

## Install

### Standalone

```bash
cd ~
git clone https://github.com/OpenAMSOrg/gco-routines.git
cd gco-routines
python3 tools/install_gco_routines.py --klipper ~/klipper
```

The installer prints `Installed:` and the symlink path. It refuses to overwrite
an existing `klippy/extras/gco_routines`, and it reruns safely: on a correct
install it prints `Already installed`. Keep the checkout in place. The symlink
points into it.

### With OpenAMS

OpenAMS's `install.sh` installs this dependency for you. By default it clones
the repository to `~/gco-routines` and links it into `~/klipper`.

| Option | Effect |
|---|---|
| `-r <dir>` | Use a different checkout directory |
| `-k <dir>` | Use a different Klipper directory |
| `--skip-gco-routines` | Skip this step; no network access |
| `--require-gco-routines` | Stop the whole install on any gco-routines problem |

Without `--require-gco-routines`, a problem (a modified checkout, a foreign
`gco_routines` link, no network) prints a warning, skips this step and installs
OpenAMS anyway. Installing does not turn the feature on. See
[openams.md](openams.md).

## Configure the essentials

Add this section to `printer.cfg`, above every `[gcode_macro ...]` section and
every include that defines macros:

```ini
[gco_routines]
```

It takes no options. Then restart Klipper while the printer is idle. The
extension needs to see macro source as it loads, so a macro loaded earlier makes
it fail closed.

## Check that it works

After the restart, Klipper must reach the ready state. Then confirm the object
exists. With Moonraker:

```bash
curl -s 'http://localhost:7125/printer/objects/query?gco_routines'
```

The reply contains a `gco_routines` object with `schema_version`, `run_id`,
`fault` and `routines`. For a no-motion test, add this macro below the
`[gco_routines]` section, restart, and run `GCO_PROBE` from the console:

```ini
[gcode_macro GCO_PROBE]
render_mode: ordered
gcode:
    START NAME=probe
        M117 background
    END
    WAIT ON=probe
    M117 joined
```

The display shows `joined` and the console reports no error. Typing `START`,
`END` or `WAIT` by hand in a console fails by design. See
[language.md](language.md).

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `--klipper does not look like a complete Klipper checkout` | Wrong path | Point `--klipper` at the directory that contains `klippy/` |
| `destination already exists; refusing to overwrite it` | An older or foreign `gco_routines` in `klippy/extras` | Back it up, remove it, rerun |
| `Unsupported Klipper GCodeDispatch baseline` | `klippy/gcode.py` is not a tested version | Compare the hash above; move to a tested Klipper |
| `Reserved gco-routine command 'START' collides with an existing registration` | Another extra already defines `START`, `END` or `WAIT` | Rename or remove that command |
| `[gco_routines] must be loaded before [gcode_macro ...]` | Section order | Move `[gco_routines]` above all macros |

More in [troubleshooting.md](troubleshooting.md).

## Update and uninstall

Update by pulling the checkout, then restart Klipper while idle:

```bash
cd ~/gco-routines && git pull --ff-only
```

Uninstall. First remove `[gco_routines]` and every `render_mode` line from your
configuration, since stock Klipper rejects `render_mode`. Then:

```bash
python3 ~/gco-routines/tools/install_gco_routines.py --klipper ~/klipper --uninstall
```

The installer removes only its own symlink. OpenAMS's `-u` does not remove the
extension, because other macros may use it.

## Gotchas

- The extension is a symlink. Deleting or moving the checkout breaks Klipper's
  startup. Klipper takes this personally.
- The installer never restarts Klipper or edits `printer.cfg`.
- A Klipper update that changes `gcode.py` can stop the extension from loading
  until a tested version of gco-routines supports it. Update both with care.

## Where next

[language.md](language.md), or [openams.md](openams.md) if you run an AMS.
