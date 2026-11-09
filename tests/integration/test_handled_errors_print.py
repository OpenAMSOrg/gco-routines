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
from test_review_regressions import execute, macro

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


# RESUME retries the toolchange (decisions.md, 2026-11-08, legacy rendering in
# recovery): OpenAMS's RESUME runs the same ordered macro again before the
# print continues. A recovery handler starts no routines, so the macro takes
# its serial branch while paused; inside legacy_in_recovery() it renders like a
# legacy macro instead of being refused.
SERIAL_BODY = ("{% if params.SERIAL|default(0)|int %}\n"
               "FEED\nCLEAN\n"
               "{% else %}\n"
               "START NAME=feed\nFEED\nEND\nCLEAN\nWAIT ON=feed\n"
               "{% endif %}\n"
               "RELOAD\n")


def _print_with_resume_retry(env, tmp_path, fail, retry, legacy=True):
    reactor, gcode = env.reactor, env.gcode
    log = []

    def step(name):
        def handler(gcmd):
            log.append(name)
            reactor.pause(reactor.monotonic() + .005)
            if fail.get(name):
                fail[name] -= 1
                raise gcmd.error("%s failed" % name)
        return handler
    for name in ("FEED", "CLEAN", "RELOAD"):
        gcode.register_command(name, step(name))
    gcode.register_command("AFTER", lambda g: log.append("AFTER"))
    macro(env, "TC_BODY", SERIAL_BODY, render_mode="ordered")

    def toolchange(gcmd):
        try:
            with env.manager.handled_errors():
                gcode.run_script_from_command("TC_BODY")
        except env.printer.command_error as exc:
            log.append("handled: %s" % exc)
            gcode.run_script_from_command("PAUSE")
    gcode.register_command("TC", toolchange)
    vsd, states = _sd_print(env, tmp_path, "TC\nAFTER\n")
    vsd.do_resume = types.MethodType(virtual_sdcard.VirtualSD.do_resume, vsd)

    class PauseResume:
        def cmd_PAUSE(self, gcmd):
            vsd.do_pause()
            log.append("paused")

        def cmd_RESUME(self, gcmd):
            region = env.manager.legacy_in_recovery() if legacy else _nothing()
            try:
                with region:
                    gcode.run_script_from_command(retry)
            except env.printer.command_error as exc:
                log.append("retry failed: %s" % exc)
                raise
            vsd.do_resume()
            log.append("resumed")

        def cmd_CLEAR_PAUSE(self, gcmd):
            pass

        def cmd_CANCEL_PRINT(self, gcmd):
            vsd.do_cancel()
    pause_resume = PauseResume()
    for command in ("PAUSE", "RESUME", "CLEAR_PAUSE", "CANCEL_PRINT"):
        gcode.register_command(command, getattr(pause_resume, "cmd_" + command))
    env.manager.adapter._hook_pause_resume(pause_resume)
    vsd.work_timer = reactor.register_timer(vsd.work_handler, reactor.NOW)
    return vsd, states, log


def test_resume_reruns_an_ordered_macro_inside_legacy_in_recovery(klippy_env, tmp_path):
    env = klippy_env
    vsd, states, log = _print_with_resume_retry(
        env, tmp_path, {"FEED": 1}, "TC_BODY SERIAL=1")
    _run(env, lambda: "paused" in log and vsd.work_timer is None)
    run = env.manager.runs["sd_print"]
    assert run.fault is None
    log[:] = []
    errors = []
    _run_script_later(env, "RESUME", .010, errors)
    _run(env, lambda: "AFTER" in log and vsd.work_timer is None)
    assert not errors
    # The retry ran serially inside RESUME, then the file went on.
    assert log == ["FEED", "CLEAN", "RELOAD", "resumed", "AFTER"]
    assert run.fault is None and states[-1] == "complete"


def test_without_the_region_resume_refuses_the_ordered_macro(klippy_env, tmp_path):
    env = klippy_env
    vsd, states, log = _print_with_resume_retry(
        env, tmp_path, {"FEED": 1}, "TC_BODY SERIAL=1", legacy=False)
    _run(env, lambda: "paused" in log and vsd.work_timer is None)
    errors = []
    _run_script_later(env, "RESUME", .010, errors)
    _run(env, lambda: bool(errors))
    assert "Recovery macros cannot start managed routines" in str(errors[0])
    assert "FEED" not in log[log.index("paused"):] and "AFTER" not in log


def test_in_the_region_a_control_in_the_output_is_still_refused_before_anything_runs(
        klippy_env, tmp_path):
    env = klippy_env
    # The retry takes the branch with START: a recovery handler never starts
    # routines, so the whole rendered body is refused and nothing of it runs.
    vsd, states, log = _print_with_resume_retry(
        env, tmp_path, {"FEED": 1}, "TC_BODY")
    _run(env, lambda: "paused" in log and vsd.work_timer is None)
    log[:] = []
    errors = []
    _run_script_later(env, "RESUME", .010, errors)
    _run(env, lambda: bool(errors))
    assert "Recovery scripts cannot start managed routines" in str(errors[0])
    assert not [item for item in log if item in ("FEED", "CLEAN", "RELOAD")]
    assert env.manager.runs["sd_print"].fault is None


def test_outside_recovery_the_region_changes_nothing(klippy_env):
    env = klippy_env
    seen = []
    env.gcode.register_command("MARK", lambda g: seen.append("MARK"))
    macro(env, "OVERLAP", "START NAME=job\nMARK\nEND\nWAIT ON=job\n",
          render_mode="ordered")
    with env.manager.legacy_in_recovery():
        assert not execute(env, "OVERLAP")
    assert seen == ["MARK"]
