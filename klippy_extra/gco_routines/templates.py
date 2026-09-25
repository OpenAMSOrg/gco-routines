"""Ordered Jinja templates for managed gco-routines macros.

This module deliberately keeps the ordinary Klipper macro path out of the
managed compiler. The adapter calls this compiler only for macros explicitly
configured with ``render_mode: ordered``, with or without concurrency controls.
The Jinja AST is compiled once and then
consumed through ``Template.generate``; each generated output line is a
dispatch boundary.  Consequently assignments, branches, loops, and ordinary
Jinja expressions are evaluated in source order even when a callback yields.

The small callback surface is intentional.  Templates do not get a Jinja
``wait`` or ``start`` function: the compiler supplies private callbacks only
for literal control nodes found by the source parser.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import jinja2
from jinja2 import meta, nodes

try:  # Jinja 2.11 calls this contextfunction; 3.x calls it pass_context.
    _pass_context = jinja2.pass_context
except AttributeError:  # pragma: no cover - exercised on Klipper's older Jinja
    _pass_context = jinja2.contextfunction


_CONTROL_NAMES = frozenset(("START", "END", "WAIT"))
_MAX_MANAGED_OUTPUT = 1024 * 1024
_INTERNAL_NAMES = frozenset(
    ("result", "reply", "waited", "printer", "_gco_dispatch", "_gco_wait", "_gco_spawn")
)


def _frontend_api():
    """Return the reference parser when installed, with a small extra fallback.

    The Klipper extra is deployable on its own; the reference ``gcoroutines``
    package is not a runtime dependency.  The fallback intentionally performs
    only the admission checks needed here and never renders source while
    validating it.
    """

    try:
        from gcoroutines.frontend import guard_rendered_commands, parse

        return parse, guard_rendered_commands
    except ImportError:
        import re
        from types import SimpleNamespace

        control_re = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\b", re.ASCII)
        ident = r"[A-Za-z_][A-Za-z0-9_]*"

        def line_control(raw, line):
            clean = raw.split(";", 1)[0].strip()
            match = control_re.match(clean)
            if not match or match.group(1).upper() not in _CONTROL_NAMES:
                return None
            head = match.group(1).upper()
            if head == "START":
                m = re.fullmatch(r"START(?:[ \t]+NAME=(%s))?" % ident, clean, re.I | re.ASCII)
                if not m:
                    return SimpleNamespace(kind="error", line=line, code="E_START_SYNTAX")
                return SimpleNamespace(kind="start", line=line, name=m.group(1), body=[])
            if head == "END":
                if clean.upper() != "END":
                    return SimpleNamespace(kind="error", line=line, code="E_END_SYNTAX")
                return SimpleNamespace(kind="end", line=line)
            m = re.fullmatch(r"WAIT(?:[ \t]+ON=(%s(?:,%s)*))?" % (ident, ident), clean, re.I | re.ASCII)
            if not m:
                return SimpleNamespace(kind="error", line=line, code="E_WAIT_SYNTAX")
            return SimpleNamespace(kind="wait", line=line, targets=m.group(1).split(",") if m.group(1) else None)

        def fallback_parse(source, filename="<macro>", template=True):
            # Deploy-time subset of the reference frontend. Controls are
            # recognized only in literal TemplateData occupying a complete
            # source line, and START/END must share one Jinja region.
            try:
                env = jinja2.Environment(
                    variable_start_string="{", variable_end_string="}",
                    undefined=jinja2.StrictUndefined,
                )
                tree = env.parse(source)
            except Exception as exc:
                return SimpleNamespace(
                    ok=False,
                    diagnostics=[SimpleNamespace(
                        code="E_JINJA_SYNTAX",
                        line=getattr(exc, "lineno", 1), severity="error",
                        message=str(exc))],
                    nodes=[],
                )

            raw_lines = source.replace("\r\n", "\n").replace("\r", "\n").splitlines()
            controls = {}
            diagnostics = []
            captured_types = {"AssignBlock", "FilterBlock", "Macro", "CallBlock", "Block"}
            unsupported = []

            def content(value):
                return value.split(";", 1)[0].strip()

            def visit(item, region="root", captured=False):
                kind = type(item).__name__
                captured = captured or kind in captured_types
                if kind in {"Include", "Import", "FromImport", "Extends"}:
                    unsupported.append(item)
                if isinstance(item, nodes.TemplateData):
                    for offset, fragment in enumerate(item.data.split("\n")):
                        lineno = item.lineno + offset
                        if not (1 <= lineno <= len(raw_lines)):
                            continue
                        node = line_control(fragment, lineno)
                        if node is None:
                            continue
                        if node.kind == "error":
                            diagnostics.append(SimpleNamespace(
                                code=node.code, line=lineno, severity="error",
                                message="Malformed control"))
                            continue
                        if content(fragment) != content(raw_lines[lineno - 1]):
                            diagnostics.append(SimpleNamespace(
                                code="E_LITERAL_CONTROL", line=lineno,
                                severity="error",
                                message="Control must occupy a complete literal line"))
                            continue
                        if captured:
                            diagnostics.append(SimpleNamespace(
                                code="E_CAPTURED_CONTROL", line=lineno,
                                severity="error",
                                message="Captured template text may not contain controls"))
                        node.region = region
                        node.body = []
                        node.end_line = None
                        node.targets = getattr(node, "targets", None)
                        node.name = getattr(node, "name", None)
                        controls[lineno] = node
                    return
                for attr, value in item.iter_fields():
                    if isinstance(value, list):
                        for child in value:
                            if isinstance(child, nodes.Node):
                                child_region = (region if kind in {"Template", "Output"}
                                                else "%s/%s@%s.%s" %
                                                (region, kind, item.lineno, attr))
                                visit(child, child_region, captured)
                    elif isinstance(value, nodes.Node):
                        visit(value, region, captured)

            visit(tree)
            if controls and unsupported:
                diagnostics.append(SimpleNamespace(
                    code="E_TEMPLATE_COMPOSITION",
                    line=unsupported[0].lineno, severity="error",
                    message="Managed templates may not import or extend templates"))

            top = []
            block = None
            for lineno, raw in enumerate(raw_lines, 1):
                node = controls.get(lineno)
                if node is None:
                    node = SimpleNamespace(kind="template_source", line=lineno,
                                           text=raw, body=[])
                if node.kind == "start":
                    if block is not None:
                        diagnostics.append(SimpleNamespace(
                            code="E_NESTED_START", line=lineno,
                            severity="error", message="Nested START"))
                    else:
                        node.kind = "routine"
                        block = node
                        top.append(node)
                elif node.kind == "end":
                    if block is None:
                        diagnostics.append(SimpleNamespace(
                            code="E_UNMATCHED_END", line=lineno,
                            severity="error", message="Unmatched END"))
                    else:
                        if block.region != node.region:
                            diagnostics.append(SimpleNamespace(
                                code="E_TEMPLATE_REGION", line=lineno,
                                severity="error",
                                message="START and END must share a Jinja body/branch"))
                        block.end_line = lineno
                        block = None
                elif block is not None:
                    block.body.append(node)
                else:
                    top.append(node)
            if block is not None:
                diagnostics.append(SimpleNamespace(
                    code="E_UNCLOSED_START", line=block.line,
                    severity="error", message="Unclosed START"))
            return SimpleNamespace(ok=not diagnostics,
                                   diagnostics=diagnostics, nodes=top)

        def fallback_guard(text):
            diagnostics = []
            for line, raw in enumerate(text.splitlines(), 1):
                clean = raw.split(";", 1)[0].strip()
                match = control_re.match(clean)
                transport = re.match(r"^N\d+\s*(?:START|END|WAIT)\b",
                                     clean, re.I | re.ASCII)
                if transport or (match and match.group(1).upper() in _CONTROL_NAMES):
                    diagnostics.append(SimpleNamespace(code="E_GENERATED_CONTROL", line=line, message="Generated controls are not allowed"))
                    break
            return diagnostics

        return fallback_parse, fallback_guard


class OrderedTemplateError(ValueError):
    """A source or execution error in a managed template."""


class LiveReplyProxy:
    """A read-only view of the current routine's most recent reply.

    Missing values return the environment's Undefined object rather than a
    Python ``KeyError``.  This is important because Jinja's existing
    ``default`` filter can then handle an explicitly optional field while
    StrictUndefined still fails when the field is actually used.
    """

    def __init__(self, getter: Callable[[], Mapping[str, Any]], undefined=None):
        self._getter = getter
        self._undefined = undefined or jinja2.StrictUndefined

    def __setattr__(self, name, value):
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            raise AttributeError("reply is read-only")

    def _missing(self, key: Any):
        return self._undefined(name="reply.%s" % key, hint="reply field is missing")

    def __getitem__(self, key):
        data = self._getter() or {}
        return data[key] if key in data else self._missing(key)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        data = self._getter() or {}
        return data[name] if name in data else self._missing(name)

    def __contains__(self, key):
        return key in (self._getter() or {})

    def get(self, key, default=None):
        data = self._getter() or {}
        return data.get(key, default)

    def __repr__(self):  # pragma: no cover - diagnostic convenience
        return "<LiveReplyProxy %r>" % (dict(self._getter() or {}),)


class LiveWaitedProxy:
    """A live, read-only sequence of the caller's latest WAIT results."""

    def __init__(self, getter: Callable[[], Sequence[Any]], undefined=None):
        self._getter = getter
        self._undefined = undefined or jinja2.StrictUndefined

    def __setattr__(self, name, value):
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            raise AttributeError("waited is read-only")

    def _missing(self, index):
        return self._undefined(name="waited[%s]" % index, hint="waited result is missing")

    def __getitem__(self, index):
        values = tuple(self._getter() or ())
        try:
            return values[index]
        except (IndexError, KeyError, TypeError):
            # Jinja's default filter can use this value, while an ordinary
            # attribute access or conversion still raises under StrictUndefined.
            return self._missing(index)

    def __len__(self):
        return len(tuple(self._getter() or ()))

    def __iter__(self):
        return iter(tuple(self._getter() or ()))

    def __repr__(self):  # pragma: no cover - diagnostic convenience
        return "<LiveWaitedProxy %r>" % (tuple(self._getter() or ()),)


