"""Pause suspends admitted children at their next command boundary.

A paused run admits no new routines.  An admitted child reaching its next own
command boundary suspends (cooperatively, outside the G-code mutex) until
RESUME/CLEAR_PAUSE, and cancellation wakes and cancels it.  Real reactor,
dispatcher and virtual-SD worker from the pinned checkout.
"""
import json
import os
import types

import jsonschema
import pytest
from extras import virtual_sdcard

from test_command_errors import _sd_print
from test_review_regressions import execute

SCHEMA = os.path.join(os.path.dirname(__file__), "..", "..", "schemas",
                      "status.schema.json")


def _pause_resume(env, vsd, log):
    class PauseResume:
        def cmd_PAUSE(self, gcmd):
            if vsd is not None:
                vsd.do_pause()
            log.append("paused")

        def cmd_RESUME(self, gcmd):
            if vsd is not None:
                vsd.do_resume()
            log.append("resumed")

        def cmd_CLEAR_PAUSE(self, gcmd):
            log.append("cleared")

        def cmd_CANCEL_PRINT(self, gcmd):
            if vsd is not None:
                vsd.do_cancel()
            log.append("cancel-handler")
    pause_resume = PauseResume()
    for command in ("PAUSE", "RESUME", "CLEAR_PAUSE", "CANCEL_PRINT"):
        env.gcode.register_command(command, getattr(pause_resume, "cmd_" + command))
    env.manager.adapter._hook_pause_resume(pause_resume)
    return pause_resume


def _paused_sd_print(env, tmp_path, log):
    reactor, gcode = env.reactor, env.gcode

    def background(gcmd):
        log.append("BG start")
        reactor.pause(reactor.monotonic() + .030)
        log.append("BG end")
    gcode.register_command("BG", background)
    gcode.register_command("FG", lambda g: reactor.pause(reactor.monotonic() + .005))
    gcode.register_command("AFTER", lambda g: log.append("AFTER"))
    vsd, states = _sd_print(env, tmp_path, "START NAME=job\nBG\nBG\nEND\n"
                            "FG\nWAIT ON=job\nAFTER\n")
    vsd.do_resume = types.MethodType(virtual_sdcard.VirtualSD.do_resume, vsd)
    vsd.print_stats.note_cancel = lambda: states.append("cancelled")
    _pause_resume(env, vsd, log)
    vsd.work_timer = reactor.register_timer(vsd.work_handler, reactor.NOW)
    return vsd, states


def _run(env, until, timeout=1.5):
    reactor = env.reactor
    deadline = reactor.monotonic() + timeout

    def poll(eventtime):
        if until() or eventtime > deadline:
            reactor.end()
            return reactor.NEVER
        return eventtime + .005
    reactor.register_timer(poll, reactor.monotonic() + .01)
    reactor.run()


def _job(env):
    return next(r for r in env.manager.get_status()["routines"]
                if r["name"] == "job")


def _run_script_later(env, script, delay, errors=None):
    reactor = env.reactor

    def invoke(eventtime):
        try:
            env.gcode.run_script(script)
        except Exception as exc:
            if errors is None:
                raise
            errors.append(exc)
        return reactor.NEVER
    reactor.register_timer(invoke, reactor.monotonic() + delay)


def test_pause_suspends_child_until_resume(klippy_env, tmp_path):
    env = klippy_env
    log, errors, seen = [], [], {}
    vsd, states = _paused_sd_print(env, tmp_path, log)
    run = env.manager.runs["sd_print"]
    with open(SCHEMA) as f:
        schema = json.load(f)

    def observe(eventtime):
        status = env.manager.get_status()
        jsonschema.validate(instance=status, schema=schema)
        seen["job"] = _job(env)
        seen["worker_stopped"] = vsd.work_timer is None
        return env.reactor.NEVER
    _run_script_later(env, "PAUSE", .002, errors)
    env.reactor.register_timer(observe, env.reactor.monotonic() + .120)
    _run_script_later(env, "RESUME", .150, errors)
    _run(env, lambda: "AFTER" in log and vsd.work_timer is None)

    assert not errors
    assert log.count("BG end") == 2 and log[-1] == "AFTER"
    # The second BG started only after RESUME, not while paused.
    assert log.index("paused") < log.index("resumed")
    assert [i for i, item in enumerate(log) if item == "BG start"][1] > log.index("resumed")
    assert seen["worker_stopped"]
    assert seen["job"]["state"] == "waiting" and seen["job"]["waiting_on"] == []
    assert seen["job"]["detail"] == {"suspended": "paused"}
    assert seen["job"]["command"] == "BG"
    assert run.fault is None
    assert all(r.state == "completed" for r in run.routines.values())
    assert states == ["start", "paused", "start", "complete"]
    assert not env.manager.admissions_paused and not env.manager._suspended
    assert not env.printer.is_shutdown()


