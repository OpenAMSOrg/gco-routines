"""External request serialization while routine commands are in flight.

The virtual-SD worker takes and releases the G-code mutex per physical line,
and an API/macro owner may unwind while a child command is still executing.
An unrelated request must never run while any command of the run is in
flight.  Real reactor, dispatcher and virtual-SD worker from the pin.
"""
from test_command_errors import _run_print, _sd_print


def _between(log, first, second, item):
    return log.index(first) < log.index(item) < log.index(second)


def test_sd_external_request_waits_for_in_flight_child_command(klippy_env, tmp_path):
    env = klippy_env
    reactor, gcode = env.reactor, env.gcode
    log = []

    def background(gcmd):
        log.append("background-start")
        reactor.pause(reactor.monotonic() + .060)
        log.append("background-end")

    def foreground(gcmd):
        log.append("foreground-start")
        reactor.pause(reactor.monotonic() + .005)
        log.append("foreground-end")

    def external(eventtime):
        log.append("external-request")
        gcode.run_script("EXTERNAL")
        return reactor.NEVER
    gcode.register_command("BACKGROUND", background)
    gcode.register_command("FOREGROUND", foreground)
    gcode.register_command("EXTERNAL", lambda g: log.append("EXTERNAL"))
    gcode.register_command("AFTER", lambda g: log.append("after"))
    vsd, states = _sd_print(env, tmp_path, "START NAME=job\nBACKGROUND\nEND\n"
                            "FOREGROUND\nFOREGROUND\nWAIT ON=job\nAFTER\n")
    reactor.register_timer(external, reactor.monotonic() + .002)
    _run_print(env, vsd)

    # The request arrived while the first line and the child both ran.
    assert _between(log, "background-start", "background-end", "external-request")
    assert not _between(log, "background-start", "background-end", "EXTERNAL")
    assert not _between(log, "foreground-start", "foreground-end", "EXTERNAL")
    assert log[-1] == "after" and log.count("foreground-end") == 2
    assert states == ["start", "complete"]
    mutex = gcode.get_mutex()
    assert not mutex.test() and mutex.inside() == ()


def test_owner_unwinding_keeps_mutex_until_child_command_returns(klippy_env):
    env = klippy_env
    reactor, gcode = env.reactor, env.gcode
    log, errors = [], []
    env.gcode.is_fileinput = False
    env.printer.lookup_object('gcode_io').is_fileinput = False

    def background(gcmd):
        log.append("background-start")
        reactor.pause(reactor.monotonic() + .030)
        log.append("background-end")

    def fail(gcmd):
        raise gcmd.error("foreground failure")
    gcode.register_command("BACKGROUND", background)
    gcode.register_command(
        "FOREGROUND", lambda g: reactor.pause(reactor.monotonic() + .005))
    gcode.register_command("FAIL", fail)
    gcode.register_command("EXTERNAL", lambda g: log.append("EXTERNAL"))

    def managed(eventtime):
        try:
            gcode.run_script("START NAME=job\nBACKGROUND\nEND\nFOREGROUND\nFAIL")
        except Exception as exc:
            errors.append(exc)
        log.append("owner-unwound")
        return reactor.NEVER

    def external(eventtime):
        gcode.run_script("EXTERNAL")
        return reactor.NEVER
    deadline = reactor.monotonic() + 1.0

    def poll(eventtime):
        if ("EXTERNAL" in log and "background-end" in log) or eventtime > deadline:
            reactor.end()
            return reactor.NEVER
        return eventtime + .005
    reactor.register_callback(managed)
    reactor.register_timer(external, reactor.monotonic() + .002)
    reactor.register_timer(poll, reactor.monotonic() + .01)
    reactor.run()

    assert len(errors) == 1 and "foreground failure" in str(errors[0])
    # The owner left while the cancelled child's native command still ran;
    # the unrelated request waited for that command to return.
    assert log.index("owner-unwound") < log.index("background-end")
    assert log.index("background-end") < log.index("EXTERNAL")
    mutex = gcode.get_mutex()
    assert not mutex.test() and mutex.inside() == ()