class LiveGetStatusWrapper:
    """Shared printer observation with an explicit per-boundary cache.

    Klipper's stock ``GetStatusWrapper`` caches object lookups for one complete
    render.  Managed execution instead invalidates this cache around every
    command and wait boundary, so a resumed expression observes current state.
    """

    def __init__(self, printer, eventtime=None):
        self.printer = printer
        self.eventtime = eventtime
        self._cache: Dict[str, Any] = {}

    def clear_cache(self):
        self._cache.clear()

    def _lookup(self, name):
        name = str(name).strip()
        if name in self._cache:
            return self._cache[name]
        obj = self.printer.lookup_object(name, None)
        if obj is None or not hasattr(obj, "get_status"):
            raise KeyError(name)
        reactor = self.printer.get_reactor()
        now = reactor.monotonic() if self.eventtime is None else self.eventtime
        guard = getattr(reactor, "assert_no_pause", None)
        if guard is None:
            value = obj.get_status(now)
        else:
            with guard():
                value = obj.get_status(now)
        value = copy.deepcopy(value)
        self._cache[name] = value
        return value

    def __getitem__(self, name):
        return self._lookup(name)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._lookup(name)

    def __contains__(self, name):
        try:
            self._lookup(name)
        except KeyError:
            return False
        return True

    def __iter__(self):
        objects = self.printer.lookup_objects()
        return iter([name for name, obj in objects if hasattr(obj, "get_status")])


