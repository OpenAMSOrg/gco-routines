# Gco-routines / Klipper feasibility audit

This package is an audit and isolated experiment set. **It is not an installable Klipper extra. Do not install the probes in a printer's `extras` directory.**

Read `gco-routines-klipper-audit.md` for the findings and implementation boundary.

## What was actually retrieved and tested

A Git clone and archive download were attempted and failed in the audit environment. Several current upstream `master` source pages were inspected through browser access instead. No complete source checkout or verified commit SHA was obtained. The included upstream excerpts were transcribed from those browser-visible implementations and include scaffolding; they are not byte-for-byte downloaded files.

Fourteen isolated tests passed. They use selected upstream dispatcher/mutex/completion/macro implementations, a deterministic scheduler test double, fake command handlers, and the locally installed Jinja. No real MCU, heater, motion planner, Moonraker, virtual-SD print, or complete Klippy process was tested.

Local test environment: Python 3.13.5; Jinja2 3.1.6; greenlet 3.5.1; pytest 9.0.2. The inspected upstream dependency file pins Jinja2 2.11.3, so Jinja compiler conclusions must be retested against that environment before shipping.

## Run the isolated probes

```sh
python -m pytest probes -v
```

Requires `pytest`, `greenlet`, and `jinja2` in a separate development environment. Do not alter a working printer environment to run these probes.

`probe-results.txt` contains the actual test run output.

## Obtain a complete upstream checkout where network access is available

```sh
./fetch_upstream.sh ./klipper-upstream
```

The script refuses to overwrite an existing path and records the downloaded revision separately. It does not install an extension or change an existing printer checkout. This fetch script was syntax-checked but could not complete in the audit environment.

## Files

- `gco-routines-klipper-audit.md`: findings and proposed implementation structure.
- `gco-routines-spec.md`: unchanged copy of the supplied behavior specification.
- `probes/test_feasibility.py`: 14 isolated probes, including expected failure-mode reproductions.
- `probes/upstream_excerpts.py`: selected upstream methods with excerpt scaffolding, source URLs, and provenance.
- `probe-results.txt`: observed test results.
- `fetch_upstream.sh`: non-installing checkout helper.
- `COPYING`: GNU GPL version 3, governing the included Klipper-derived code.

Klipper-derived code retains Kevin O'Connor's copyright and GPLv3 licensing. New audit probe code is also provided under GPLv3. Written audit conclusions are identified as proposed designs or observed source behavior; the specification remains a proposal, not existing functionality.
