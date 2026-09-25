"""Contract violations must be command errors, never Klipper shutdowns.

Klipper's dispatcher (``GCodeDispatch._process_commands``) and webhooks treat
every exception other than ``gcode.error`` as an internal error and call
``invoke_shutdown``.  These tests drive the real reactor, dispatcher and
virtual-SD worker from the pinned checkout.
"""
from types import SimpleNamespace

import pytest

import gco_routines
from gco_routines.integration import CompatibilityError
from test_review_regressions import execute, macro
from test_virtual_sd_lifecycle import _LoadCommand, _virtual_sd


def _sd_print(env, directory, text):
    env.gcode.is_fileinput = False
    env.printer.lookup_object('gcode_io').is_fileinput = False
    (directory / "job.gcode").write_text(text)
    vsd = _virtual_sd(env, directory)
    states = []
    vsd.print_stats.note_start = lambda: states.append("start")
    vsd.print_stats.note_complete = lambda: states.append("complete")
    vsd.print_stats.note_pause = lambda: states.append("paused")
    vsd.print_stats.note_error = lambda error: states.append("error")
    vsd.cmd_M23(_LoadCommand("job.gcode"))
    return vsd, states


def _run_print(env, vsd, timeout=2.0):
    reactor = env.reactor
    vsd.work_timer = reactor.register_timer(vsd.work_handler, reactor.NOW)
    deadline = reactor.monotonic() + timeout

    def poll(eventtime):
        if vsd.work_timer is None or eventtime > deadline:
            reactor.end()
            return reactor.NEVER
        return eventtime + .005
    reactor.register_timer(poll, reactor.monotonic() + .01)
    reactor.run()
    assert vsd.work_timer is None, "print did not finish"


@pytest.mark.parametrize("request_script", [
    "ORD",                                  # ordered macro from an unbound caller
    "START NAME=x\nEND\nWAIT ON=x",         # complete API script with controls
])
def test_new_run_during_live_sd_child_is_command_error(klippy_env, tmp_path,
                                                        request_script):
    env = klippy_env
    reactor, gcode = env.reactor, env.gcode
    events, errors = [], []

    def background(gcmd):
        events.append("bg-start")
        reactor.pause(reactor.monotonic() + .030)
        events.append("bg-end")
    gcode.register_command("BACKGROUND", background)
    gcode.register_command(
        "FOREGROUND", lambda g: reactor.pause(reactor.monotonic() + .005))
    gcode.register_command("AFTER", lambda g: events.append("after"))
    gcode.register_command("M117", lambda g: events.append("unsafe"))
    macro(env, "ORD", "M117 hello", render_mode="ordered")
    vsd, states = _sd_print(env, tmp_path, "START NAME=job\nBACKGROUND\n"
                            "BACKGROUND\nEND\nFOREGROUND\nWAIT ON=job\nAFTER\n")

    def external(eventtime):
        # Arrives while the file's child routine is still live.
        try:
            gcode.run_script(request_script)
        except Exception as exc:
            errors.append(exc)
        return reactor.NEVER
    reactor.register_timer(external, reactor.monotonic() + .002)
    _run_print(env, vsd)

    assert len(errors) == 1
    assert isinstance(errors[0], gcode.error), repr(errors[0])
    assert "Cannot replace a run with active routines" in str(errors[0])
    assert not env.printer.is_shutdown()
    assert "unsafe" not in events
    assert events.count("bg-end") == 2 and events[-1] == "after"
    assert states == ["start", "complete"]
    assert env.manager.runs["sd_print"].fault is None


def test_m23_rejected_source_is_command_error_not_shutdown(klippy_env, tmp_path):
    env = klippy_env
    (tmp_path / "bad.gcode").write_text("G1 X1\nSTART NAME=oops\nG1 X2\n")
    vsd = _virtual_sd(env, tmp_path)
    env.gcode.register_command("M23", vsd.cmd_M23)
    errors = execute(env, "M23 bad.gcode")
    assert len(errors) == 1 and isinstance(errors[0], env.gcode.error)
    assert "E_UNCLOSED_START" in str(errors[0])
    assert not env.printer.is_shutdown()
    assert vsd.current_file is None