class MacroRecursionTracker:
    """Track managed macro recursion per routine, not per macro object."""

    def __init__(self):
        self._stacks: Dict[str, set] = {}

    def enter(self, routine_id: str, macro_name: str) -> None:
        stack = self._stacks.setdefault(routine_id, set())
        if macro_name in stack:
            raise OrderedTemplateError(
                "Macro %s called recursively within routine %s" % (macro_name, routine_id)
            )
        stack.add(macro_name)

    def exit(self, routine_id: str, macro_name: str) -> None:
        stack = self._stacks.get(routine_id)
        if stack is None:
            return
        stack.discard(macro_name)
        if not stack:
            self._stacks.pop(routine_id, None)


def _split_output_nodes(body: Sequence[nodes.Node]) -> List[nodes.Node]:
    """Split static newlines in Output nodes without rendering expressions."""

    out: List[nodes.Node] = []
    current: List[nodes.Node] = []
    current_line = None

    def flush():
        nonlocal current, current_line
        if current:
            out.append(nodes.Output(current, lineno=current_line or current[0].lineno))
        current = []
        current_line = None

    for item in body:
        if not isinstance(item, nodes.Output):
            flush()
            out.append(item)
            continue
        for child in item.nodes:
            if isinstance(child, nodes.TemplateData):
                parts = child.data.split("\n")
                for offset, part in enumerate(parts):
                    if offset:
                        flush()
                        current_line = child.lineno + offset
                    if part:
                        if current_line is None:
                            current_line = child.lineno + offset
                        current.append(nodes.TemplateData(part, lineno=child.lineno + offset))
            else:
                if current_line is None:
                    current_line = getattr(child, "lineno", getattr(item, "lineno", 1))
                current.append(child)
    flush()
    return out


