"""Offline CLI. Parses only; never sends commands or invokes Jinja helpers."""
import argparse
import json
from pathlib import Path
from .frontend import parse, lint_closed_world

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source", type=Path)
    ap.add_argument("--template", action="store_true", help="Parse a macro BODY using Klipper-style Jinja delimiters; not an entire printer.cfg")
    ap.add_argument("--closed-world", action="store_true", help="Assume ordinary commands cannot spawn referenced routines; check straight-line names")
    ap.add_argument("--json", action="store_true", help="Emit source AST and diagnostics as JSON")
    args = ap.parse_args(argv)
    try:
        source = args.source.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        ap.exit(2, f"Cannot read source: {exc}\n")
    report = parse(source, str(args.source), template=args.template)
    if args.closed_world:
        lint_closed_world(report)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        for d in report.diagnostics:
            print(f"{report.source}:{d.line}:{d.column}: {d.severity} {d.code}: {d.message}")
        print(f"{'PASS' if report.ok else 'FAIL'}: {report.validation_level}; no commands executed")
    return 0 if report.ok else 1

if __name__ == "__main__":
    raise SystemExit(main())
