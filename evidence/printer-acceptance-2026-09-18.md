# Staged printer acceptance suite

The user requested printer tests for the entire concurrent API and restricted
filament testing to bay 1 / T1. This work prepared tests, not a deployment or
physical test run. No connection to the printer, G-code submission, configuration
change, service restart, or upstream source edit was performed in this step.

## Deliverables

- `printer_tests/gco_routines_test.py`: separate opt-in test extra, bounded
  structured event journal, reactor-only native wait, public driver reply/detail
  hooks. No device access or production scheduler replacement.
- `printer_tests/macros.cfg` and `suite.py`: 32 shared printer/CI acceptance
  cases covering the public controls, dependency/results semantics, ordered
  Jinja, native helper frames, errors and complete API ingress.
- `tools/run_printer_tests.py`: explicit-target, default-list-only client;
  read-only cold/idle preflight, sampled status, HTTP serialization check,
  report preservation on failure, and no retry/restart/hardware recovery.
- Four exact virtual-SD files, plus manual pause/cancel/reset/shutdown and
  WebSocket reconnect/subscription acceptance steps.
- Optional guarded hardware macros for X/M400, M109 to 50 C, and T1-only
  load-unload-load. The latter refuses any other initially loaded group.

## Local verification

```text
.venv/bin/python -m pytest -q
315 passed in 34.09s

.venv/bin/python tools/run_printer_tests.py list
32 cases listed; no network connection

git diff --check
git -C vendor/klipper diff --exit-code
git -C vendor/klipper-upstream diff --exit-code
All exited 0.
```

Full test output is in `evidence/printer-suite-local-2026-09-18.txt`.
Environment: Python 3.12.3, Jinja 3.1.6, greenlet 3.3.2. Target-checkout pin:
`c0c7ef2a5a82f1b60c207fb02274c6536bb952cb`; clean-upstream pin:
`ad425fc22e01ca05db4852a81dfa9dab17373ff8`.

All new runtime/client Python sources also parsed under Python 3.9's grammar;
this is a syntax compatibility check, not a new Pi interpreter execution claim.

The tests exercise the actual shipped cases on real Klippy, including every
expected failure. The virtual-SD worker executes the actual EOF, failure, and
uninterrupted lifecycle files; malformed file rejection is checked before its
prefix can run. Hardware commands are inert in these checks. A separate run
executes successive cases in one Klippy instance and verifies recovery between
expected failures without resetting the runtime.

During preparation two fixture issues were corrected: a test expected a
different spelling of the undefined-result error, and a diagnostic macro name
with a digit embedded in it was not dispatched as intended by Klipper. The
hardware entry point is now `GCO_TEST_FILAMENT`; tests prove both its guarded
failure and its T1/unload/T1 success path. A client regression also proves that
an in-flight status snapshot is not mistaken for the post-HTTP-return state.

## Not yet verified on the printer

Installation, all 32 live API cases, the uploaded files, lifecycle operations,
status subscriptions/reconnect, the native motion/heater fixtures, and the T1
roundtrip all remain **NOT RUN** for this new suite. Prior live toolchange
observations remain historical evidence, not passes for newly created tests.
Dangerous startup fault injection/resource exhaustion stays in isolated CI;
the coverage matrix does not count those as live printer tests.
