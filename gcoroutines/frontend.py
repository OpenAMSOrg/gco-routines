"""Parse the extension; never render a template or execute a command.

Ordinary G-code is opaque. The template mode delegates Jinja syntax to Jinja's
parser, recognizes control lines in TemplateData, and checks structural region
boundaries. Its output is a SOURCE AST, not a flattened executable Jinja program.
"""
from dataclasses import dataclass, field, asdict
import re
from typing import Optional, Any

IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
HEAD = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\b", re.ASCII)
TRANSPORT_CONTROL = re.compile(r"^N\d+\s*(?:START|END|WAIT)\b", re.I | re.ASCII)
RESERVED = {"START", "END", "WAIT"}

@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    line: int
    column: int = 1
    severity: str = "error"

@dataclass
class Node:
    kind: str
    line: int
    text: str
    column: int = 1
    end_line: Optional[int] = None
    name: Optional[str] = None
    targets: Optional[list[str]] = None
    body: list = field(default_factory=list)
    region: str = "root"

@dataclass
class Report:
    source: str
    mode: str
    nodes: list[Node] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    validation_level: str = "extension-syntax"
    template_control_flow: bool = False
    @property
    def ok(self):
        return not any(d.severity == "error" for d in self.diagnostics)
    def to_dict(self):
        return {"schema_version": 1, "source": self.source, "mode": self.mode,
                "ok": self.ok, "validation_level": self.validation_level,
                "template_control_flow": self.template_control_flow,
                "runtime_checks_required": ["name/instance binding", "active wait cycles", "result data", "host command validity", "device/resource policy"] + (["generated command guard", "live template values"] if self.mode == "jinja" else []),
                "ast": [asdict(n) for n in self.nodes],
                "diagnostics": [asdict(d) for d in self.diagnostics]}

def _content(line):
    # Native line comments; ordinary lines are stored without changing them.
    return line.split(";", 1)[0].strip(" \t")

def _head(line):
    m = HEAD.match(_content(line))
    return m.group(1).upper() if m else None

def control_line(line: str, number: int):
    """Return (node, diagnostics), with node=None for an ordinary line."""
    value = _content(line)
    if TRANSPORT_CONTROL.match(value):
        return None, [Diagnostic("E_TRANSPORT", "Decode and validate transport framing before parsing control commands.", number)]
    head = _head(line)
    if head not in RESERVED:
        return None, []
    col = len(line) - len(line.lstrip(" \t")) + 1
    if head == "START":
        m = re.fullmatch(r"START(?:[ \t]+NAME=(" + IDENT + r"))?", value, re.I | re.ASCII)
        if not m:
            return None, [Diagnostic("E_START_SYNTAX", "Expected START or START NAME=identifier; names must be literal.", number, col)]
        return Node("start", number, line, col, name=m.group(1)), []
    if head == "END":
        if value.upper() != "END":
            return None, [Diagnostic("E_END_SYNTAX", "END takes no arguments.", number, col)]
        return Node("end", number, line, col), []
    m = re.fullmatch(r"WAIT(?:[ \t]+ON=(" + IDENT + r"(?:," + IDENT + r")*))?", value, re.I | re.ASCII)
    if not m:
        return None, [Diagnostic("E_WAIT_SYNTAX", "Expected WAIT or WAIT ON=a,b (no brackets, spaces or empty names).", number, col)]
    names = m.group(1).split(",") if m.group(1) else None
    return Node("wait", number, line, col, targets=names), []

