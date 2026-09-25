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
