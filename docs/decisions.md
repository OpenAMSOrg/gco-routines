# Decisions carried from the design conversation

Keep: default sequential G-code; cooperative background blocks; optional NAME;
WAIT ON=a,b (no brackets); caller-local waits; result/reply/waited; existing Jinja
delimiters; observable dependencies; small independently embeddable frontend.

Drop: JOIN alias, Jinja start/wait functions, required names, operation-oriented
START DEVICE/ACTION syntax, explicit PUBLISH G-code, arbitrary threading APIs,
new timeout syntax, extra device protocols and a replacement MMU framework.

The most recent behavioral spec keeps modal G-code state shared. Earlier discussion
of automatic private parser modes is superseded. Likewise, 'waited' replaces early
'joined' wording. New-mode macros evaluate in order; unaffected macros stay legacy.

The desired result is an extra with a small, explicit upstream compatibility layer,
not a Klipper fork. An extra may wrap live objects; it must not hide changes to tracked
upstream files in installation scripts. All such hooks need verified compatibility.

New clarification: static grammar is necessary but not sufficient. Structure must
be literal to be validated before rendering. Name resolution, scope-dependent
availability, command replies and safety require later checks. Preserve ordinary
G-code syntax and do not make the slicer evaluate printer state to validate a block.

## Design corrections (review 2026-09-25)

- Contract errors are command errors. Klipper's dispatcher and webhooks shut the
  printer down on any exception other than `gcode.error`. Source, run/routine,
  dependency, result and managed-template errors (`ContractError`,
  `ProgramError`, `OrderedTemplateError`, Jinja `TemplateError`) are converted
  to `gcode.error` at every dispatcher seam; genuine internal errors are no
  longer masked as command errors. Consequently `M23`/`SDCARD_PRINT_FILE` of a
  rejected file now raises `gcode.error` instead of a bare `ValueError` (which
  shut Klipper down); the two tests asserting `ValueError` were corrected.
  Ordered-macro Jinja evaluation errors remain command errors, as stock Klipper
  reports legacy render errors.
- Pause suspends admitted children instead of faulting the run. Previously an
  admitted child's next command failed with "Routine command admission is
  paused", and the resulting run fault cancelled the default routine so the
  print could not be resumed. Now a paused run still rejects new routines, but
  an admitted child that reaches its own next command boundary suspends
  cooperatively (a reactor completion, outside the G-code mutex so RESUME and
  CANCEL_PRINT can always be accepted) until RESUME/CLEAR_PAUSE, then
  continues. Cancel/reset/shutdown wake and cancel it. Commands nested in a
  command already executing complete, as stock Klipper completes a running
  macro after PAUSE. Children are not suspended while a routine of the same
  run holds the G-code mutex inside WAIT, which would otherwise deadlock
  (RESUME cannot be accepted until that wait ends). Status keeps the schema:
  a suspended child is `waiting` with empty `waiting_on` and detail
  `{"suspended": "paused"}`.
- Virtual-SD preflight has no whole-file size limit. It previously rejected any
  file over 50 MB with `E_SOURCE_LIMIT`, even without controls, and built a
  whole-file `Program` in memory. It now streams the file through the
  `BlockCollector` without retaining ordinary lines; per-block limits and all
  structural errors are unchanged, plus a 16 Mi-character physical-line sanity
  bound. `parse_program` remains the parser for (small) complete API scripts.
- Virtual-SD preflight never holds Klipper's reactor (2026-11-13). Streaming
  every line through Python took 4.4 s on a Raspberry Pi for a 2.5-million-line
  OrcaSlicer file, inside SDCARD_PRINT_FILE on the reactor: no timer ran, the
  host keepalives to the printer's other MCUs stopped, and the AMS units'
  4 s host watchdogs fired at print start. A fast pass over the raw bytes
  (C-speed searches for a line that could be START, END, WAIT or transport
  framing, and for an overlong line) now settles a file with no control line
  without parsing it, about 7x faster; a file it cannot settle goes through
  the unchanged line-by-line pass. Both passes yield to the reactor between
  steps (1 MiB, or 20,000 lines), and a file validated before and unchanged
  (path, size, modification time) is not scanned again. The yields are
  cooperative reactor pauses, not threads.