def _static_output_text(item: nodes.Node) -> Optional[str]:
    if not isinstance(item, nodes.Output):
        return None
    if not all(isinstance(child, nodes.TemplateData) for child in item.nodes):
        return None
    return "".join(child.data for child in item.nodes)


def _snapshot_plain(value):
    """Copy ordinary local data while leaving host objects shared."""

    if isinstance(value, (str, bytes, int, float, bool, type(None))):
        return value
    if isinstance(value, dict):
        return {k: _snapshot_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_snapshot_plain(v) for v in value)
    if isinstance(value, set):
        return {_snapshot_plain(v) for v in value}
    if isinstance(value, jinja2.utils.Namespace):
        return jinja2.utils.Namespace(**{
            k: _snapshot_plain(v)
            for k, v in value._Namespace__attrs.items()
        })
    # Printer and helper objects are observations, not routine-local state.
    return value


def _namespace_data(namespace) -> Dict[str, Any]:
    attrs = getattr(namespace, "_Namespace__attrs", None)
    if attrs is None:
        attrs = getattr(namespace, "_attrs", {})
    return {key: _freeze_result(value) for key, value in dict(attrs).items()}


def _freeze_result(value, depth=0):
    """Detach result data and reject Undefined/host objects at completion."""

    if isinstance(value, jinja2.Undefined):
        raise OrderedTemplateError("Managed result contains an undefined field")
    if depth > 32:
        raise OrderedTemplateError("Managed result is too deeply nested")
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float:
        if value != value or value in (float("inf"), float("-inf")):
            raise OrderedTemplateError("Managed result contains a non-finite number")
        return value
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_result(item, depth + 1) for item in value)
    if isinstance(value, dict):
        if any(type(key) is not str for key in value):
            raise OrderedTemplateError("Managed result map keys must be strings")
        return {key: _freeze_result(item, depth + 1) for key, item in value.items()}
    raise OrderedTemplateError("Managed result contains unsupported %s" % type(value).__name__)


@dataclass
class _Child:
    name: Optional[str]
    template: "OrderedTemplate"
    line: int


