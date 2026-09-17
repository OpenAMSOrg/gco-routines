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
