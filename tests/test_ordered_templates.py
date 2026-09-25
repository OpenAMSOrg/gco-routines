import builtins
from pathlib import Path

import pytest
from jinja2 import Environment, StrictUndefined

from klippy_extra.gco_routines.templates import OrderedTemplateCompiler, OrderedTemplateError


class FakeRoutine:
    def __init__(self, rid, name=None):
        self.id = rid
        self.name = name
        self.reply = {}
        self.waited = ()
        self.result = None
        self.command = None


class FakeRun:
    default_id = "run:0"

    def __init__(self):
        self.default = FakeRoutine(self.default_id, "default")
        self.routines = {self.default_id: self.default}
        self.by_name = {}
        self.next_id = 1

    def get_routine(self, rid):
        return self.routines[rid]

    def start_routine(self, name, caller_id=None):
        rid = "run:%d" % self.next_id
        self.next_id += 1
        routine = FakeRoutine(rid, name)
        self.routines[rid] = routine
        if name is not None:
            self.by_name[name] = routine
        return routine


class FakeRuntime:
    def __init__(self):
        self.run = FakeRun()
        self.events = []

    def get_current_run(self):
        return self.run

    def dispatch_command_for_routine(self, routine, command):
        self.events.append((routine.id, command))
        # A fake device response, scoped to the current command frame.
        if command.startswith("T"):
            routine.reply = {"lane": int(command[1:])}
        else:
            routine.reply = {}

    def spawn_child_routine(self, run, routine, template, context):
        self.events.append(("spawn", routine.name))
        template.execute(self, context, routine)

    def wait_for_routine(self, routine, targets):
        self.events.append((routine.id, "WAIT", tuple(targets) if targets is not None else None))
        if targets is None:
            selected = [r for r in self.run.routines.values() if r is not routine and r.name != "default"]
            selected.sort(key=lambda r: int(r.id.split(":")[1]))
        else:
            selected = [self.run.by_name[name] for name in targets]
        routine.waited = tuple(r.result or {} for r in selected)


def env():
    return Environment(
        variable_start_string="{",
        variable_end_string="}",
        undefined=StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
    )


def run(source, context=None):
    runtime = FakeRuntime()
    runner = OrderedTemplateCompiler(env()).compile(source)
    assert runner is not None
    result = runner.execute(runtime, context or {}, runtime.run.default)
    return runtime, result


def test_results_are_evaluated_after_the_command_boundary():
    runtime, result = run(
        "START NAME=filament\n"
        "T2\n"
        "{% set result.lane = reply.lane %}\n"
        "END\n"
        "WAIT ON=filament\n"
        "M117 Lane { waited[0].lane }\n"
    )
    assert result == {}
    assert runtime.events == [
        ("spawn", "filament"),
        ("run:1", "T2"),
        ("run:0", "WAIT", ("filament",)),
        ("run:0", "M117 Lane 2"),
    ]
    assert runtime.run.by_name["filament"].result == {"lane": 2}


def test_anonymous_results_follow_start_order_for_bare_wait():
    runtime, _result = run(
        "START\nT1\n{% set result.lane = reply.lane %}\nEND\n"
        "START\nT2\n{% set result.lane = reply.lane %}\nEND\n"
        "WAIT\nM117 { waited[0].lane } { waited[1].lane }\n"
    )
    assert runtime.events[-2] == ("run:0", "WAIT", None)
    assert runtime.events[-1] == ("run:0", "M117 1 2")


def test_conditional_and_loop_scopes_are_preserved():
    runtime, _result = run(
        "{% if enabled %}\n"
        "{% for tool in [0, 1] %}\n"
        "START NAME=tool\nT{tool}\nEND\nWAIT ON=tool\n"
        "{% endfor %}\n{% endif %}",
        {"enabled": True},
    )
    commands = [event for event in runtime.events if isinstance(event, tuple) and len(event) == 2 and event[1].startswith("T")]
    assert commands == [("run:1", "T0"), ("run:2", "T1")]


def test_locals_are_copied_at_start_not_read_later():
    runtime, _result = run(
        "{% set lane = 'before' %}\n"
        "START\nM117 {lane}\nEND\n"
        "{% set lane = 'after' %}\nWAIT\n"
    )
    assert ("run:1", "M117 before") in runtime.events


def test_child_locals_and_future_parent_names_are_not_eagerly_snapshotted():
    runtime, _result = run(
        "START\n"
        "{% set child_local = 'inside' %}\n"
        "M117 {child_local}\n"
        "END\n"
        "{% set future_parent = 'later' %}\n"
        "WAIT\n"
    )
    assert ("run:1", "M117 inside") in runtime.events


