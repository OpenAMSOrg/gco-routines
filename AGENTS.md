# Project instructions

Implement gco-routines as a Klipper extra; do not redesign the public model.
Read CODEX_HANDOFF.md and docs/language-v0.2.md before changing code.

## Invariants

1. Only `START [NAME=name]`, `END`, `WAIT [ON=a,b]`. No brackets, JOIN alias,
   action vocabulary, Jinja concurrency functions, timeouts or new lock language.
2. Ordinary code is sequential. Native waits suspend only their calling routine.
3. Flat blocks only. No direct or macro-mediated nested spawning in v0.2.
4. Names are optional, case-sensitive values. Bare waits and result collection are
   per caller; completion is retained, never consumed by the first waiter.
5. Preserve legacy macro rendering unless literal controls opt that macro in.
   Preserve command buffering, modal state, existing motion planner and MCU firmware.
6. `result`, `reply`, `waited` have the meanings in the behavioral spec. No console
   scraping, mutable shared result namespaces or implicit last-subcommand replies.
7. Parse before rendering. Keep grammar, name/type checks and runtime checks separate.
   No `eval`, rendering or printer helper calls in static validation.
8. No edits to tracked upstream Klipper files. Confine any private-method wrappers
   to a tested compatibility adapter. Unknown versions must not silently run anyway.
9. No Python OS threads for command execution; use the existing cooperative reactor.
   No global lock bypass or mutable global current-routine pointer across yields.
10. No command following a failed dependency may reach the printer. Cancellation
    of software is not proof of physical stop. Keep emergency handling independent.
11. No installing on or sending commands to a real printer during development.
    Use fake devices and a pinned Klippy test environment first.
12. Do not claim a passing reference test proves integrated Klipper behavior.

## Commands

```bash
python -m pytest -q
python -m gcoroutines examples/toolchange.gcode --closed-world --json
python -m gcoroutines examples/results.jinja --template --json
python tools/demo_state.py
```

Python >=3.9 is the reference-tool baseline. Validate the eventual adapter against
the Python/Jinja versions of the pinned Klipper checkout; do not upgrade the user's
Klipper environment just to hide compiler incompatibility. Do not change tests to
weaken public semantics without an explicit documented design correction.

Keep commits narrowly scoped. Record actual commands/results and outstanding
limitations. If integration needs an upstream change, demonstrate why and report
it; do not conceal the edit in an installer or quietly add a required fork.
