# Gco-routines: behavior by example

**Draft 0.1 — proposed extension, not existing Klipper functionality.**

Ordinary G-code remains sequential. `START` / `END` run a block independently; `WAIT` suspends the calling routine until its dependencies succeed. Jinja supplies values, not synchronization.

These examples specify behavior. They do not require operating-system threads, a new device protocol, or an MMU-specific framework. `T0`, `CUT_FILAMENT`, and `CLEAN_NOZZLE` refer to the machine's installed commands.

## 1. No background block: no change

```gcode
M109 S220
T0
CLEAN_NOZZLE
; The default routine executes these commands in their existing order.
; Existing command semantics, including motion buffering, are unchanged.
```

A **run** is one executing job or explicitly submitted program. Its ordinary command stream is the **default routine**. Called macros execute in the calling routine unless they explicitly start background work.

## 2. Anonymous background work

```gcode
CUT_FILAMENT
; Any required physical positioning/cutting must be complete before the fork.

START
    T0
END

CLEAN_NOZZLE
; The default routine continues while the background routine executes T0.
; This overlap is valid only when these commands use independent resources.

WAIT
; Continue only after the background routine succeeds.
M117 Ready
```

`START` and its matching `END` delimit a block. The executor collects the complete block without executing or rendering its body, creates a background routine, then continues after `END`. `END` is not a wait in the default routine. Reaching the end of the background body completes that routine.

Indentation is for readability; the delimiters define the block. Commands inside each routine remain ordered. Relative progress between routines is not guaranteed.

## 3. Optional names and selective waiting

```gcode
START NAME=filament_change
    T0
END

START NAME=nozzle_heating
    M109 S220
END

CLEAN_NOZZLE

WAIT ON=filament_change,nozzle_heating
; Wait for BOTH routines. No brackets, quoting, or Jinja expression needed.
```

```gcode
WAIT ON=filament_change
; Wait for this routine only. Other routines continue.

WAIT
; Wait for all other outstanding background routines in this run.
; With no outstanding routines, return immediately.
```

A wait binds its targets when executed; it does not acquire new targets later. Bare `WAIT` excludes the caller and the default routine. It includes completed routines whose results this caller has not yet collected, so finishing early cannot lose a result. Successful waiting marks the selected instances collected **for that caller**, not for other callers.

Names are run-local, case-sensitive identifiers using letters, digits, and underscores, beginning with a letter or underscore. `default` is reserved. Unknown names and empty `ON=` are errors, not indefinite waits. The comma-separated list contains distinct names without spaces.

## 4. Any routine may wait on another

```gcode
START NAME=nozzle_heating
    M109 S220
END

START NAME=filament_change
    T0
    WAIT ON=nozzle_heating
    CLEAN_NOZZLE
    ; Cleaning starts only after T0 and heating have both succeeded.
END

WAIT ON=filament_change
; This dependency includes filament_change's own wait and cleaning.
```

Only the calling routine suspends. Device feedback, other routines, status queries, and emergency handling continue. Multiple routines may wait on the same target; completion is retained state, not a consumable notification. Checking completion and registering a wait must not leave a missed-notification window.

```text
Illustrative observable dependency:

default: WAIT ON=filament_change
    filament_change: WAIT ON=nozzle_heating
        nozzle_heating: M109 S220 — waiting for temperature
```

A named target must already have been started. Waiting on oneself or adding a circular dependency is an execution error. The executor checks the entire proposed dependency set before registering a wait. Bare `WAIT` inside a background routine uses the same rules and can also create an invalid cycle.

## 5. Results: ordinary Jinja, native G-code waits

The examples below use Klipper's existing `{expression}` and `{% statement %}` delimiters.[1] They belong in macro definitions; raw slicer G-code does not need Jinja.

Each routine has three reserved bindings:

| Binding | Meaning |
|---|---|
| `result` | Private Jinja namespace for the fields this routine returns. Initially empty. |
| `reply` | Read-only structured response of its most recently completed ordinary command. Initially empty. |
| `waited` | Read-only sequence of result records collected by its latest successful `WAIT`. Initially empty. |

`result` uses Jinja's existing namespace assignment syntax.[2] The executor supplies it; authors do not need to construct it.