class OrderedTemplate:
    """A compiled managed template and its statically extracted child bodies."""

    def __init__(self, env, template, children: Sequence[_Child], source: str = ""):
        self.env = env
        self.template = template
        self.children = tuple(children)
        self.source = source

    def render(self, context=None, **kwargs):
        """Compatibility shim for the Klipper child callback.

        A child scheduled by a runtime is normally executed through
        :meth:`execute`.  The shim is useful to adapters that already prepare
        private callbacks and invoke the compiled child like a stock template.
        """

        values = dict(context or {})
        values.update(kwargs)
        # Even the adapter compatibility path must consume the generator
        # incrementally; ``Template.render`` would buffer all output and can
        # evaluate a post-WAIT expression before the wait callback runs.
        for _ in self.template.generate(values):
            pass
        return ""

    def generate(self, context=None, **kwargs):
        values = dict(context or {})
        values.update(kwargs)
        return self.template.generate(values)

    def execute(self, runtime, context_vars: Mapping[str, Any], caller_routine=None):
        """Run in source order, delegating effects to the supplied runtime."""

        run = runtime.get_current_run() if hasattr(runtime, "get_current_run") else None
        if caller_routine is None and hasattr(runtime, "get_current_routine"):
            caller_routine = runtime.get_current_routine()
        if caller_routine is None and run is not None:
            caller_routine = run.get_routine(run.default_id)

        def routine_reply():
            return getattr(caller_routine, "reply", {}) or {}

        def routine_waited():
            return getattr(caller_routine, "waited", ()) or ()

        undefined = getattr(self.env, "undefined", jinja2.StrictUndefined)
        reply = LiveReplyProxy(routine_reply, undefined)
        waited = LiveWaitedProxy(routine_waited, undefined)
        result = jinja2.utils.Namespace()
        output_size = [0]
        original_printer = dict(context_vars).get("printer")
        if isinstance(original_printer, LiveGetStatusWrapper):
            printer = original_printer
        elif original_printer is not None and hasattr(original_printer, "lookup_object"):
            printer = LiveGetStatusWrapper(original_printer)
        else:
            # Preserve a user-provided test/dummy printer as-is.  It may itself
            # implement live lookup semantics.
            printer = original_printer

        def boundary_clear():
            if hasattr(printer, "clear_cache"):
                printer.clear_cache()

        def dispatch(text):
            raw = str(text).strip()
            if not raw or raw.startswith(";"):
                return ""
            output_size[0] += len(raw)
            if output_size[0] > _MAX_MANAGED_OUTPUT:
                raise jinja2.exceptions.SecurityError(
                    "managed template output is too large")
            _unused_parse, guard_rendered_commands = _frontend_api()
            diagnostics = guard_rendered_commands(raw)
            if diagnostics:
                diag = diagnostics[0]
                raise OrderedTemplateError("%s: %s" % (diag.code, diag.message))
            boundary_clear()
            if hasattr(caller_routine, "command"):
                caller_routine.command = raw
            value = runtime.dispatch_command_for_routine(caller_routine, raw)
            if isinstance(value, Mapping) and hasattr(caller_routine, "reply"):
                caller_routine.reply = dict(value)
            boundary_clear()
            return ""

        def wait(targets):
            boundary_clear()
            value = runtime.wait_for_routine(caller_routine, targets)
            boundary_clear()
            return ""

        def start_child(name, child_index, local_values):
            check_spawn = getattr(runtime, "check_spawn", None)
            if check_spawn is not None:
                check_spawn()
            child = self.children[int(child_index)]
            child_context = _copy_child_context(local_values, context_vars)
            child_context.pop("result", None)
            child_context.pop("reply", None)
            child_context.pop("waited", None)
            child_context["printer"] = printer
            if run is not None and hasattr(run, "start_routine"):
                routine = run.start_routine(name, caller_id=getattr(caller_routine, "id", None))
                if hasattr(runtime, "spawn_child_routine"):
                    runtime.spawn_child_routine(run, routine, child.template, child_context)
                elif hasattr(runtime, "spawn_child_template"):
                    runtime.spawn_child_template(run, routine, child.template, child_context)
                elif hasattr(runtime, "spawn"):
                    runtime.spawn(routine, child.template, child_context)
                else:
                    raise OrderedTemplateError("Runtime cannot schedule managed routines")
            elif hasattr(runtime, "spawn_child"):
                runtime.spawn_child(name, child.template, child_context, caller_routine)
            else:
                raise OrderedTemplateError("Runtime cannot schedule managed routines")
            boundary_clear()
            return ""

        # Exceptions raised by the runtime effects below keep their identity
        # (command errors, contract errors, or genuine internal errors).
        # Anything else escaping the generator came from evaluating Jinja
        # itself, which stock Klipper reports as a command error.
        effect_errors = []

        def effect(func, *args):
            try:
                return func(*args)
            except Exception as exc:
                effect_errors[:] = [exc]
                raise

        @_pass_context
        def gco_spawn(jinja_context, name, child_index, local_values):
            # pass_context is used for compatibility with Jinja helpers, but
            # local_values is intentionally explicit: loop variables and local
            # assignment slots are Python locals in generated Jinja code and
            # cannot be recovered reliably from Context.vars after a yield.
            return effect(start_child, name, child_index, local_values)

        @_pass_context
        def gco_dispatch(jinja_context, text):
            return effect(dispatch, text)

        @_pass_context
        def gco_wait(jinja_context, targets):
            return effect(wait, targets)

        execution = dict(context_vars)
        execution.update(
            {
                "result": result,
                "reply": reply,
                "waited": waited,
                "printer": printer,
                "_gco_dispatch": gco_dispatch,
                "_gco_wait": gco_wait,
                "_gco_spawn": gco_spawn,
            }
        )
        # Iterating the generator is the key distinction from stock render():
        # each transformed Output node is consumed only after prior effects.
        generator = self.template.generate(execution)
        while True:
            try:
                next(generator)
            except StopIteration:
                break
            except Exception as exc:
                if effect_errors and exc is effect_errors[0]:
                    raise
                raise OrderedTemplateError(str(exc)) from exc
            check = getattr(runtime, "check_execution", None)
            if check is not None:
                check(run, caller_routine)

        data = _namespace_data(result)
        if caller_routine is not None and hasattr(caller_routine, "result"):
            caller_routine.result = data
        return data