- The implicit end-of-file join is an SD command, like an explicit final
  WAIT line: it holds the G-code mutex and sets the virtual-SD `cmd_from_sd`
  flag through its error cleanup. Previously it ran outside the mutex, so a
  PAUSE could take the mutex and spin in `do_pause()` waiting for a worker
  that was waiting for children queued behind that PAUSE (and an API cancel
  deadlocked the same way through `on_error_gcode`).

## Design addition: handled regions (2026-11-08)

Problem, found on a printer: OpenAMS turns a failed toolchange in a print into a
pause, so the owner can fix the cause and press RESUME. It catches the error in
its own command (`OAMSM_TOOLCHANGE`). Under gco-routines that could not work: the
dispatcher faults the run at the innermost line that raises, even when an outer
handler catches the error, so the handler's own `PAUSE` then raised "Run is
faulted" and Klipper ended the print through `on_error_gcode`.

Addition: `GcoRoutinesManager.handled_errors()`, a region opened by another
extra's command around the commands it runs (`Run.handled_errors(rid)` in the
runtime). The behavioral spec already limits the immediate run fault to an
*unrecoverable* routine error; a handled region is how a host component declares
the errors it recovers from itself. Inside it:

- an error raised by one of the owner's own commands does not fault the run; it
  reaches the owner as an ordinary `gcode.error`;
- a routine the owner starts fails alone: no other routine is cancelled, and the
  WAIT of a routine waiting on it raises with the reason.

When an error leaves the region, the routines it started that are still running
or waiting are cancelled: an in-flight command finishes, no later command of
theirs runs. Either way, the region's failed and cancelled routines are then
retired, so neither a bare WAIT nor the run's implicit final wait waits for them.
A routine of the region still running after a normal exit becomes an ordinary
routine of the run. An error the owner does not handle faults the run where
gco-routines next sees it, exactly as without the region; outside a managed run
(a console command) the region does nothing.

Invariant 10 holds: no command following a failed dependency reaches the
printer. A failed routine's dependents are its waiters, whose WAIT raises; the
region cancels the region's unfinished routines when an error leaves it; and the
owner is responsible for what the run does next (OpenAMS pauses the print, and
its RESUME finishes the toolchange before the file continues). The status schema
is unchanged: a routine that failed alone shows `failed` with its `error`, and the
run's `fault` stays null. Tests: `tests/test_handled_errors.py` (runtime) and
`tests/integration/test_handled_errors_print.py` (a real virtual-SD print).
The state machine is drawn in `docs/images/routine-states.svg`.

## Design addition: legacy rendering in recovery (2026-11-08)

Problem, found on the same printer the same evening: OpenAMS's RESUME retries a
failed toolchange by running its toolchange macro (`_TX`, an ordered macro) again
before the print continues. RESUME is a recovery handler, and a recovery handler
refuses every ordered macro at entry ("Recovery macros cannot start managed
routines"), so a retry that had to feed again could not run.

A recovery handler still starts no routines. `_TX` starts none while the printer
is paused: it takes its serial branch, the same one it takes without
gco-routines. Addition: `GcoRoutinesManager.legacy_in_recovery()`, a region its
caller opens (per greenlet, like the recovery context itself). Inside a recovery
handler, an ordered macro called inside the region renders all at once like a
legacy macro instead of being refused, and a control in its rendered output is
still refused, before any of its commands runs (the existing check of recovery
scripts). Outside a recovery handler the region changes nothing. It is opt-in at
the call site because rendering all at once is a different evaluation from
ordered rendering: only a caller that knows its macro's paused branch should ask
for it. Tests: four cases in `tests/integration/test_handled_errors_print.py`.