```ini
[gcode_macro PREPARE_TOOL]
gcode:
    START NAME=filament_change
        T0
        ; Example contract: this T0 handler returns lane and loaded_mm.
        ; T0 remains pending until the device confirms the operation.
        {% set result.lane = reply.lane %}
        {% set result.loaded_mm = reply.loaded_mm %}
    END

    CLEAN_NOZZLE
    WAIT ON=filament_change

    ; These expressions are evaluated AFTER WAIT succeeds.
    {% set change = waited[0] %}
    M117 Loaded lane {change.lane}
```

The example's response fields are supplied by the device-aware command handler, not automatically available from an existing `T0` macro. A handler without structured output supplies an empty `reply`; console text and transport acknowledgments are not results. Each completed ordinary command replaces `reply`, preventing stale data from masquerading as a new response. `START`, `END`, and `WAIT` are control instructions, not ordinary replies.

Only fields assigned to `result` are returned. Unassigned routines return an empty record. At successful completion, the executor freezes a snapshot containing plain data: strings, numbers, booleans, nulls, lists, and maps. Siblings cannot mutate it. Failure does not produce a successful result.

### Anonymous routines and result order

The following is a macro-body example in the same syntax:

```jinja
START
    T0
    {% set result.lane = reply.lane %}
END

START
    M109 S220
    {% set result.ready = true %}
END

WAIT
; Bare WAIT orders its selected results by START order, never finish order.
{% set change = waited[0] %}
{% set heating = waited[1] %}
M117 Lane {change.lane}, heated={heating.ready}
```

For `WAIT ON=a,b`, `waited[0]` is `a`'s result and `waited[1]` is `b`'s. A one-target wait still returns a one-element sequence. Inside a background routine, `WAIT` populates that routine's own `waited` in exactly the same way. An empty bare wait sets `waited` to an empty sequence. Later waits replace only the calling routine's `waited`; a saved result such as `change` remains unchanged.

### Evaluation order and compatibility

```jinja
START NAME=filament_change
    T0
    ; Do not evaluate this assignment until T0 has completed.
    {% set result.used_fallback = reply.used_fallback %}
END

WAIT ON=filament_change
; Do not evaluate this branch before WAIT returns successfully.
{% if waited[0].used_fallback %}
    M117 Fallback lane selected
{% else %}
    M117 Requested lane selected
{% endif %}
```

A macro containing these literal concurrency instructions uses **ordered evaluation**: execute its G-code and evaluate its Jinja statements in source order, resuming after blocking commands. Parse ahead, but do not evaluate ahead across commands or waits. Locals survive suspension; existing Jinja expression and scope rules still apply. Child routines receive copies of the ordinary local data visible at `START`, with fresh `result`, `reply`, and `waited` bindings. Live `printer` access remains shared observation, not copied hardware.

Legacy macros without the new instructions retain their existing whole-template rendering behavior.[1] Calling a legacy macro does not convert its internals into ordered evaluation. This is an explicit compatibility boundary, not a claim that stock Jinja or stock Klipper already executes these examples incrementally.

Missing result fields are evaluation errors in the new mode; optional fields may use the existing `default` filter. There are no Jinja `start()`, `join()`, or `wait()` functions.

## 6. Observable without extra reporting commands

The executor exposes a current snapshot and subscriptions through the host's existing status interface. In Klipper, the existing object query/subscription API is the integration point.[3] No `PUBLISH` command or custom UI logic is required to discover routine state and dependencies.

Illustrative snapshot; YAML is used for readability, not as a new wire protocol:

```yaml
revision: 12
routines:
  - id: run7:0
    name: default
    state: waiting
    command: WAIT ON=filament_change,nozzle_heating
    source: {macro: PREPARE_TOOL, line: 14}
    waiting_on: [run7:1, run7:2]

  - id: run7:1
    name: filament_change
    state: waiting
    command: T0
    source: {macro: PREPARE_TOOL, line: 3}
    waiting_on: []
    detail: {device: mmu, reason: seeking_handoff, lane: 0}

  - id: run7:2
    name: nozzle_heating
    state: completed
    result: {}
```

When the MMU confirms completion and its routine finishes:

```yaml
# Update to the SAME routine record, not a transient completion signal.
id: run7:1
name: filament_change
state: completed
result: {lane: 0, loaded_mm: 684.5}
```

Routine states are `running`, `waiting`, `completed`, `failed`, or `cancelled`. Every routine has a stable internal ID even when unnamed. Current command, source location, dependencies, terminal result/error, and a monotonically increasing snapshot revision are observable.

Command handlers may supply structured `detail` while blocked, such as phase, device, measurements with units, or an intervention reason. Without that feedback the UI shows only the known command and wait; it must not invent device progress. Private Jinja locals are not published automatically.

A subscription provides an initial snapshot and ordered revisioned updates, or requires a fresh snapshot after a delivery gap. Reconnecting observers can discover completed outcomes. Slow or disconnected observers must not block execution or wakeups.

## 7. Preserve existing physical command semantics

```gcode
; Assumes the required positioning mode and safe destination are established.
G0 X155 Y155
M400
; Existing motion completion barrier before independent device work begins.

START
    T0
    ; This handler must wait for real device completion, not just acceptance.
END

WAIT
```

`WAIT` waits for routines, not implicitly for all physical activity. Existing queued commands keep their meaning. For example, a background block containing buffered moves needs its existing `M400` barrier when its completion must imply finished motion.[4]

Gco-routines do not provide another toolhead, duplicate motion planners, or automatic isolation of printer objects and persistent macro variables. Existing modal G-code behavior remains shared; authors must not concurrently change modes or operate the same physical resource through independent sequences. Use `WAIT` to order conflicting work. Device integrations must serialize or reject unsafe overlap, not assume that putting commands in different routines makes it safe.

## 8. Completion, reuse, and errors

```gcode
START NAME=filament_change
    T0
END
WAIT ON=filament_change

START NAME=filament_change
    T1
END
WAIT ON=filament_change
; Reuse is allowed after the previous instance completed successfully
; and was collected by its starter. This WAIT refers to the NEW instance.
```

Each start creates a fresh internal identity. An already-registered wait keeps its original target even if that name is later reused. Named completed results remain available until name reuse or run teardown; snapshots already held by consumers stay valid. Retiring a name never invalidates an existing waiter. Resource limits must cause explicit rejection rather than silent result loss.

```gcode
START NAME=filament_change
    T0
    ; Suppose the handler reports an unrecoverable loading error here.
END

WAIT ON=filament_change
M117 Ready
; MUST NOT execute on that failure: the wait cannot report success.
```

An unrecoverable routine error faults/holds the run immediately through the host's existing failure path. Dependent waits report failure, not success. Already-running hardware follows its defined safety handling; a software wait ending does not prove the actuator stopped. Cancellation/shutdown uses existing host controls and prevents orphaned routine execution.

Run completion includes an implicit final wait for outstanding work. Macro return alone is not run completion. Results are not persistent across a controller restart.

The initial grammar is deliberately small: flat `START` / `END` blocks, named or anonymous, and `WAIT` with optional `ON`. Block boundaries must be literal complete command lines, matched within the same submitted source; they cannot cross Jinja branch/loop boundaries or macro definitions. Nested starts are rejected, including spawning through a called macro inside a background routine. A malformed block is rejected before any part of that block runs. No forward references, timeout syntax, new locks, signal commands, or cancellation language are added here.

Installations with existing commands named `START`, `END`, or `WAIT` must resolve that conflict explicitly; enabling this feature must not silently override them. A receiver without this extension must reject the required commands, not ignore synchronization.

## References for the compatibility baseline

These references describe existing syntax and APIs. All gco-routine behavior above is proposed by this document.

[1]: https://www.klipper3d.org/Command_Templates.html "Klipper command templates: delimiters, modes, and whole-template evaluation"
[2]: https://jinja.palletsprojects.com/en/stable/templates/#assignments "Jinja namespace assignments"
[3]: https://www.klipper3d.org/API_Server.html "Klipper object queries and subscriptions"
[4]: https://www.klipper3d.org/G-Codes.html "Klipper G-code commands and motion completion"

[1 — Klipper command templates][1] · [2 — Jinja assignments][2] · [3 — Klipper API][3] · [4 — Klipper G-codes][4]
