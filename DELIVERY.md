# Delivery correction

The previously delivered gco-routines-codex-work.zip was inspected directly:
60 files, a valid ZIP CRC check, and all 59 original manifest entries matching.
It already contained AGENTS.md, both specification documents, frontend/semantic
source, grammar/, tests/, examples/, tools/, schemas/, and tools/fetch_klipper.py,
under an enclosing gco-routines-codex-work/ directory. It did NOT contain Git metadata.

The receiving agent's workspace was not inspected, so the exact reason that its
source files were unavailable is unknown. This replacement avoids the enclosing
directory and provides both a complete checkout with .git/ and a clonable bundle.

## What changed

- Added RECEIVING.md with exact extraction/clone and verification commands.
- Added tools/verify_handoff.py to check required paths, all payload hashes, and Git.
- Added delivery provenance and fresh reference-test evidence.
- Added a visible file inventory and refreshed the payload checksum manifest.
- Added extraction instructions to README.md and CODEX_PROMPT.md.
- Initialized a new local main-branch Git baseline with all payload files committed.
  This is not recovered historical project metadata or the Klipper repository.

No grammar, behavior document, parser, semantic reference, or original test source
was changed during this delivery correction.

## Implementation status is unchanged

gcoroutines/frontend.py is the validator/parser; gcoroutines/semantics.py is the
executable state-transition reference. A real Klipper scheduler, extra loader,
ordered-Jinja executor, and ingress/lifecycle adapters remain the implementation
task. Calling the reference a production runtime would be inaccurate.

The upstream Klipper checkout is intentionally absent. Its acquisition script
is supplied. The earlier network failure has not been reinterpreted as a download.

See evidence/delivery-reference-tests.txt for the fresh 165-test reference run.
