# Integration tests for ordered Jinja macro compilation and execution (I03, I04, I07)

import os
from types import SimpleNamespace
import jinja2
import pytest
from gco_routines.templates import OrderedTemplateCompiler, MacroRecursionTracker
from extras import gcode_macro


class MacroConfig:
    def __init__(self, printer, name, source):
        self.printer = printer
        self.name = name
        self.source = source

    def get_printer(self):
        return self.printer

    def get_name(self):
        return "gcode_macro %s" % self.name

    def get(self, option, default=None):
        if option == "gcode":
            return self.source
        return default

    def get_prefix_options(self, prefix):
        return []

    def error(self, message):
        return RuntimeError(message)


def test_real_macro_hook_captures_source_and_runs_managed(klippy_env):
    env = klippy_env
    reactor = env.reactor
    events = []

    def background(gcmd):
        events.append("background-start")
        reactor.pause(reactor.monotonic() + 0.020)
        env.manager.driver_api.set_reply(gcmd, {"value": 7})
        events.append("background-end")

    env.gcode.register_command("BACKGROUND", background)
    env.gcode.register_command("FOREGROUND", lambda gcmd: events.append("foreground"))
    env.gcode.register_command(
        "SHOW", lambda gcmd: events.append("show=" + gcmd.get("VALUE")))
    macro = gcode_macro.GCodeMacro(MacroConfig(
        env.printer, "MANAGED_TEST",
        "START NAME=job\nBACKGROUND\n"
        "{% set result.value = reply.value %}\nEND\n"
        "FOREGROUND\nWAIT ON=job\nSHOW VALUE={waited[0].value}",
    ))
    env.printer.add_object("gcode_macro MANAGED_TEST", macro)
    assert macro.template.gco_source.startswith("START")
    assert macro.template.gco_runner is not None

    def invoke(eventtime):
        try:
            env.gcode.run_script("MANAGED_TEST")
        finally:
            reactor.end()
        return reactor.NEVER

    reactor.register_callback(invoke)
    reactor.run()
    assert events == [
        "foreground", "background-start", "background-end", "show=7",
    ]
    assert all(item["state"] == "completed"
               for item in env.manager.get_status()["routines"])


def test_legacy_helper_macro_is_reentrant_across_routines(klippy_env):
    env = klippy_env
    reactor = env.reactor
    events = []

    def slow(gcmd):
        events.append("start")
        reactor.pause(reactor.monotonic() + 0.020)
        events.append("end")

    env.gcode.register_command("SLOW", slow)
    helper = gcode_macro.GCodeMacro(
        MacroConfig(env.printer, "LEGACY_HELPER", "SLOW"))
    env.printer.add_object("gcode_macro LEGACY_HELPER", helper)
    assert helper.template.gco_runner is None

    def invoke(eventtime):
        try:
            env.gcode.run_script(
                "START NAME=a\nLEGACY_HELPER\nEND\n"
                "START NAME=b\nLEGACY_HELPER\nEND\nWAIT"
            )
        finally:
            reactor.end()
        return reactor.NEVER

    reactor.register_callback(invoke)
    reactor.run()
    assert events[:2] == ["start", "start"]
    assert events[2:] == ["end", "end"]

def test_results_jinja_execution(klippy_env):
    """Verify execution of examples/results.jinja with live reply, wait, and formatting."""
    env = klippy_env
    r = env.reactor
    gcode = env.gcode
    mgr = env.manager

    # Jinja env matching Klipper GCodeSandbox
    jinja_env = jinja2.Environment(
        variable_start_string='{', variable_end_string='}',
        undefined=jinja2.StrictUndefined, autoescape=False
    )

    with open(os.path.join(os.path.dirname(__file__), "..", "..", "examples", "results.jinja")) as f:
        script = f.read()

    compiler = OrderedTemplateCompiler(jinja_env)
    runner = compiler.compile(script)
    assert runner is not None, "results.jinja must be compiled as ordered macro"

    dispatched = []

    def cmd_T0(gcmd):
        mgr.driver_api.set_reply(gcmd, {'lane': 0, 'loaded_mm': 684.5})

    def cmd_CLEAN_NOZZLE(gcmd):
        dispatched.append("CLEAN_NOZZLE")

    def cmd_M117(gcmd):
        dispatched.append(f"M117 {gcmd.get_raw_command_parameters()}")

    gcode.register_command('T0', cmd_T0)
    gcode.register_command('CLEAN_NOZZLE', cmd_CLEAN_NOZZLE)
    gcode.register_command('M117', cmd_M117)

    done = [False]

    def test_run(eventtime):
        try:
            curr_routine = mgr.get_current_routine()
            kwparams = {
                'params': {},
                'rawparams': '',
                'printer': env.printer
            }
            runner.execute(mgr, kwparams, curr_routine)
            done[0] = True
        finally:
            r.end()
        return r.NEVER

    r.register_callback(test_run)
    r.run()

    assert done[0]
    assert "CLEAN_NOZZLE" in dispatched
    assert any("Loaded lane 0" in cmd for cmd in dispatched)


def test_macro_reentrancy_concurrent_routines():
    """Verify two different routines can concurrently invoke the same helper macro."""
    tracker = MacroRecursionTracker()

    # Routine 1 enters HELPER
    tracker.enter("run1:1", "HELPER")
    # Routine 2 also enters HELPER concurrently -> must succeed!
    tracker.enter("run1:2", "HELPER")

    # Genuine recursion in Routine 1 -> must fail!
    with pytest.raises(ValueError) as exc:
        tracker.enter("run1:1", "HELPER")
    assert "recursively" in str(exc.value).lower()

    # Routine 1 exits HELPER
    tracker.exit("run1:1", "HELPER")
    # Routine 2 exits HELPER
    tracker.exit("run1:2", "HELPER")


def test_rendered_control_injection_guarded(klippy_env):
    """Verify template injection cannot manufacture START, END, or WAIT commands."""
    env = klippy_env
    r = env.reactor
    mgr = env.manager

    jinja_env = jinja2.Environment(
        variable_start_string='{', variable_end_string='}',
        undefined=jinja2.StrictUndefined, autoescape=False
    )

    script = """
START NAME=a
    T0
END
WAIT ON=a
M117 {params.INJECTED}
"""
    compiler = OrderedTemplateCompiler(jinja_env)
    runner = compiler.compile(script)

    done = [False]
    error = [None]

    def test_run(eventtime):
        try:
            curr = mgr.get_current_routine()
            # Attempt to inject WAIT command via parameter
            kwparams = {
                'params': {'INJECTED': 'safe\nWAIT ON=fake'},
                'rawparams': '',
                'printer': env.printer
            }
            runner.execute(mgr, kwparams, curr)
            done[0] = True
        except Exception as e:
            error[0] = e
        finally:
            r.end()
        return r.NEVER

    r.register_callback(test_run)
    r.run()

    assert not done[0]
    assert error[0] is not None
    assert "E_GENERATED_CONTROL" in str(error[0])


def test_legacy_macro_preserves_full_rendering(klippy_env):
    """Verify macros without literal controls return None from compiler (staying on legacy path)."""
    jinja_env = jinja2.Environment(
        variable_start_string='{', variable_end_string='}',
        undefined=jinja2.StrictUndefined, autoescape=False
    )
    legacy_script = """
G28
M104 S{params.TEMP|default(200)}
G1 X100 Y100
"""
    compiler = OrderedTemplateCompiler(jinja_env)
    runner = compiler.compile(legacy_script)
    assert runner is None, "Legacy macro without controls must not opt into ordered runner"