def _copy_child_context(local_values, parent_context):
    values = dict(parent_context)
    if isinstance(local_values, Mapping):
        for key, value in local_values.items():
            if key not in _INTERNAL_NAMES:
                values[key] = _snapshot_plain(value)
    return values


class OrderedTemplateCompiler:
    """Parse, validate and compile a literal-control Jinja macro once."""

    def __init__(self, env: Optional[jinja2.Environment] = None):
        if env is None:
            self.env = jinja2.Environment(
                undefined=jinja2.StrictUndefined,
                autoescape=False,
                keep_trailing_newline=True,
            )
        else:
            # Klipper intentionally uses permissive Undefined for legacy
            # macros.  Managed routines require missing reply/waited fields to
            # fail unless the template explicitly applies |default, so compile
            # them in an overlay that retains Klipper's sandbox, delimiters,
            # filters, globals, and tests while changing only Undefined.
            self.env = env.overlay(undefined=jinja2.StrictUndefined)

    def compile(self, source: str, filename: str = "<macro>") -> OrderedTemplate:
        """Compile an explicitly selected ordered source, even without controls."""
        parse, _unused_guard = _frontend_api()
        report = parse(source, filename=filename, template=True)
        if not report.ok:
            diagnostic = next(d for d in report.diagnostics if d.severity == "error")
            raise OrderedTemplateError(
                "Macro syntax error [%s] at line %s: %s"
                % (diagnostic.code, diagnostic.line, diagnostic.message)
            )
        # Dispatch boundaries are complete physical command lines. Inline
        # statements can otherwise split e.g. "G1 X{% if ... %}10{% endif %}"
        # into several incomplete commands. Reject this unsupported form before
        # any effect, while leaving legacy templates entirely unchanged.
        statement_lines, output_lines = set(), set()
        in_statement = False
        for lineno, kind, value in self.env.lex(source):
            if kind == "block_begin":
                in_statement = True
            if in_statement:
                statement_lines.update(range(lineno, lineno + value.count('\n') + 1))
            elif kind == "data":
                output_lines.update(lineno + offset for offset, text in enumerate(value.split('\n')) if text.strip())
            elif kind == "variable_begin":
                output_lines.add(lineno)
            if kind == "block_end":
                in_statement = False
        mixed = statement_lines & output_lines
        if mixed:
            raise OrderedTemplateError(
                "Managed Jinja statements must occupy separate command lines "
                "(line %d)" % min(mixed))

        try:
            tree = self.env.parse(source, name=filename)
        except Exception as exc:
            raise OrderedTemplateError("Jinja syntax error: %s" % exc) from exc

        # The structural frontend checks composition when it finds controls.
        # Ordered macros without controls need the same restriction: external
        # templates have not been transformed into command dispatch boundaries.
        for item in tree.find_all((nodes.Include, nodes.Import,
                                   nodes.FromImport, nodes.Extends)):
            raise OrderedTemplateError(
                "E_TEMPLATE_COMPOSITION: managed templates may not include, "
                "import or extend templates (line %s)" % item.lineno)

        control_by_line = {}

        def register(node):
            if node.kind == "routine":
                control_by_line[node.line] = node
                if node.end_line is not None:
                    control_by_line[node.end_line] = type("End", (), {"kind": "end", "line": node.end_line})()
                for child in node.body:
                    register(child)
            elif node.kind in ("wait", "command", "template_source"):
                if node.kind == "wait":
                    control_by_line[node.line] = node
                for child in node.body:
                    register(child)

        for top in report.nodes:
            register(top)

        children: List[_Child] = []

        def snapshot_expression(names):
            pairs = [nodes.Pair(nodes.Const(name), nodes.Name(name, "load")) for name in names]
            return nodes.Dict(pairs)

        def free_names(body_items):
            child_tree = nodes.Template(copy.deepcopy(list(body_items)))
            child_tree.set_environment(self.env)
            return sorted(meta.find_undeclared_variables(child_tree) - _INTERNAL_NAMES)

        def transform(body: Sequence[nodes.Node]) -> List[nodes.Node]:
            split = _split_output_nodes(body)
            transformed: List[nodes.Node] = []
            index = 0
            while index < len(split):
                item = split[index]
                if isinstance(item, nodes.If):
                    item.body = transform(item.body)
                    for branch in getattr(item, "elif_", ()):
                        branch.body = transform(branch.body)
                    item.else_ = transform(item.else_)
                    transformed.append(item)
                    index += 1
                    continue
                if isinstance(item, nodes.For):
                    item.body = transform(item.body)
                    item.else_ = transform(item.else_)
                    transformed.append(item)
                    index += 1
                    continue
                if isinstance(item, (nodes.With, nodes.Scope)):
                    item.body = transform(item.body)
                    transformed.append(item)
                    index += 1
                    continue
                if isinstance(item, (nodes.FilterBlock, nodes.CallBlock)):
                    raise OrderedTemplateError(
                        "Managed templates do not support output-producing "
                        "filter/call blocks (line %s)" % item.lineno)

                text = _static_output_text(item)
                control = control_by_line.get(getattr(item, "lineno", -1)) if text is not None else None
                # The reference frontend relabels a matched START as
                # ``routine`` while attaching its body/end_line.  Accept both
                # spellings so the compiler also works with a future parser
                # that keeps the lexical ``start`` kind.
                if control is not None and control.kind in ("start", "routine"):
                    body_items: List[nodes.Node] = []
                    cursor = index + 1
                    found = False
                    while cursor < len(split):
                        candidate = split[cursor]
                        ctext = _static_output_text(candidate)
                        ccontrol = control_by_line.get(getattr(candidate, "lineno", -1)) if ctext is not None else None
                        if ccontrol is not None and ccontrol.kind == "end":
                            found = True
                            break
                        body_items.append(candidate)
                        cursor += 1
                    if not found:
                        raise OrderedTemplateError("START at line %s has no matching END" % control.line)
                    child_names = free_names(body_items)
                    child_body = transform(body_items)
                    child_tree = nodes.Template(child_body, lineno=control.line)
                    child_code = self.env.compile(child_tree)
                    child_template = jinja2.Template.from_code(
                        self.env, child_code, self.env.make_globals(None), None
                    )
                    child = OrderedTemplate(self.env, child_template, [], source="")
                    # Child templates can themselves contain children.  The
                    # recursive transformation above appended them to the
                    # shared list only after this point, so attach the child
                    # runner's entries via a temporary list below.
                    child_index = len(children)
                    children.append(_Child(control.name, child, control.line))
                    call = nodes.Call(
                        nodes.Name("_gco_spawn", "load"),
                        [nodes.Const(control.name), nodes.Const(child_index),
                         snapshot_expression(child_names)],
                        [], None, None,
                    )
                    transformed.append(nodes.Output([nodes.MarkSafeIfAutoescape(call)], lineno=control.line))
                    index = cursor + 1
                    continue
                if control is not None and control.kind == "wait":
                    call = nodes.Call(
                        nodes.Name("_gco_wait", "load"),
                        [nodes.Const(control.targets)],
                        [], None, None,
                    )
                    transformed.append(nodes.Output([nodes.MarkSafeIfAutoescape(call)], lineno=control.line))
                    index += 1
                    continue
                if isinstance(item, nodes.Output):
                    # Whitespace-only output is retained as a no-op rather than
                    # sent to Klipper.  Nonempty output is one complete command
                    # boundary; rendered embedded newlines are guarded in the
                    # callback before any line reaches the dispatcher.
                    if text is None or text.strip():
                        call = nodes.Call(
                            nodes.Name("_gco_dispatch", "load"),
                            [nodes.Concat(item.nodes)],
                            [], None, None,
                        )
                        transformed.append(nodes.Output([nodes.MarkSafeIfAutoescape(call)], lineno=item.lineno))
                    index += 1
                    continue
                transformed.append(item)
                index += 1
            return transformed

        tree = copy.deepcopy(tree)
        tree.body = transform(tree.body)
        code = self.env.compile(tree)
        template = jinja2.Template.from_code(self.env, code, self.env.make_globals(None), None)
        return OrderedTemplate(self.env, template, children, source=source)


# Names used by adapters/tests that called the earlier implementation.
OrderedMacroCompiler = OrderedTemplateCompiler
OrderedMacroRunner = OrderedTemplate


__all__ = [
    "LiveReplyProxy",
    "LiveWaitedProxy",
    "LiveGetStatusWrapper",
    "MacroRecursionTracker",
    "OrderedTemplate",
    "OrderedTemplateCompiler",
    "OrderedMacroCompiler",
    "OrderedMacroRunner",
    "OrderedTemplateError",
]
