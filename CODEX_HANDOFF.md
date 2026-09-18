# Codex implementation task: gco-routines

## Objective

Build a working, separately installed Klipper extra that implements the exact
behavior in `docs/behavior-v0.1.md`, as clarified by `docs/language-v0.2.md`.
No edits to tracked upstream source files. An explicit compatibility layer wrapping
selected live methods is permitted; a hidden fork or copied replacement gcode.py
is not. Preserve the user's small G-code vocabulary and current workflows.

This package is the implementation starting point, not another architecture task.
Do not merely restate this plan. Produce implementation code and integrated tests.

## Inputs and precedence

1. Public syntax and compatibility constraints in AGENTS.md.
2. Behavior Draft 0.1 plus language/validation clarifications Draft 0.2.
3. Formal grammars and tests as machine-checkable examples of those contracts.
4. Prior audit as evidence/hypotheses to verify on an actual pinned checkout.

If the reference code contradicts the written contract, add a failing regression,
explain the correction and fix it. Never quietly broaden the author-facing language.
The audit was not performed on a complete checkout. Its transcribed source excerpts
and fourteen old probes must not be passed off as current integrated upstream tests.

## Start by establishing a reproducible baseline

```bash
python -m pytest -q
python -m gcoroutines examples/toolchange.gcode --closed-world --json
python -m gcoroutines examples/results.jinja --template --json
python tools/demo_state.py
python tools/fetch_klipper.py --ref master --dest vendor/klipper \
    --manifest evidence/klipper-pin.json
```

Fetching resolves the ref to a real SHA and records selected file hashes. After
that, use the pinned SHA, not floating master, for all integration results. Record
Python, Jinja, greenlet and dependencies from that checkout. Test the installed
Klipper dependency versions rather than upgrading them to make the compiler work.
Network failed in the handoff environment; vendor/klipper is intentionally absent.

Inspect the real implementations of:

```text
klippy/gcode.py
klippy/reactor.py
klippy/extras/gcode_macro.py
klippy/extras/virtual_sdcard.py
klippy/extras/pause_resume.py
klippy/klippy.py
klippy/webhooks.py
scripts/klippy-requirements.txt
```

## Stage A — prove the native runtime with actual Klippy machinery

Implement a small extra package under `klippy/extras/gco_routines/` (or equivalent
separately installed location). The following is a suggested split, not a demand
for a framework:

```text
__init__.py       load_config, collisions, status and lifecycle
runtime.py        run/routine/frame identity, wait graph, completions, results
program.py        shared source parser and native command nodes
integration.py   every upstream-private wrapper in one place
```

Reuse the reference syntax, source locations and semantic invariants. Reuse the
Klippy cooperative reactor/greenlet machinery, not OS threads or a second event
loop. The reference Registry is an oracle, not a drop-in asynchronous scheduler.

Wrap/intercept source admission before ordinary dispatch so START captures the
entire literal block without rendering/executing it. Keep independent collectors
by source/run. Register collision checks for all three reserved command names.
Do not capture unrelated API requests, helper macro bodies or emergency traffic.

The difficult lock constraint: a parent suspended in WAIT may own the G-code
mutex, while its managed child must still progress. Only children of an admitted
run may use the appropriate internal execution path. Preserve unrelated external
command serialization. Raw virtual-SD lines do not hold one lock for the whole run;
explicitly handle that case rather than relying on a parent always owning the lock.
Never replace the lock with a no-op or call run_script() blindly inside children.

Use greenlet/frame-local context; a mutable global current_routine is incorrect
across yields. Attach every command to its actual frame and source. Called legacy
macros remain in the caller unless they contain a valid START in the default context.
Nested spawning, including through macro calls, must fail at runtime.

Acceptance: run the example with cooperating fake MMU/heater handlers but the real
reactor and dispatcher. Show overlap, caller-only suspension, child-to-child waits,
multiple waiters, completion-before-WAIT, explicit ordering, cycles, repeated names
and all failure cases. No full hardware capability claim follows from that test.

## Stage B — ordered Jinja without a new authoring language

```jinja
START NAME=filament
    T0
    {% set result.lane = reply.lane %}
END
WAIT ON=filament
M117 Lane {waited[0].lane}
```

Parse raw macro source during configuration. Validate literal boundaries before
rendering. Separate background bodies before they evaluate. Compile managed macros
into resumable execution using Jinja's parser/expression/scoping machinery where
possible; command dispatch and WAIT remain native effects. Macro configuration
must explicitly select `render_mode: ordered`; absent that property (or with
`render_mode: legacy`), preserve render-entire-template-then-execute behavior.
Do not infer rendering mode from literal controls or inherit it from callers.
Ordered macros need not contain concurrency controls. This explicit opt-in
supersedes the original Draft 0.2 control-presence selection rule.