def test_cancel_print_wakes_and_cancels_suspended_child(klippy_env, tmp_path):
    env = klippy_env
    log, errors = [], []
    vsd, states = _paused_sd_print(env, tmp_path, log)
    run = env.manager.runs["sd_print"]
    _run_script_later(env, "PAUSE", .002, errors)
    _run_script_later(env, "CANCEL_PRINT", .120, errors)
    _run(env, lambda: "cancel-handler" in log and not run.greenlet_to_rid)

    assert not errors
    assert log.count("BG start") == 1 and "AFTER" not in log
    assert run.is_cancelled and run.fault == "Print cancelled"
    job = run.routines[run.names["job"]]
    assert job.state == "cancelled" and job.error == "Print cancelled"
    assert job.suspended is None and not env.manager._suspended
    assert not run.greenlet_to_rid and vsd.current_file is None
    assert not env.manager.admissions_paused
    assert "cancelled" in states and "complete" not in states
    assert not env.printer.is_shutdown()


@pytest.mark.parametrize("cancel", [
    lambda env: env.manager.cancel_active_runs("Virtual SD file reset"),
    lambda env: env.printer.send_event("klippy:shutdown"),
])
def test_reset_or_shutdown_wakes_suspended_child(klippy_env, tmp_path, cancel):
    env = klippy_env
    log, errors = [], []
    vsd, states = _paused_sd_print(env, tmp_path, log)
    run = env.manager.runs["sd_print"]
    _run_script_later(env, "PAUSE", .002, errors)

    def fire(eventtime):
        cancel(env)
        return env.reactor.NEVER
    env.reactor.register_timer(fire, env.reactor.monotonic() + .120)
    _run(env, lambda: run.is_cancelled and not run.greenlet_to_rid)
    job = run.routines[run.names["job"]]
    assert job.state == "cancelled" and job.suspended is None
    assert not env.manager._suspended and not run.greenlet_to_rid


def test_mutex_holding_waiter_releases_suspended_child(klippy_env):
    """An API script holds the G-code mutex; RESUME cannot arrive until it
    ends, so a child suspended for pause must continue once the script WAITs
    rather than deadlock."""
    env = klippy_env
    reactor, log = env.reactor, []
    _pause_resume(env, None, log)

    def background(gcmd):
        log.append("BG start")
        reactor.pause(reactor.monotonic() + .010)
        log.append("BG end")

    def foreground(gcmd):
        log.append("FG start")
        reactor.pause(reactor.monotonic() + .040)
        log.append("FG end")
        log.append(("job during FG", _job(env)["state"],
                    _job(env)["detail"]))
    env.gcode.register_command("BG", background)
    env.gcode.register_command("FG", foreground)
    env.gcode.register_command("AFTER", lambda g: log.append("AFTER"))
    errors = execute(env, "START NAME=job\nBG\nBG\nEND\nPAUSE\nFG\n"
                          "WAIT ON=job\nAFTER")
    assert not errors
    assert ("job during FG", "waiting", {"suspended": "paused"}) in log
    assert log.index("FG end") < log.index("BG start")
    assert log.count("BG end") == 2 and log[-1] == "AFTER"
    assert all(r["state"] == "completed" for r in env.manager.get_status()["routines"])
    # Pause still rejects new routines until RESUME.
    assert env.manager.admissions_paused
    assert execute(env, "START NAME=x\nEND\nWAIT ON=x")
    assert not env.printer.is_shutdown()


def test_cancel_while_child_queued_for_mutex_never_suspends(klippy_env, tmp_path):
    """Shutdown arrives while the child waits for the mutex behind PAUSE; the
    child must end instead of suspending on a completion nobody completes."""
    env = klippy_env
    log, errors = [], []
    vsd, states = _paused_sd_print(env, tmp_path, log)
    run = env.manager.runs["sd_print"]
    original_pause = env.printer.lookup_object("gcode").ready_gcode_handlers["PAUSE"]

    def pause_then_shutdown(gcmd):
        original_pause(gcmd)
        env.printer.send_event("klippy:shutdown")
    env.gcode.register_command("PAUSE", None)
    env.gcode.register_command("PAUSE", pause_then_shutdown)
    _run_script_later(env, "PAUSE", .002, errors)
    _run(env, lambda: run.is_cancelled and not run.greenlet_to_rid
         and vsd.work_timer is None, timeout=.5)

    job = run.routines[run.names["job"]]
    assert log.count("BG start") == 1 and job.state == "cancelled"
    assert job.suspended is None and not env.manager._suspended
    assert not run.greenlet_to_rid