def _assemble(lines, controls, report, template=False):
    block = None
    for number, text in enumerate(lines, 1):
        node = controls.get(number)
        if node is None:
            node = Node("template_source" if template else "command", number, text)
        if node.kind == "start":
            if block is not None:
                report.diagnostics.append(Diagnostic("E_NESTED_START", "Nested START blocks are not supported.", number))
                continue
            node.kind = "routine"
            block = node
            report.nodes.append(node)
        elif node.kind == "end":
            if block is None:
                report.diagnostics.append(Diagnostic("E_UNMATCHED_END", "END has no matching START in this source.", number))
                continue
            if template and block.region != node.region:
                report.diagnostics.append(Diagnostic("E_TEMPLATE_REGION", "START and END must belong to the same Jinja body/branch.", number))
            block.end_line = number
            block = None
        elif block is not None:
            block.body.append(node)
        else:
            report.nodes.append(node)
    if block is not None:
        report.diagnostics.append(Diagnostic("E_UNCLOSED_START", "START reaches end of source without END.", block.line))


def parse(source: str, filename="<input>", template=False, max_bytes=2_000_000, max_lines=100_000):
    report = Report(filename, "jinja" if template else "gcode")
    if len(source.encode("utf-8")) > max_bytes:
        report.diagnostics.append(Diagnostic("E_SOURCE_LIMIT", "Source exceeds configured byte limit.", 1))
        return report
    source = source.replace("\r\n", "\n").replace("\r", "\n")
    if "\x00" in source:
        report.diagnostics.append(Diagnostic("E_NUL", "NUL is not allowed in source.", 1))
        return report
    lines = source.split("\n") if source else []
    if source.endswith("\n"):
        lines.pop()
    if len(lines) > max_lines:
        report.diagnostics.append(Diagnostic("E_SOURCE_LIMIT", "Source exceeds configured line limit.", 1))
        return report
    controls = {}
    if not template:
        for number, text in enumerate(lines, 1):
            node, errors = control_line(text, number)
            report.diagnostics.extend(errors)
            if node:
                controls[number] = node
    else:
        try:
            from jinja2 import Environment, StrictUndefined, nodes
        except ImportError:
            report.diagnostics.append(Diagnostic("E_DEPENDENCY", "Jinja template validation requires Jinja2; plain G-code does not.", 1))
            return report
        env = Environment(variable_start_string="{", variable_end_string="}",
                          undefined=StrictUndefined, autoescape=False,
                          keep_trailing_newline=True)
        try:
            tree = env.parse(source)
        except Exception as exc:
            report.diagnostics.append(Diagnostic("E_JINJA_SYNTAX", str(exc), getattr(exc, "lineno", 1)))
            return report
        report.validation_level = "extension-and-jinja-syntax"
        captured_types = {"AssignBlock", "FilterBlock", "Macro", "CallBlock", "Block"}
        unsupported = []
        def visit(n, region="root", captured=False):
            kind = type(n).__name__
            captured = captured or kind in captured_types
            if kind in {"If", "For"}:
                report.template_control_flow = True
            if kind in {"Include", "Import", "FromImport", "Extends"}:
                unsupported.append(n)
            if isinstance(n, nodes.TemplateData):
                for offset, fragment in enumerate(n.data.split("\n")):
                    ln = n.lineno + offset
                    if not (1 <= ln <= len(lines)):
                        continue
                    # Structural controls must exist as complete literal source lines.
                    if _head(fragment) in RESERVED or TRANSPORT_CONTROL.match(_content(fragment)):
                        original = lines[ln - 1]
                        if _content(fragment) != _content(original):
                            report.diagnostics.append(Diagnostic("E_LITERAL_CONTROL", "Control instructions must occupy complete literal source lines.", ln))
                            continue
                        node, errors = control_line(original, ln)
                        report.diagnostics.extend(errors)
                        if captured:
                            report.diagnostics.append(Diagnostic("E_CAPTURED_CONTROL", "Control instructions may not be generated through captured/template-macro text.", ln))
                        if node:
                            node.region = region
                            controls[ln] = node
                return
            for attr, value in n.iter_fields():
                if isinstance(value, list):
                    for idx, child in enumerate(value):
                        if isinstance(child, nodes.Node):
                            # Output children belong to their containing executable body.
                            child_region = region if kind in {"Template", "Output"} else region + f"/{kind}@{n.lineno}.{attr}"
                            # Separate sibling If nodes are distinguished by source line;
                            # source-line collisions cannot contain complete control lines.
                            visit(child, child_region, captured)
                elif isinstance(value, nodes.Node):
                    visit(value, region, captured)
        visit(tree)
        if controls and unsupported:
            report.diagnostics.append(Diagnostic("E_TEMPLATE_COMPOSITION", "Reference frontend cannot validate imported/inherited template structure in managed mode; inline it or extend the frontend.", unsupported[0].lineno))
    _assemble(lines, controls, report, template=template)
    if report.ok:
        _local_checks(report)
    return report


