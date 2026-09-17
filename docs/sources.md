# Sources and evidence boundaries

## Project inputs

- `behavior-v0.1.md`: the prior behavior-by-example specification, copied unchanged.
- `prior-klipper-audit.md`: the prior feasibility audit, copied unchanged.
- `../evidence/prior-audit/`: old probes, excerpt attribution and their COPYING.
  These source excerpts are transcriptions, not a verified full checkout.

## Primary documentation to recheck in the implementation workspace

- Klipper command templates: https://www.klipper3d.org/Command_Templates.html
- Klipper code/host modules: https://www.klipper3d.org/Code_Overview.html
- Klipper object API: https://www.klipper3d.org/API_Server.html
- Klipper repository: https://github.com/Klipper3d/klipper
- Jinja parsing/metadata API: https://jinja.palletsprojects.com/en/stable/api/
- Jinja scope/namespace assignments: https://jinja.palletsprojects.com/en/stable/templates/
- Codex AGENTS.md guide: https://developers.openai.com/codex/guides/agents-md

The Jinja API and Codex guide were accessible while preparing this handoff.
The current raw Klipper source opens and container repository retrieval failed;
therefore the prior audit is not upgraded to a new verified source assessment.
The grammar, examples and reference implementation here are project proposals,
not descriptions of functionality already shipped by Klipper.

A network-enabled workspace must use tools/fetch_klipper.py and record the resolved
commit before claiming compatibility. Method names in the handoff are integration
candidates; the pinned actual source and its tested behavior are authoritative.
