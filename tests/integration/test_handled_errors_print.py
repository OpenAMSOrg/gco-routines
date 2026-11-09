"""A host command handles the errors of the commands it runs, in a real print.

The shape is OpenAMS's overlapped toolchange: a command (here TC) runs an
ordered macro that feeds in a background routine, cleans the nozzle meanwhile,
joins the feed and reloads. Under ``handled_errors()`` a failure anywhere in it
reaches TC, which pauses the print instead of letting the run fault; RESUME
then continues the file. Real reactor, dispatcher and virtual-SD worker from
the pinned checkout (docs/decisions.md, 2026-11-08).
"""
import types

import pytest
from extras import virtual_sdcard

from test_command_errors import _sd_print
from test_pause_policy import _pause_resume, _run, _run_script_later
from test_review_regressions import macro

BODY = ("START NAME=feed\nFEED\nEND\n"
        "CLEAN\n"
        "WAIT ON=feed\n"
        "RELOAD\n")


def _print_with_toolchange(env, tmp_path, fail, handled=True, absorb=True):
    reactor, gcode = env.reactor, env.gcode
    log = []

    def step(name, delay):
        def handler(gcmd):
            log.append(name + " start")
            reactor.pause(reactor.monotonic() + delay)
            if fail.get(name):
                fail[name] -= 1
                log.append(name + " failed")
                raise gcmd.error("%s failed" % name)
            log.append(name + " end")
        return handler
    gcode.register_command("FEED", step("FEED", .030))
    gcode.register_command("CLEAN", step("CLEAN", .005))
    gcode.register_command("RELOAD", step("RELOAD", .002))
    gcode.register_command("AFTER", lambda g: log.append("AFTER"))
    macro(env, "TC_BODY", BODY, render_mode="ordered")

    def toolchange(gcmd):
        region = env.manager.handled_errors() if handled else _nothing()
        try:
            with region:
                gcode.run_script_from_command("TC_BODY")
        except env.printer.command_error as exc:
            log.append("handled: %s" % exc)
            if not absorb:
                raise
            gcode.run_script_from_command("PAUSE")
            return
        log.append("TC ok")
    gcode.register_command("TC", toolchange)
    vsd, states = _sd_print(env, tmp_path, "TC\nAFTER\n")
    vsd.do_resume = types.MethodType(virtual_sdcard.VirtualSD.do_resume, vsd)
    _pause_resume(env, vsd, log)
    vsd.work_timer = reactor.register_timer(vsd.work_handler, reactor.NOW)
    return vsd, states, log


class _nothing:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


def _routine(env, name):
    run = env.manager.runs["sd_print"]
    return next(r for r in run.routines.values() if r.name == name)


@pytest.mark.parametrize("failing", ["FEED", "RELOAD", "CLEAN"])
def test_a_handled_failure_pauses_the_print_and_resume_finishes_it(klippy_env, tmp_path, failing):
    env = klippy_env
    vsd, states, log = _print_with_toolchange(env, tmp_path, {failing: 1})
    errors = []
    _run(env, lambda: "paused" in log and vsd.work_timer is None)
    run = env.manager.runs["sd_print"]
    assert run.fault is None, run.fault
    assert any(item.startswith("handled: ") and failing in item for item in log)
    assert "AFTER" not in log and states == ["start", "paused"]
    feed = _routine(env, "feed")
    if failing == "FEED":
        # The background feed failed alone; its WAIT reported it to TC.
        assert feed.state == "failed" and feed.retired
    elif failing == "CLEAN":
        # CLEAN failed while the feed ran: the error left the region, which
        # cancelled the feed. Its in-flight command finished; nothing more ran.
        assert feed.state == "cancelled" and feed.retired
        assert "RELOAD start" not in log
    else:
        assert feed.state == "completed"
    _run_script_later(env, "RESUME", .010, errors)
    _run(env, lambda: "AFTER" in log and vsd.work_timer is None)
    assert not errors
    assert log[-1] == "AFTER"
    assert run.fault is None
    assert states == ["start", "paused", "start", "complete"]
    assert not env.printer.is_shutdown()


def test_without_the_region_the_same_failure_ends_the_print(klippy_env, tmp_path):
    env = klippy_env
    vsd, states, log = _print_with_toolchange(env, tmp_path, {"FEED": 1}, handled=False)
    _run(env, lambda: vsd.work_timer is None)
    run = env.manager.runs["sd_print"]
    assert run.fault is not None
    assert "AFTER" not in log and "error" in states


def test_an_error_the_owner_does_not_absorb_still_faults_the_run(klippy_env, tmp_path):
    env = klippy_env
    vsd, states, log = _print_with_toolchange(env, tmp_path, {"RELOAD": 1}, absorb=False)
    _run(env, lambda: vsd.work_timer is None)
    run = env.manager.runs["sd_print"]
    assert run.fault is not None and "RELOAD failed" in run.fault
    assert "AFTER" not in log and "error" in states


def test_a_clean_toolchange_is_unchanged(klippy_env, tmp_path):
    env = klippy_env
    vsd, states, log = _print_with_toolchange(env, tmp_path, {})
    _run(env, lambda: "AFTER" in log and vsd.work_timer is None)
    run = env.manager.runs["sd_print"]
    assert run.fault is None and "TC ok" in log
    # The feed overlapped the nozzle cleaning, as without the region.
    assert log.index("FEED start") < log.index("CLEAN end") < log.index("FEED end")
    assert _routine(env, "feed").state == "completed"
    assert states == ["start", "complete"]
