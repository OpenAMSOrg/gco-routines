# Gco-routines in Klipper: extra-only implementation audit

**Audit date:** September 17, 2026  
**Input:** `gco-routines-spec.md`, Draft 0.1  
**Status:** Source-level feasibility assessment and isolated probes; not an implementation ready for a printer.

## Conclusion

The design appears implementable as a separately installed Klipper extra **without editing upstream source files on disk**. The complete specification cannot be implemented by registering `START`, `END`, and `WAIT` alone. It needs a small runtime plus explicitly version-sensitive interception of command dispatch, selected macro execution, and print-source lifecycle.

Those are different claims:

| Requirement | Assessment |
|---|---|
| No fork of Klipper or edits to tracked upstream files | Feasible architecture: install an additional extra/package and wrap selected live objects. |
| Only ordinary documented command-registration and status hooks | Enough for a restricted, separately owned program runner; not the full transparent specification. |
| Exact existing `[gcode_macro ...]` syntax and raw slicer G-code | Requires compatibility adapters at dispatch, macro, and input-source boundaries. |
| No MCU firmware change for host-side gco-routines | The orchestration runtime itself needs none. New device behavior or completion feedback may require separate device work. |
| Arbitrary existing commands are automatically safe to overlap | Not established and not promised by the specification. Shared hardware and modal state remain shared. |

**Recommendation:** an extra with a deliberately small compatibility shim, not a replacement reactor, a new MMU framework, or an unrestricted bypass of Klipper's command lock.

## Evidence and retrieval limitation

`git clone --depth 1` failed because the environment could not resolve GitHub. Archive and raw-file downloads into the container also failed. Browser access supplied several upstream `master` source pages, which were inspected directly. **No full checkout or verified commit SHA was obtained.** The browser-served files are not a cryptographically pinned, guaranteed coherent repository snapshot.

Fourteen isolated probes passed. The probes use selected upstream method excerpts, a deterministic scheduler test double, fake device commands, and the locally installed Jinja. They do not boot Klippy or exercise real heaters, motion, an MCU, a complete virtual-SD print, or Moonraker. Included excerpts were transcribed from browser-visible implementations and are explicitly labeled; they are not represented as downloaded source files.

