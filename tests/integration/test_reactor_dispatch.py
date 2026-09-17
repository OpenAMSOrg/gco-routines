# Integration tests for reactor scheduling, dispatcher overlap, and wait graph (I01, I12)

import time
from types import SimpleNamespace
import pytest


def test_complete_api_script_dispatches_in_source_order(klippy_env):
    """A multiline API source must not batch ordinary commands past WAIT."""
    env = klippy_env
    reactor = env.reactor
    gcode = env.gcode
    log = []

    def cmd_BACKGROUND(gcmd):
        log.append("background-start")
        reactor.pause(reactor.monotonic() + 0.030)
        log.append("background-end")

    def cmd_FOREGROUND(gcmd):
        log.append("foreground-start")
        reactor.pause(reactor.monotonic() + 0.010)
        log.append("foreground-end")

    def cmd_AFTER(gcmd):
        log.append("after")

    gcode.register_command("BACKGROUND", cmd_BACKGROUND)
    gcode.register_command("FOREGROUND", cmd_FOREGROUND)
    gcode.register_command("AFTER", cmd_AFTER)

    def invoke(eventtime):
        try:
            gcode.run_script(
                "START NAME=job\n"
                "BACKGROUND\n"
                "END\n"
                "FOREGROUND\n"
                "WAIT ON=job\n"
                "AFTER"
            )
        finally:
            reactor.end()
        return reactor.NEVER

    reactor.register_callback(invoke)
    reactor.run()
    assert log == [
        "foreground-start", "background-start", "foreground-end",
        "background-end", "after",
    ]
    status = env.manager.get_status()
    assert status["run_id"].startswith("api_run")
    assert all(item["state"] == "completed" for item in status["routines"])


def test_ordinary_api_call_does_not_replace_active_managed_run(klippy_env):
    env = klippy_env
    initial = env.manager.get_current_run()
    seen = []
    env.gcode.register_command("PING", lambda gcmd: seen.append("ping"))
    env.gcode.run_script("PING")
    assert seen == ["ping"]
    assert env.manager.get_current_run() is initial


def test_unrelated_external_request_remains_serialized(klippy_env):
    env = klippy_env
    reactor = env.reactor
    gcode = env.gcode
    log = []
    external_scheduled = [False]

    def external(eventtime):
        gcode.run_script("EXTERNAL")
        reactor.end()
        return reactor.NEVER

    def foreground(gcmd):
        log.append("foreground")
        if not external_scheduled[0]:
            external_scheduled[0] = True
            reactor.register_callback(external)
        reactor.pause(reactor.monotonic() + 0.010)

    def background(gcmd):
        log.append("background-start")
        reactor.pause(reactor.monotonic() + 0.020)
        log.append("background-end")

    gcode.register_command("FOREGROUND", foreground)
    gcode.register_command("BACKGROUND", background)
    gcode.register_command("EXTERNAL", lambda gcmd: log.append("external"))

    def managed(eventtime):
        gcode.run_script(
            "START NAME=job\nBACKGROUND\nEND\n"
            "FOREGROUND\nWAIT ON=job"
        )
        log.append("managed-done")
        return reactor.NEVER

    reactor.register_callback(managed)
    reactor.run()
    assert log[-2:] == ["managed-done", "external"]