def test_ordered_macro_evaluation_error_is_command_error(klippy_env):
    env = klippy_env
    seen = []
    env.gcode.register_command("SHOW", lambda g: seen.append(g.get_commandline()))
    # A missing printer object and a failing expression are template
    # evaluation errors, reported like stock Klipper's legacy render errors.
    macro(env, "MISSING", "SHOW V=a\nSHOW V={printer.no_such_object.value}",
          render_mode="ordered")
    macro(env, "DIVIDE", "SHOW V=b\nSHOW V={1 / 0}", render_mode="ordered")
    for name, first in (("MISSING", "SHOW V=a"), ("DIVIDE", "SHOW V=b")):
        seen.clear()
        errors = execute(env, name)
        assert len(errors) == 1 and isinstance(errors[0], env.gcode.error), errors
        assert seen == [first]
        assert not env.printer.is_shutdown()


def test_internal_error_in_managed_macro_is_not_masked(klippy_env):
    env = klippy_env

    def broken(gcmd):
        raise RuntimeError("device driver bug")
    env.gcode.register_command("BROKEN", broken)
    macro(env, "CALLS_BROKEN", "BROKEN", render_mode="ordered")
    errors = execute(env, "CALLS_BROKEN")
    # Klipper, not the extension, decides how an internal error is handled.
    assert len(errors) == 1 and not isinstance(errors[0], env.gcode.error)
    assert env.printer.is_shutdown()
    run = next(run for run in env.manager.runs.values()
               if run.run_id.startswith("macro_calls_broken"))
    assert run.is_cancelled and "device driver bug" in run.fault


@pytest.mark.parametrize("render_mode,script,recursive_routine", [
    ("ordered", "REC", "macro_rec:0"),        # ordered macro from the API
    (None, "START\nREC\nEND\nWAIT", "api_run:1"),  # legacy macro in a child
])
def test_managed_recursion_is_command_error(klippy_env, render_mode, script,
                                            recursive_routine):
    env = klippy_env
    macro(env, "REC", "REC", render_mode=render_mode)
    errors = execute(env, script)
    assert len(errors) == 1 and isinstance(errors[0], env.gcode.error), errors
    assert ("Macro REC called recursively within routine %s" % recursive_routine
            in str(errors[0]))
    assert not env.printer.is_shutdown()
    # The failed invocation left no recursion entry or greenlet binding.
    assert env.manager.recursion_tracker._stacks == {}
    assert env.manager.get_bound_context() == (None, None)
    assert all(not run.greenlet_to_rid for run in env.manager.runs.values())


def test_non_recursive_helper_still_runs_after_recursion_error(klippy_env):
    env = klippy_env
    seen = []
    env.gcode.register_command("MARK", lambda g: seen.append(g.get("V")))
    macro(env, "REC", "REC", render_mode="ordered")
    macro(env, "HELPER", "MARK V=helper")
    macro(env, "TWICE", "HELPER\nHELPER", render_mode="ordered")
    assert execute(env, "REC")
    assert not execute(env, "TWICE")
    assert seen == ["helper", "helper"]
    assert not env.printer.is_shutdown()


def test_incompatible_setup_is_a_config_error(stock_klippy_env):
    """CompatibilityError from the manager constructor or the connect-time
    hooks is reported as a configuration error, not an internal error."""
    env = stock_klippy_env
    # A macro section loaded before [gco_routines] cannot be validated.
    macro(env, "EARLY", "M117 early")

    class ConfigError(Exception):
        pass
    cfg = SimpleNamespace(get_printer=lambda: env.printer, error=ConfigError)
    with pytest.raises(ConfigError, match=r"must be loaded before \[gcode_macro EARLY\]"):
        gco_routines.load_config(cfg)


def test_connect_time_incompatibility_is_a_config_error(klippy_env):
    env = klippy_env
    env.printer.add_object("virtual_sdcard", SimpleNamespace())
    with pytest.raises(env.printer.config_error, match="lacks _load_file") as exc:
        env.manager.adapter._handle_connect()
    assert not isinstance(exc.value, CompatibilityError)
