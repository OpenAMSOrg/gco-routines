"""PAUSE and cancel during the implicit end-of-file join.

The implicit final WAIT at EOF behaves like an explicit WAIT line: it holds
the G-code mutex (children spawned before EOF are admitted) with
``cmd_from_sd`` set, so Klipper's ``do_pause()`` never spins against it.
Real reactor, dispatcher, virtual-SD worker and pause_resume extra.
"""
from types import SimpleNamespace

from extras import pause_resume as pause_resume_module

from test_virtual_sd_lifecycle import _LoadCommand, _virtual_sd

SOURCE = "START NAME=tail\nBG\nBG\nEND\n"   # no explicit WAIT: EOF join


def _tail_print(env, directory, log):
    reactor, gcode = env.reactor, env.gcode
    env.gcode.is_fileinput = False
    env.printer.lookup_object('gcode_io').is_fileinput = False
    mutex = gcode.get_mutex()

    def background(gcmd):
        log.append("BG start")
        log.append(("inside", len(mutex.inside())))
        reactor.pause(reactor.monotonic() + .030)
        log.append("BG end")
    gcode.register_command("BG", background)
    gcode.register_command("CLEANUP", lambda g: log.append("cleanup"))
    for command in ("SAVE_GCODE_STATE", "RESTORE_GCODE_STATE"):
        gcode.register_command(command, lambda g: None)
    (directory / "job.gcode").write_text(SOURCE)
    # Real Klipper always has an on_error_gcode template.
    vsd = _virtual_sd(env, directory,
                      on_error_gcode=SimpleNamespace(render=lambda: "CLEANUP"))
    states = []
    vsd.print_stats.note_start = lambda: states.append("start")
    vsd.print_stats.note_complete = lambda: states.append("complete")
    vsd.print_stats.note_pause = lambda: states.append("paused")
    vsd.print_stats.note_error = lambda error: states.append("error")
    vsd.print_stats.note_cancel = lambda: states.append("cancelled")
    config = SimpleNamespace(get_printer=lambda: env.printer,
                             getfloat=lambda name, default=None: default)
    pause_resume = pause_resume_module.PauseResume(config)
    env.printer.add_object("pause_resume", pause_resume)
    pause_resume.handle_connect()
    env.manager.adapter._hook_pause_resume(pause_resume)
    vsd.cmd_M23(_LoadCommand("job.gcode"))
    vsd.work_timer = reactor.register_timer(vsd.work_handler, reactor.NOW)
    return vsd, states, pause_resume


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


def _later(env, delay, action):
    reactor = env.reactor

    def fire(eventtime):
        action()
        return reactor.NEVER
    reactor.register_timer(fire, reactor.monotonic() + delay)


def test_console_pause_during_eof_join_waits_for_join_then_pauses(klippy_env,
                                                                  tmp_path):
    env = klippy_env
    log, errors = [], []
    vsd, states, pause_resume = _tail_print(env, tmp_path, log)

    def pause():
        # An unbound greenlet, as console/webhook PAUSE requests are.
        log.append("pause-requested")
        try:
            env.gcode.run_script("PAUSE")
        except Exception as exc:
            errors.append(exc)
        log.append("pause-returned")
    _later(env, .010, pause)
    _run(env, lambda: "pause-returned" in log and vsd.work_timer is None)

    assert "pause-returned" in log, "PAUSE hung against the EOF join"
    assert not errors and not env.printer.is_shutdown()
    # Children spawned before EOF are admitted while the join holds the mutex.
    assert log.count(("inside", 2)) == 2 and log.count("BG end") == 2
    # PAUSE queued until the join finished; the print ended complete and
    # PAUSE then applied to the idle printer (paused, nothing to resume).
    assert log.index("pause-requested") < log.index("BG end")
    assert log.index("pause-returned") > len(log) - 2
    assert states == ["start", "complete"]
    assert pause_resume.is_paused and not pause_resume.sd_paused
    run = env.manager.runs["sd_print"]
    assert run.fault is None
    assert all(r.state == "completed" for r in run.routines.values())
    assert not vsd.cmd_from_sd and env.gcode.get_mutex().inside() == ()


def test_runout_pause_command_during_eof_join_returns_immediately(klippy_env,
                                                                  tmp_path):
    env = klippy_env
    log = []
    vsd, states, pause_resume = _tail_print(env, tmp_path, log)

    def runout():
        # filament_switch_sensor calls this directly, without the mutex.
        pause_resume.send_pause_command()
        log.append("send-pause-returned")
    _later(env, .010, runout)
    _run(env, lambda: "send-pause-returned" in log and vsd.work_timer is None)

    assert log.index("send-pause-returned") < log.index("BG end")
    assert log.count("BG end") == 2 and states == ["start", "complete"]
    assert pause_resume.sd_paused and not env.printer.is_shutdown()
    assert not vsd.cmd_from_sd


def test_api_cancel_during_eof_join_unwinds(klippy_env, tmp_path):
    env = klippy_env
    log = []
    vsd, states, pause_resume = _tail_print(env, tmp_path, log)
    endpoint = env.printer.lookup_object("webhooks")._endpoints["pause_resume/cancel"]

    def cancel():
        endpoint(None)            # Moonraker's print-cancel request
        log.append("cancel-returned")
    _later(env, .010, cancel)
    _run(env, lambda: "cancel-returned" in log and vsd.work_timer is None)

    assert "cancel-returned" in log, "cancel hung against the EOF join"
    assert not env.printer.is_shutdown()
    run = env.manager.runs["sd_print"]
    tail = run.routines[run.names["tail"]]
    assert run.is_cancelled and tail.state == "cancelled"
    # The in-flight native command completes; no later child command runs.
    assert log.count("BG start") == 1 and log.count("BG end") == 1
    assert states[-1] == "cancelled" and "complete" not in states
    assert vsd.current_file is None and not pause_resume.is_paused
    assert not vsd.cmd_from_sd and env.gcode.get_mutex().inside() == ()


def test_shutdown_during_eof_join_unwinds(klippy_env, tmp_path):
    env = klippy_env
    log = []
    vsd, states, pause_resume = _tail_print(env, tmp_path, log)
    _later(env, .010, lambda: env.printer.send_event("klippy:shutdown"))
    _run(env, lambda: vsd.work_timer is None)
    assert vsd.work_timer is None
    run = env.manager.runs["sd_print"]
    assert run.is_cancelled and log.count("BG start") == 1
    assert "complete" not in states
    assert not vsd.cmd_from_sd and env.gcode.get_mutex().inside() == ()
