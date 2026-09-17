# Verification record

Date: 2026-09-17. This record covers the supplied development handoff only.

## Executed in this environment

- `python -m pytest -q`: **165 passed**, no skipped tests. See tests-final.txt.
- One test also compares 600 deterministically generated control variations against
  the executable CFG. These are not counted as 600 additional pytest tests.
- `PYTHONPATH=evidence/prior-audit/probes python -m pytest -q evidence/prior-audit/probes`:
  **14 passed**. These remain isolated excerpt/mock probes, not integrated Klippy tests.
- Both the plain tool-change and Jinja result examples validate; JSON ASTs are saved.
- Invalid cross-branch Jinja source is rejected before rendering at line 4 with
  `E_TEMPLATE_REGION`; see invalid-source-diagnostic.txt.
- The state demo produced three synthetic snapshots; no device/G-code execution.
- Python compileall succeeded for the reference package and utility scripts.
- The upstream-fetch utility help/argument entrypoint was exercised. A separate
  git ls-remote retrieval attempt failed DNS; see klipper-fetch.txt.
- See environment.json for exact local versions and schema-check.txt for schema checks.

## Not established

No full or pinned Klipper checkout was obtained. No extra loaded into Klippy.
No ordered/resumable Jinja compiler, dispatcher adapter or virtual-SD adapter is
implemented. No real heater/MMU, motion, pause/cancellation or emergency-stop test.
No deployment-version Jinja 2.11.3 validation was run. No current raw upstream source
was retrieved successfully in this turn. The prior audit remains a feasibility input.

Static validation checks the extension and Jinja syntax without evaluation. It does
not prove arbitrary ordinary G-code valid, all dynamic names present, all live result
fields typed, all command handlers cooperative, or any physical operation safe.
The tests exercise a transition model by explicitly calling state changes; they do
not execute concurrency on the actual printer event loop.

## Handoff status

No direct Codex Work task-creation/submission tool was available after connector
search. CODEX_PROMPT.md and CODEX_HANDOFF.md are ready to attach to that workspace;
no task was submitted, no repository was published, and no printer was modified.
