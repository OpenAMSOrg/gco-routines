# Language addition and validation contract

**Draft 0.2.** This supplements the behavior-by-example specification; it does not
replace native G-code with a general-purpose programming language.

## 1. The entire public syntax

```gcode
START
    T0
END
CLEAN_NOZZLE
WAIT
```

```gcode
START NAME=heating
    M109 S220
END
START NAME=filament
    T0
    WAIT ON=heating
    ; Only this background routine waited for the heater.
END
WAIT ON=filament
```

`START`/`END` delimit a separately scheduled sequential block. The default routine
continues after END. WAIT with ON is an all-of dependency; no ON means all other
outstanding background instances in the run, bound at the moment of the wait.
Completed, not-yet-collected results are included in a bare wait for that caller.
Explicit ON results follow list order; bare results follow START order.

The commands/parameter keys are ASCII case-insensitive, matching the host's usual
command normalization. Identifier values are case-sensitive. Valid names match
`[A-Za-z_][A-Za-z0-9_]*`; `default` is reserved. Indentation is cosmetic. Trailing
semicolon comments are allowed. NAME and ON arguments are literal, not templates.
No spaces, quotes or brackets occur inside the comma-separated ON value.

See `grammar/gco-routines.ebnf` and `grammar/gco-routines.lark`. The latter is an
executable whole-program CFG, used as an independent syntax oracle in tests.

## 2. What “context-free” guarantees

The CFG recognizes structure: control syntax, complete blocks and their contents.
It deliberately treats an ordinary G-code line as an opaque terminal accepted by
the existing host. Malformed reserved control lines are not ordinary-command
fallbacks. Direct nested START is structurally rejected.

This flat addition is regular, and therefore context-free. The use of a CFG is
about an explicit language contract, not adding arbitrary nesting or claiming all
G-code dialects share a complete grammar. No new user syntax is needed for ASTs.

Names and runtime state are not grammar productions:

| Phase | Checks | Does not establish |
|---|---|---|
| Lex/parse, before rendering | START/END balance, argument shape, no direct nesting, Jinja syntax, branch boundaries | Live values or physical safety |
| Source-local semantics | Reserved/duplicate targets, explicit self-wait | All future name bindings |
| Optional closed-world analysis | Prior starts/name reuse in simple complete sources | Macro side effects or branch-dependent execution |
| Runtime admission | Current instance names, no nested macro spawn, whole proposed wait graph cycle check, device/config capabilities | Whether hardware will eventually finish |
| Command/result boundary | Rendered line validation, known schema when available, plain-data/missing-field checks | A physical stop not acknowledged by a device |

The reference validator labels its achieved level. `--closed-world` explicitly
assumes ordinary commands cannot secretly spawn names. Do not apply that assumption
to arbitrary user macro libraries. Template branch-sensitive name analysis remains
a future improvement; runtime validation is authoritative in all cases.

The grammar is checked before execution of any available complete macro/program.
An incremental source can validate only the available prefix; it must buffer and
validate an entire START block before admitting its body. It cannot guarantee the
validity of future bytes. Source IDs prevent unrelated requests joining a block.
The CLI parses macro bodies, not full Klipper configuration/include files. A
production adapter extracts bodies through the existing config loader and validates
all relevant bodies at configuration time without executing template functions.

## 3. Jinja is parsed, not used as a control preprocessor

```jinja
START NAME=filament
    T0
    {% set result.lane = reply.lane %}
END
WAIT ON=filament
{% set change = waited[0] %}
M117 Lane {change.lane}
```

The compiler sees the same literal START/END/WAIT before any rendering. T0 runs
before the assignment; WAIT succeeds before `waited[0]` is read. Jinja has no start
or wait function. A routine gets private locals and an explicit output namespace.
Missing fields fail in managed mode unless an ordinary `default` filter handles
an optional value. Device result schemas may enable additional static diagnostics;
arbitrary future `reply` fields cannot be type-proved from grammar alone.

```jinja
{% if params.DO_CHANGE|default(0)|int %}
    START NAME=filament
        T0
    END
    WAIT ON=filament
{% endif %}
; Valid: the whole block lies inside one branch.
```

```jinja
{% if params.DO_CHANGE|int %}
    START NAME=filament
{% else %}
    END
{% endif %}
; Invalid BEFORE rendering: a block crosses branch boundaries.
```

