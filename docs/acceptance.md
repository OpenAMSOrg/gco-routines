# Acceptance checklist

The reference tests implement only the subset marked **reference** below. All
**integration** rows are work for Codex; they are not silently skipped passing tests.

| ID | Required observation | Level |
|---|---|---|
| P01 | Balanced flat blocks, named/anonymous forms, strict ON commas | Reference |
| P02 | Reserved malformed commands cannot become ordinary fallback | Reference |
| P03 | File/line diagnostics, comments, case, line endings | Reference |
| P04 | Jinja parse succeeds/fails without executing any helper | Reference |
| P05 | No delimiters crossing branches; literal names and controls | Reference |
| P06 | Executable CFG agrees with parser on valid/invalid corpus | Reference |
| S01 | Bare wait binds snapshot excluding caller/default | Reference |
| S02 | Explicit result order vs anonymous START order | Reference |
| S03 | Multiple/late waiters see retained immutable results | Reference |
| S04 | Instance-stable name reuse and caller-local collection | Reference |
| S05 | Atomic self/cycle rejection, not partial registration | Reference |
| S06 | Failure prevents success path; no false physical-stop claim | Reference |
| S07 | Bounded retained data and detached revisioned snapshots | Reference |
| I01 | Real reactor/dispatcher overlap without global lock bypass | Integration |
| I02 | Source-isolated block capture across real virtual-SD reads | Integration |
| I03 | Ordered Jinja with scopes/live replies on pinned dependencies | Integration |
| I04 | Legacy macro golden tests unchanged, reentrancy fixed per routine | Integration |
| I05 | No success before implicit EOF WAIT; no orphan routines | Integration |
| I06 | Cancel/pause/reset/errors/emergency paths under pending waits | Integration |
| I07 | Macro/helper-generated controls guarded before dispatch | Integration |
| I08 | Native status subscription current state/reconnect behavior | Integration |
| I09 | No tracked upstream files changed, install/uninstall checked | Integration |
| I10 | Capability preflight and command conflicts fail closed | Integration |
| I11 | Host transport framing and supported ingress are explicit | Integration |
| I12 | Cooperative device completion vs acceptance vs buffered motion | Integration |

At minimum, each I-row needs a failing regression before its implementation and
an integration test after it. Safety-adjacent paths must use fake hardware only.
Test schedule variations; a single lucky ordering is not a concurrency guarantee.