def test_missing_fields_are_strict_but_default_filter_is_available():
    runtime, _result = run(
        "START\n"
        "M117 { reply.missing|default('fallback') }\n"
        "END\n"
    )
    assert ("run:1", "M117 fallback") in runtime.events
    with pytest.raises(Exception):
        run("START\nM117 { reply.missing }\nEND\n")


def test_managed_compiler_overlays_klipper_permissive_undefined():
    permissive = Environment(
        variable_start_string="{", variable_end_string="}",
        autoescape=False,
    )
    compiler = OrderedTemplateCompiler(permissive)
    assert compiler.env.undefined is StrictUndefined
    runner = compiler.compile("START\nM117 { missing }\nEND\n")
    runtime = FakeRuntime()
    with pytest.raises(Exception):
        runner.execute(runtime, {}, runtime.run.default)


def test_standalone_fallback_rejects_cross_region_controls(monkeypatch):
    real_import = builtins.__import__

    def without_reference(name, *args, **kwargs):
        if name == "gcoroutines.frontend":
            raise ImportError("standalone extra")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_reference)
    compiler = OrderedTemplateCompiler(env())
    with pytest.raises(OrderedTemplateError, match="E_TEMPLATE_REGION"):
        compiler.compile("{% if enabled %}\nSTART\n{% endif %}\nEND\n")
    with pytest.raises(OrderedTemplateError, match="E_CAPTURED_CONTROL"):
        compiler.compile("{% set text %}\nSTART\nM117 nope\nEND\n{% endset %}")


def test_rendered_embedded_controls_are_rejected_before_dispatch():
    with pytest.raises(OrderedTemplateError, match="E_GENERATED_CONTROL"):
        run(
            "{% set generated = 'ok\\nWAIT' %}\n"
            "START\nM117 {generated}\nEND\n"
        )


def test_single_fps_oams_config_templates_parse_and_tx_can_be_ordered():
    path = Path(__file__).parents[1] / "config" / "oams_macros.cfg"
    sections = []
    name = None
    body = []
    in_gcode = False
    for line in path.read_text().splitlines() + ["[end]"]:
        if line.startswith("["):
            if name is not None and in_gcode:
                sections.append((name, "\n".join(body) + "\n"))
            name, body, in_gcode = line.strip(), [], False
        elif name is not None and line.strip() == "gcode:":
            in_gcode = True
        elif in_gcode:
            body.append(line[4:] if line.startswith("    ") else line)

    compiler = OrderedTemplateCompiler(env())
    for name, source in sections:
        compiler.env.parse(source)
    tx = dict(sections)['[gcode_macro _TX]']
    assert len(compiler.compile(tx, filename='[gcode_macro _TX]').children) == 1


def test_with_scope_dispatches_ordinary_commands():
    runtime, _ = run('WAIT\n{% with value = 7 %}\nM117 {value}\n{% endwith %}')
    assert runtime.events[-1] == ('run:0', 'M117 7')


def test_child_namespace_is_a_private_snapshot():
    runtime, _ = run(
        "{% set local = namespace(value=1) %}\n"
        "START\n{% set local.value = 2 %}\nEND\nWAIT\nM117 {local.value}")
    assert runtime.events[-1] == ('run:0', 'M117 1')


def test_inline_statement_cannot_dispatch_fragments_as_commands():
    source = 'WAIT\nG1 X{% if enabled %}10{% else %}20{% endif %}'
    with pytest.raises(OrderedTemplateError, match='separate command lines'):
        run(source, {'enabled': True})


def test_standalone_ordered_compiler_needs_no_controls(monkeypatch):
    real_import = builtins.__import__
    def without_reference(name, *args, **kwargs):
        if name == 'gcoroutines.frontend':
            raise ImportError('standalone extra')
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', without_reference)
    runtime, _ = run('T1\nM117 {reply.lane}')
    assert runtime.events == [('run:0', 'T1'), ('run:0', 'M117 1')]
    with pytest.raises(OrderedTemplateError, match='E_TEMPLATE_COMPOSITION'):
        run('{% include "external.jinja" %}')


def test_statement_with_trailing_gcode_comment_is_accepted():
    # The trailing ';' comment is dropped before dispatch: not mixed output.
    runtime, _ = run('{% set x = 7 %} ; remember x\nM117 {x}')
    assert runtime.events == [('run:0', 'M117 7')]


def test_inline_statement_inside_a_command_is_still_rejected():
    with pytest.raises(OrderedTemplateError, match='separate command lines'):
        run('G1 X{% if true %}10{% endif %}')
    with pytest.raises(OrderedTemplateError, match='separate command lines'):
        run('M117 a ; b {% set x = 1 %}{x}')