```jinja
START NAME={params.ROUTINE}
    T0
END
; Invalid: routine identities in the control grammar must be literal.
```

Regular numeric/string parameters remain templatable:

```jinja
START
    M109 S{params.TEMP|default(220)|float}
END
WAIT
```

`grammar/mixed-source.ebnf` describes the structural composition with if/for
bodies. The installed Jinja parser remains authoritative for Jinja syntax. A
complete block may occur in a loop, with normal name reuse rules when executed.
The reference frontend validates structure, stores source and source locations,
and **does not provide executable flattened Jinja IR**. Codex must implement the
ordered compiler while preserving scopes; do not execute the reference AST as if
all conditional lines were unconditional.

Captured strings, includes or template macro factories must not manufacture new
control structure. The reference tool explicitly rejects unresolved template
composition in managed mode; a production compiler must either implement it with
complete static source provenance or reject it clearly. It must not silently
reinterpret generated START/END/WAIT from opaque output as trusted literal nodes.

A rendered ordinary segment must be checked in full before any of its lines are
dispatched. `guard_rendered_commands()` demonstrates rejecting a generated control
line, including one introduced through an embedded newline. This guard is not yet
connected to Klipper. Validate ordinary rendered commands with host rules as well.
Existing trusted Jinja helper actions can still have effects in managed execution;
static parsing never calls them and is not a sandbox for malicious runtime templates.

## 4. Preserve the host, not a speculative replacement

No new G-code tokenization of ordinary parameters. No replacement motion planner.
No automatic private coordinate system, extruder or heater per routine. No runtime
isolation of persistent macro variables. Use native completion barriers such as
M400 where physical completion is required, and avoid conflicting device use.

Transport line numbers and checksums are ingress framing. Decode/validate them
with the appropriate host adapter before parsing this addition. The reference CLI
rejects framed extension commands with E_TRANSPORT; it does not alter framed legacy
lines. Unframed file G-code is the first implementation target. Pseudo-TTY streaming
requires explicit run/source ownership and is not silently supported by inference.

Legacy macros without literal controls retain full-template rendering. A legacy
macro called from a routine retains its rendering mode and executes in that routine;
managed recursion detection is per routine, not a globally disabled guard.

## 5. Observability contract clarification

Each routine has a stable ID, source location, current command, state, dependencies,
result/error and optional device-supplied detail. `get_status()` returns fresh,
detached current snapshots; existing host subscriptions may coalesce intermediate
revisions. They are NOT an every-transition event journal. A skipped revision does
not lose a completion or a scheduler wakeup, which uses internal retained state.
An optional trace journal is outside v0.2. Private Jinja locals are not published.

This explicitly clarifies the overly broad ordered-update wording in Draft 0.1.
The complete current state and retained outcomes are sufficient for the first UI.
The included demo produces synthetic snapshots, not real printer telemetry.

## 6. Capability preflight and errors

A sender must check that the receiver advertises this extension/version before
admitting a job. An absent extension cannot enforce rejection on a stock host that
continues after an unknown command. No new G-code capability keyword is required:
use existing host object/API access or an equivalent deployment manifest.

New controls must not collide silently with existing START/END/WAIT commands.
All invalid dependencies and malformed blocks reject/fault the managed execution;
never fall back to serial interpretation. Hardware cancellation still belongs to
the host/device safety path, not to a discarded software wait.

## 7. Runtime checks remain small and mandatory

WAIT validates and binds all targets atomically. Reject unknown/self/cyclic targets
before registering any part of a wait. Check cycles on active dependencies, not a
historical union of names from different instances. Explicit repeated waits may
reread a retained result. A name is reusable only after its instance succeeded and
its starter collected it. Existing waits retain the old identity after reuse.

No routine is preempted arbitrarily inside a non-yielding native command. The
implementation is cooperative. End-of-run joins background work before reporting
print completion. Error/cancel/reset prevents orphaned command admission. Completed
results are bounded; reject resource exhaustion rather than silently lose results.

The reference semantic model keeps records until run teardown and deliberately
rejects at its configured cap. Production may retire unreachable records while
honoring all existing waiters and retention contracts. That optimization is not a
reason to change public result semantics.