def test_concurrent_overlap_and_wait(klippy_env):
    """Verify cooperative overlap of background routine and default routine on real SelectReactor."""
    env = klippy_env
    r = env.reactor
    gcode = env.gcode
    mgr = env.manager

    log = []

    def cmd_T0(gcmd):
        log.append(('T0_start', r.monotonic()))
        mgr.driver_api.set_detail(gcmd, device='mmu', reason='seeking_handoff')
        r.pause(r.monotonic() + 0.050)
        mgr.driver_api.set_reply(gcmd, {'lane': 0, 'loaded_mm': 684.5})
        log.append(('T0_end', r.monotonic()))

    def cmd_CLEAN_NOZZLE(gcmd):
        log.append(('CLEAN_start', r.monotonic()))
        r.pause(r.monotonic() + 0.020)
        log.append(('CLEAN_end', r.monotonic()))

    gcode.register_command('T0', cmd_T0)
    gcode.register_command('CLEAN_NOZZLE', cmd_CLEAN_NOZZLE)

    done = [False]

    def test_script(eventtime):
        try:
            lines = [
                'START NAME=filament_change',
                '    T0',
                'END',
                'CLEAN_NOZZLE',
                'WAIT ON=filament_change'
            ]
            env.printer.objects['virtual_sdcard'] = SimpleNamespace(is_cmd_from_sd=lambda: True)
            for l in lines:
                gcode.run_script(l)
            log.append(('DONE', r.monotonic()))
            done[0] = True
        finally:
            r.end()
        return r.NEVER

    r.register_callback(test_script)
    r.run()

    assert done[0], "Test script did not complete"
    events = [e[0] for e in log]
    assert events == ['CLEAN_start', 'T0_start', 'CLEAN_end', 'T0_end', 'DONE']

    # Both CLEAN and T0 started simultaneously
    t_clean_start = [t for e, t in log if e == 'CLEAN_start'][0]
    t_t0_start = [t for e, t in log if e == 'T0_start'][0]
    assert abs(t_clean_start - t_t0_start) < 0.005

    # CLEAN ended before T0 ended
    t_clean_end = [t for e, t in log if e == 'CLEAN_end'][0]
    t_t0_end = [t for e, t in log if e == 'T0_end'][0]
    assert t_clean_end < t_t0_end

    sts = mgr.get_status()
    fc = [rt for rt in sts['routines'] if rt['name'] == 'filament_change'][0]
    assert fc['state'] == 'completed'


def test_completion_before_wait_retains_result(klippy_env):
    """Verify routine completing before caller WAIT is retained and not lost."""
    env = klippy_env
    r = env.reactor
    gcode = env.gcode
    mgr = env.manager

    log = []

    def cmd_QUICK_TASK(gcmd):
        log.append(('QUICK_start', r.monotonic()))
        mgr.driver_api.set_reply(gcmd, {'status': 'fast'})
        log.append(('QUICK_end', r.monotonic()))

    def cmd_LONG_DEFAULT(gcmd):
        log.append(('LONG_start', r.monotonic()))
        r.pause(r.monotonic() + 0.040)
        log.append(('LONG_end', r.monotonic()))

    gcode.register_command('QUICK_TASK', cmd_QUICK_TASK)
    gcode.register_command('LONG_DEFAULT', cmd_LONG_DEFAULT)

    done = [False]

    def test_script(eventtime):
        try:
            lines = [
                'START NAME=quick',
                '    QUICK_TASK',
                'END',
                'LONG_DEFAULT',
                'WAIT ON=quick'
            ]
            env.printer.objects['virtual_sdcard'] = SimpleNamespace(is_cmd_from_sd=lambda: True)
            for l in lines:
                gcode.run_script(l)
            log.append(('DONE', r.monotonic()))
            done[0] = True
        finally:
            r.end()
        return r.NEVER

    r.register_callback(test_script)
    r.run()

    assert done[0]
    events = [e[0] for e in log]
    assert 'QUICK_end' in events
    assert 'LONG_end' in events
    sts = mgr.get_status()
    q = [rt for rt in sts['routines'] if rt['name'] == 'quick'][0]
    assert q['state'] == 'completed'