def _local_checks(report):
    """Context-sensitive checks that require no hardware and no execution."""
    for node in report.nodes:
        members = node.body if node.kind == "routine" else [node]
        if node.kind == "routine" and node.name == "default":
            report.diagnostics.append(Diagnostic("E_RESERVED_NAME", "The name 'default' is reserved.", node.line))
        for child in members:
            if child.kind != "wait" or child.targets is None:
                continue
            if len(set(child.targets)) != len(child.targets):
                report.diagnostics.append(Diagnostic("E_DUPLICATE_TARGET", "WAIT targets must be distinct.", child.line))
            if "default" in child.targets:
                report.diagnostics.append(Diagnostic("E_RESERVED_NAME", "The default routine is not an addressable background target.", child.line))
            if node.kind == "routine" and node.name in child.targets:
                report.diagnostics.append(Diagnostic("E_SELF_WAIT", "A routine may not wait on itself.", child.line))


def lint_closed_world(report):
    """Optional conservative name checks for a complete, straight-line source.

    Assumes opaque commands/macros do NOT create named routines. Does not prove
    physical safety, model command completion or replace the runtime wait graph.
    Template control flow is not flattened into a fictitious execution order.
    """
    if not report.ok:
        return report
    if report.mode == "jinja":
        report.diagnostics.append(Diagnostic("W_DYNAMIC_ANALYSIS", "Only source-local checks were performed; template branches and macro effects require runtime binding.", 1, severity="warning"))
        return report
    known = set()
    all_names = {n.name for n in report.nodes if n.kind == "routine" and n.name}
    collected = set()
    for node in report.nodes:
        if node.kind == "routine":
            if node.name in known and node.name not in collected:
                report.diagnostics.append(Diagnostic("E_NAME_IN_USE", "Name reused without a preceding successful collection by its starter.", node.line))
            prior = set(known)
            if node.name:
                known.add(node.name)
                collected.discard(node.name)
            for child in node.body:
                if child.kind == "wait" and child.targets:
                    for target in child.targets:
                        if target != node.name and target not in prior:
                            report.diagnostics.append(Diagnostic(
                                "W_SCHEDULE_DEPENDENT" if target in all_names else "E_UNKNOWN_NAME",
                                f"'{target}' is not started before this fork; binding at the child wait is runtime-dependent." if target in all_names else f"No START for '{target}' in this closed-world source.",
                                child.line, severity="warning" if target in all_names else "error"))
        elif node.kind == "wait":
            for target in node.targets or ():
                if target not in known:
                    report.diagnostics.append(Diagnostic("E_UNKNOWN_NAME", f"No preceding START for '{target}' under the closed-world assumption.", node.line))
            collected.update(node.targets if node.targets is not None else known)
    report.validation_level += "+closed-world-name-checks"
    return report


def guard_rendered_commands(text: str):
    """Do not admit controls manufactured by a template/legacy macro expansion.

    Call on the COMPLETE generated output of an ordinary-source node BEFORE
    dispatching any of its lines. Literal control AST nodes use native handling
    instead. This guard is a reference API; the Klipper adapter does not yet exist.
    """
    for number, line in enumerate(text.splitlines(), 1):
        if _head(line) in RESERVED or TRANSPORT_CONTROL.match(_content(line)):
            return [Diagnostic("E_GENERATED_CONTROL", "Generated text may not introduce START, END or WAIT; write literal control lines.", number)]
    return []