A stock render(), render_async(), generate(), or a loop over from_string(line) is
not a compliant compiler. The prior probes explain premature body evaluation,
stale resolved locals and lost multi-line scopes. Preserve if/for/else, assignments,
normal scope rules, params, rawparams, macro variables and action helper semantics.
Do not mutate suspended Jinja Context internals and assume values become live.

Child ordinary local data is copied at START. `result`, `reply`, `waited` are fresh
per child. Printer access remains a shared live observation, refreshed at defined
execution boundaries. Do not retain a permanently cached pre-WAIT status wrapper.
Use strict missing-field behavior in managed mode; preserve legacy behavior elsewhere.

Provide a tiny Python API for cooperating device handlers, conceptually
`set_reply(gcmd, mapping)` and `set_detail(gcmd, **fields)`. Frame ownership is
mandatory: console text is not output and the last nested subcommand's reply is
not automatically the called macro's reply. Freeze plain-data results on success.
Ensure serialized status/result data cannot contain Jinja Undefined or arbitrary
runtime objects. Do not expose helper functions implementing WAIT inside Jinja.

Fix managed macro recursion tracking per routine. Do not toggle the upstream
macro object's shared in_script flag off globally to permit overlap. Genuine
recursive calls still fail. Keep rename_existing and collision semantics intact.

Acceptance: execute results.jinja, anonymous-results.jinja and branch/loop examples
with command barriers on the actual pinned Jinja version. Validate source without
rendering; compare legacy macro behavior against upstream baselines. Cover two
routines concurrently invoking the same yielding legacy helper.

## Stage C — file lifecycle and observable status

Own a managed run across virtual-SD per-line submissions; macro return is not end
of job. Validate available source before execution, preserve physical file/source
positions, and do an implicit final WAIT before ANY success/EOF notification.
Wrapping only note_complete() after earlier completion output is not sufficient.

Define API complete-program scope. For pseudo-TTY input, either implement an explicit
source/run policy and transport framing or reject managed use as unsupported. Do
not pretend a single line is a job or let routine names leak across unrelated users.

Integrate failure, pause/resume, cancel, reset and shutdown. Keep emergency stop on
an independent existing path. A paused run must not admit new routine commands;
whether a currently pending device operation continues depends on its explicit
policy. Cancellation must prevent new commands and stale completion must not revive
a terminated run. Native noncooperative commands cannot be arbitrarily killed safely.

Expose gco_routines.get_status() with detached revisioned snapshots compatible with
schemas/status.schema.json. Use existing object subscriptions; sampled updates are
not a promised event journal. Finished outcomes persist according to the retention
contract. Per-device phase/detail is optional. Do not invent progress when absent.

Require sender/receiver capability preflight before admitting a routine-enabled job;
an absent plugin cannot force stock Klipper to reject unknown commands. Fail closed
on reserved-command conflicts and unsupported compatibility adapters. There is no
new public capability G-code or new reporting keyword in this version.

Acceptance: integrated virtual-SD tests, delayed completion at EOF, truncated block,
error before WAIT, successful early child completion, repeated tool changes, cancel
while parent waits, independent emergency stop, reconnecting status consumers,
retention/resource pressure, and no stale execution after reset.

## Stage D — package the extra, without altering upstream files

Provide install/uninstall instructions that add or symlink only the extra and its
own dependencies. All upstream-private accesses stay in integration.py. Include
version/hash/signature checks with a clearly declared supported baseline. Record
behavioral compatibility tests, not just a list of method names that still exist.

Demonstrate before/after tracked-file hashes and `git diff --exit-code` for the
pinned source. A new untracked extra/symlink is allowed; editing existing tracked
files is not. Installation must not silently upgrade Klipper dependencies.

Deliver a sample [gco_routines] config, documented capability/status API and tests
that can be run with no connected printer. Only after these stages may hardware
validation be considered, and that is outside this task's authorization.

## Required implementation evidence

Produce `IMPLEMENTATION_REPORT.md` with the upstream SHA, interpreter/dependency
versions, test commands/output, modified/added files, supported ingress paths,
compatibility hooks, known limits and explicit untested hardware behavior.

The package currently contains syntax/transition tests, not a running extra. Do not
count mock-only tests as Klippy integration, and do not claim ordered Jinja is done
because it parses. Do not claim a full context-free grammar proves every dynamic
name, type or deadlock property. The remaining work must be reported honestly.