def test_cycle_rejection_atomically(klippy_env):
    """Verify cyclic dependencies are rejected atomically without deadlock."""
    env = klippy_env
    run = env.manager.get_current_run()

    # Create routines A and B
    rA = run.start_routine("A", caller_id=run.default_id)
    rB = run.start_routine("B", caller_id=run.default_id)

    # A waits on B (valid)
    satisfied = run.wait(rA.id, ["B"])
    assert not satisfied
    assert rA.state == "waiting"

    # B proposes to wait on A (cycle: B -> A -> B)
    with pytest.raises(Exception) as exc_info:
        run.wait(rB.id, ["A"])
    assert "circular dependency" in str(exc_info.value).lower() or "self-wait" in str(exc_info.value).lower()

    # B state must remain running (atomic rollback)
    assert rB.state == "running"


def test_child_to_child_wait(klippy_env):
    """Verify background routine waiting on another background routine."""
    env = klippy_env
    r = env.reactor
    gcode = env.gcode
    mgr = env.manager

    log = []

    def cmd_HEAT(gcmd):
        log.append(('HEAT_start', r.monotonic()))
        r.pause(r.monotonic() + 0.030)
        mgr.driver_api.set_reply(gcmd, {'temp': 220})
        log.append(('HEAT_end', r.monotonic()))

    def cmd_LOAD(gcmd):
        log.append(('LOAD_start', r.monotonic()))
        r.pause(r.monotonic() + 0.020)
        mgr.driver_api.set_reply(gcmd, {'loaded': True})
        log.append(('LOAD_end', r.monotonic()))

    gcode.register_command('HEAT', cmd_HEAT)
    gcode.register_command('LOAD', cmd_LOAD)

    done = [False]

    def test_script(eventtime):
        try:
            # Routine 1: heating
            # Routine 2: filament (waits on heating, then loads)
            # Default: waits on filament
            lines = [
                'START NAME=heating',
                '    HEAT',
                'END',
                'START NAME=filament',
                '    WAIT ON=heating',
                '    LOAD',
                'END',
                'WAIT ON=filament'
            ]
            env.printer.objects['virtual_sdcard'] = SimpleNamespace(is_cmd_from_sd=lambda: True)
            for l in lines:
                gcode.run_script(l)
            log.append(('DONE', r.monotonic()))
            done[0] = True
        finally:
            r.end()
        return r.NEVER

    r.register_callback(test_script)
    r.run()

    assert done[0]
    events = [e[0] for e in log]
    # Ordering must be: HEAT_start -> HEAT_end -> LOAD_start -> LOAD_end -> DONE
    assert events == ['HEAT_start', 'HEAT_end', 'LOAD_start', 'LOAD_end', 'DONE']


def test_bare_wait_orders_by_start_order(klippy_env):
    """Verify bare WAIT orders collected results by START order, not completion order."""
    env = klippy_env
    r = env.reactor
    run = env.manager.get_current_run()

    # Routine A starts first, but finishes LATER
    rA = run.start_routine("A", caller_id=run.default_id)
    # Routine B starts second, but finishes EARLIER
    rB = run.start_routine("B", caller_id=run.default_id)

    # B finishes first
    run.finish_routine(rB.id, {"val": "B"})
    # A finishes second
    run.finish_routine(rA.id, {"val": "A"})

    # Caller does bare WAIT
    run.wait(run.default_id, None)
    default_r = run.get_routine(run.default_id)

    # Must be [A, B]
    assert len(default_r.waited) == 2
    assert default_r.waited[0] == {"val": "A"}
    assert default_r.waited[1] == {"val": "B"}


def test_routine_failure_halts_run(klippy_env):
    """Verify routine failure prevents dependent commands and faults the run."""
    env = klippy_env
    r = env.reactor
    run = env.manager.get_current_run()

    rA = run.start_routine("flaky", caller_id=run.default_id)
    run.fail_routine(rA.id, "MMU cutter jammed")

    # Dependent wait must fail
    with pytest.raises(Exception) as exc_info:
        run.wait(run.default_id, ["flaky"])
    msg = str(exc_info.value).lower()
    assert "faulted" in msg or "dependency did not succeed" in msg
    assert run.fault == "MMU cutter jammed"