Local environment: Python 3.13.5, Jinja2 3.1.6, greenlet 3.5.1, pytest 9.0.2. The inspected [upstream requirements](https://raw.githubusercontent.com/Klipper3d/klipper/master/scripts/klippy-requirements.txt) pin Jinja2 2.11.3. Testing the ordered compiler against the actual deployment environment is a release requirement, not optional cleanup.

## Inspected source boundaries

| Source | Relevant behavior |
|---|---|
| [`klippy/gcode.py`](https://raw.githubusercontent.com/Klipper3d/klipper/master/klippy/gcode.py) | Command registration, extended parameters, `_process_commands`, mutex ownership, ordinary handler results, unknown-command fallback, pseudo-TTY ingress. |
| [`klippy/reactor.py`](https://raw.githubusercontent.com/Klipper3d/klipper/master/klippy/reactor.py) | Greenlet-based suspension, retained `ReactorCompletion` results, multiple waiters, `ReactorMutex`. |
| [`klippy/extras/gcode_macro.py`](https://raw.githubusercontent.com/Klipper3d/klipper/master/klippy/extras/gcode_macro.py) | Full-template rendering, macro command wrapper, shared recursion flag, cached printer status, template environment. |
| [`klippy/extras/virtual_sdcard.py`](https://raw.githubusercontent.com/Klipper3d/klipper/master/klippy/extras/virtual_sdcard.py) | One-line command submission, file positions, print completion and error handling, pause/cancel entry points. |
| [`klippy/webhooks.py`](https://github.com/Klipper3d/klipper/blob/master/klippy/webhooks.py) | Script API admission, object queries and sampled subscriptions, independent emergency-stop endpoint. |
| [`klippy/klippy.py`](https://github.com/Klipper3d/klipper/blob/master/klippy/klippy.py) | Extra/package loading and printer-object lifecycle. |
| [Host-module documentation](https://www.klipper3d.org/Code_Overview.html#adding-a-host-module) | Supported loading/status facilities and warning against depending on other objects' private methods. |

## 1. Preserve the public syntax

No additional author-facing concurrency commands are needed:

```gcode
START NAME=filament_change
    T0
END

START NAME=nozzle_heating
    M109 S220
END

CLEAN_NOZZLE
WAIT ON=filament_change,nozzle_heating
```

Klipper's extended-parameter parsing already accepts `NAME=...` and a comma-separated `ON=...` value. Values preserve case; the extra can split and validate routine names itself. The local parser probe confirmed this behavior with the inspected implementation.

The extra must reject collisions with existing command names rather than replacing unrelated commands silently. This is a configuration error, not a reason to change the proposed language.

## 2. Capture blocks before ordinary dispatch

Registering a `START` handler does not capture the commands that follow it: the upstream dispatcher continues iterating through its input. Therefore the integration must observe the stream before ordinary commands are dispatched.

Proposed behavior:

```text
No managed run and no control instruction:
    retain the ordinary dispatch path

START encountered in a recognized source:
    collect a bounded, literal block through END
    do not execute the collected commands
    validate the block
    create a routine and schedule it

Ordinary command within a managed routine:
    attach source/routine/command-frame identity
    dispatch through the existing command implementation

WAIT:
    resolve target instances; validate the whole dependency set
    suspend only this routine
```

A narrow wrapper around `_process_commands` is a candidate interception point. It is **a private-method dependency**, not a newly discovered stable plugin interface. It also needs source context from callers: the dispatcher accepts commands and an acknowledgment flag, not a job identity or client identity.

A single global `collecting=True` flag is not sufficient. Commands from an unrelated API request, a called macro, or an emergency path must never become part of another source's block. The initial spec already prohibits blocks spanning macro definitions or separate submitted sources; the adapter must enforce that rule.

## 3. Reuse the reactor, but preserve command admission

The inspected reactor already has the important execution mechanisms. A retained completion can have multiple waiters, and waiting yields the current greenlet.

The immediate deadlock trap is:

```text
Parent enters run_script(), holding the G-code mutex.
Parent reaches WAIT and suspends.
Child tries run_script(), waiting for the same mutex.
Parent waits for child; child waits for parent.
```

The isolated probe reproduces this cycle. Another probe allows only the managed child to use `run_script_from_command`, which does not reacquire the outer mutex. The child completes, the parent resumes, and an unrelated external command remains serialized behind the parent's lock.

That establishes a possible execution mechanism, **not a finished permission model**. Production code needs to know when a child belongs to an admitted managed run and when it must acquire ordinary admission. Raw file input admits one command at a time, so there is not always a parent-held mutex covering an entire run. Yield points, transitions between file lines, other extras, and asynchronous control requests need explicit treatment.

Do not solve this by replacing the mutex with a no-op, releasing somebody else's mutex from `WAIT`, or sending all background work through an unrestricted low-level bypass.

Use reactor greenlets rather than Python OS threads for command execution. A routine context must follow its actual greenlet/call frame; a single mutable `current_routine` field left set across a suspension is incorrect. An arbitrary CPU-bound or blocking native command that never cooperatively yields will still block progress.

## 4. Ordered Jinja is the largest language-integration task

The existing macro path renders the entire template and then executes the resulting command text. `GetStatusWrapper` also retains a status snapshot within that evaluation. This must remain the behavior of unaffected legacy macros.

For a macro opted in by its literal concurrency instructions, the runtime must instead preserve these boundaries:

```jinja
START NAME=filament_change
    T0
    {% set result.lane = reply.lane %}
END

WAIT ON=filament_change
M117 Lane {waited[0].lane}
```

The child assignment occurs after `T0` completes. The final expression occurs after the native G-code `WAIT` completes. Parsing and compiling ahead of execution remain permissible; evaluating those statements ahead does not.

### Why changing `render()` to `generate()` is insufficient

Local Jinja probes found two independent problems:

1. Compiled template execution can resolve a binding before yielding output. Replacing `context.vars['waited']` after a wait did not update the already-resolved reference.
2. Consuming generated text to find `END` also executed Jinja statements inside that child body before any child command ran.

A runtime-owned proxy can address a live binding in a small example. It does not solve block extraction, ordinary local-variable copying, full scope rules, cancellation, or command boundaries by itself.

### Proposed implementation approach

Read the original macro source while configuration is being loaded. Parse its supported Jinja structure and literal G-code block boundaries together. Compile the default and background bodies into separate resumable execution functions/frames, with explicit command-dispatch boundaries and source locations.

Reuse Jinja's expression/scoping/compiler machinery where practical rather than inventing a new expression language. The exact extension/code-generation approach must be tested on Jinja 2.11.3 before being selected. A naive line-by-line `from_string()` loop is unsuitable because it loses multi-line `if`/`for` structure and local scopes.

Preserve `params`, `rawparams`, macro variables, ordinary Jinja scope behavior, and existing helper actions. Copy permitted ordinary local data at START; allocate fresh `result`, `reply`, and `waited` bindings for the child. Live printer access needs a fresh view at defined execution boundaries instead of a permanently stale cached wrapper.

Macro source should be captured during configuration; do not retain and use a Klipper configuration object after that phase. Installing the hook before template loading, or rebuilding only the selected macro objects from saved raw source, are alternative adapter designs.

## 5. Macro reentrancy is a separate compatibility issue

`GCodeMacro.cmd()` guards execution with one `in_script` Boolean on the macro object. The isolated probe confirms that two unrelated routines concurrently invoking the same macro can trigger the existing recursive-call error.

For full gco-routine semantics, managed calls need a recursion stack per routine, not a globally cleared guard. Genuine recursion must still be rejected. Called legacy macros should continue to render in their original way; making call-stack ownership routine-local is distinct from changing template evaluation.

A first constrained demonstrator may reject or serialize overlapping calls to the same legacy helper, but it must label that limitation. Setting `in_script=False` globally while a macro is suspended is not a valid implementation.

## 6. Structured results need an explicit driver-facing hook

The upstream dispatcher invokes handlers but does not retain their Python return value. An existing `T0` macro therefore does not automatically produce a structured `reply`.

A small extra-owned API is sufficient for cooperating command handlers. For example, this is proposed Python integration, not a new G-code command:

```python
# Inside a cooperating device command handler; illustrative API only.
runtime = printer.lookup_object("gco_routines")
runtime.set_detail(gcmd, device="mmu", reason="seeking_handoff")
# Wait through the device's existing cooperative completion mechanism.
runtime.set_reply(gcmd, {"lane": actual_lane, "loaded_mm": measured_length})
```

Bind that data to the current command frame. A nested macro's last subcommand must not accidentally become the outer command's reply. Clear the frame's reply for every ordinary command; handlers that supply no data produce an empty record. Keep console text and transport acknowledgments out of this interface.

At routine completion, validate and freeze explicit `result` fields as plain data. Namespace assignment can retain a Jinja Undefined object without immediately formatting it; final result validation must reject such objects rather than returning a fictitious successful value. Waiting consumers receive snapshots, not mutable references to another routine's namespace.

## 7. Observability fits existing status hooks

The extra can expose `get_status(eventtime)` with a revision and immutable routine records. [Klipper's object API](https://www.klipper3d.org/API_Server.html#objectssubscribe) provides query and subscription access without changing its wire format.

```json
{
  "id": 1,
  "method": "objects/subscribe",
  "params": {
    "objects": {"gco_routines": null},
    "response_template": {}
  }
}
```

Return new nested records when they change; in-place mutation can defeat change detection. Keep `get_status` bounded and nonblocking. Private Jinja locals should not be exported.

The inspected `QueryStatusHelper` samples status at `.25`-second intervals. This is suitable for current-state observability, but is not an event journal guaranteeing delivery of every intermediate revision. For the smallest implementation, define subscriptions as revisioned current-state snapshots, retaining completed outcomes until their documented retirement. A skipped revision is not a missed execution wakeup: scheduler dependencies are resolved internally, not through UI subscriptions.

An optional trace ring/API can be added later for every-transition debugging. Exposing data through the API does not, by itself, create a custom routine panel in an existing frontend.

## 8. File input and lifecycle are essential, not finishing touches

`VirtualSD.work_handler` submits one line at a time and handles file positions, EOF, print completion, and errors itself. It does not natively know about spawned routines. Macro return cannot be used as the job boundary because a later file command may wait on work started by a macro.

The virtual-SD adapter must:

- Preserve block collection across its per-line submissions, but not across unrelated sources.
- Retain a run identity until the file's real terminal outcome.
- Perform the implicit final WAIT before successful completion is reported.
- Detect a truncated block at EOF without running the incomplete body.
- Propagate child failures to the active print and prevent later success-path commands.
- Coordinate pause, resume, cancellation, reset, and shutdown with active routines.

Wrapping `note_complete()` alone is not a complete EOF solution: the inspected file reader emits its ordinary completion notification earlier. Wrapping the file/stream boundary or integrating with the worker before its EOF path is more appropriate than hiding the problem after completion has already been reported.

For complete API-submitted programs, the request boundary can define a run. For a long-lived pseudo-TTY stream, the run boundary is not automatically discoverable from arbitrary G-code. Support for that ingress needs an explicit adapter policy; it should not be silently equated with either one line or an endless job.

### Cancellation and emergency handling

Do not equate keeping the reactor responsive with keeping every control request runnable. Existing script-based pause/cancel requests can queue behind a long-running command. The managed runtime needs a control path that can mark the run, wake its own waits, and then execute appropriate existing host controls safely.

The existing emergency-stop path is independent and must remain independent of block collection. An arbitrary existing native command is not automatically cancellable at any instruction. Device cancellation needs explicit cooperation or an appropriate existing shutdown path. Cancelling Python work is not proof that an already-buffered physical action was stopped.

## 9. A stock receiver does not satisfy the spec's unsupported-command guarantee

The inspected ready-state default handler reports an unknown command instead of necessarily raising an execution error. A local dispatcher probe shows subsequent recognized commands still execute.

Therefore, the requirement that a receiver without the extension reject these jobs cannot be enforced by the absent extension. A slicer/host capability preflight or another deployment admission mechanism is required. Testing support after a job has already started is too late. This is a clarification to the deployment contract, not a new concurrency keyword.

## 10. Suggested code organization

The inspected loader accepts extras as either a module file or a package with `__init__.py`:

```text
klippy/extras/gco_routines/
    __init__.py       # load_config, command registration, status, lifecycle
    runtime.py        # run/routine/frame state, completions, dependency graph
    program.py        # literal G-code block parsing and source locations
    templates.py      # ordered Jinja compilation/evaluation
    integration.py   # isolated dispatch, macro, and input-source adapters
```

This layout is illustrative, not a requirement to create five frameworks. Keep the dependency graph, completion/result handling, and parse validation testable without Klipper. Confine accesses to upstream private members to the compatibility adapter.

Installation can add or symlink this package and enable `[gco_routines]` without replacing tracked upstream files. That is not the same as leaving upstream runtime behavior unchanged. Version/signature checks, conflict detection, and integration tests are required; unknown compatibility should disable the feature rather than degrade to sequential execution silently.

A restricted implementation avoiding private hooks is possible by giving the extra ownership of a separately submitted complete program. That would change the entry/configuration model and is not the exact transparent syntax specified here. The preferred path for this project is the explicit compatibility shim.

## 11. Implementation sequence and acceptance gates

**First gate — prove dispatch integration on a pinned Klipper checkout.** Load the extra, leave untouched G-code on its original path, capture flat blocks, run cooperative fake MMU/heater commands, and resolve named/anonymous WAIT including child-to-child dependencies. Verify unrelated external command admission and independent emergency stop.

**Second gate — implement ordered macros and results.** Run the supplied Jinja examples on the deployment Jinja version. Cover scopes, branches, loops, child-local snapshots, missing fields, nested macro calls, macro variable access, and the existing recursion/rename behavior. Confirm unchanged output/evaluation for legacy macros.

**Third gate — complete job integration.** Exercise real virtual-SD input, implicit EOF joins, malformed files, repeated tool changes/name reuse, pause/cancel/shutdown, late device replies, failure before WAIT, and status reconnects. Keep the chosen result-retention and queue bounds under stress tests.

Only then validate a real heater/MMU overlap on controlled hardware. No concurrency claim should exceed the command/resource combinations actually validated.

The small scheduler is not the main risk. The engineering work is preserving source boundaries, live Jinja evaluation, macro compatibility, and print lifecycle around it.

## 12. Observed probe results

All **14** isolated tests passed in the recorded local environment:

| Probe | Observed result |
|---|---|
| Extended parameter parsing | Comma-separated routine names and value case preserved. |
| Handler return value | Discarded by ordinary dispatch. |
| Unknown control commands | Do not inherently prevent later recognized work. |
| START registration alone | Does not collect following commands. |
| Child reacquires parent-held mutex | Parent and child remain blocked. |
| Managed child bypass within parent's admission | Child and parent progress; unrelated external command remains serialized. |
| Multiple completion waiters | Both receive the retained result; later reads still succeed. |
| Independent fake commands | Cooperative overlap demonstrated in deterministic simulated time. |
| Shared macro recursion guard | Second unrelated concurrent call is rejected. |
| Stock full rendering | Consuming a command result before execution fails. |
| Generator with replaced context binding | Previously resolved binding remains stale. |
| Generator with a live result proxy | Narrow result-binding example observes the new value. |
| Render-to-END block capture | Executes child Jinja too early. |
| Cached printer status wrapper | Retains old snapshot until wrapper is refreshed. |

These tests establish mechanisms and failure cases, not compliance of a finished extension. The package includes no installable `gco_routines` implementation, no complete Klipper checkout, and no claim of hardware validation.
