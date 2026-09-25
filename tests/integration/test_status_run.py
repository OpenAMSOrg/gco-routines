"""get_status() keeps reporting a running file print.

A transient API-script or ordered-macro run started during a virtual-SD print
must not displace the print as the run reported to unbound observers
(webhooks, status subscribers).  Real reactor and virtual-SD worker.
"""
from test_command_errors import _run_print, _sd_print
from test_review_regressions import execute, macro


def test_transient_runs_do_not_displace_running_print(klippy_env, tmp_path):
    env = klippy_env
    reactor, gcode, manager = env.reactor, env.gcode, env.manager
    log = []
    gcode.register_command("SLOW", lambda g: reactor.pause(reactor.monotonic() + .010))
    gcode.register_command("M117", lambda g: log.append("m117"))
    macro(env, "ORD", "M117 ordered", render_mode="ordered")
    vsd, states = _sd_print(env, tmp_path, "SLOW\n" * 8)

    def observe(label):
        log.append((label, manager.get_status()["run_id"],
                    manager.get_current_run().run_id, vsd.work_timer is not None))

    def external(eventtime):
        observe("before")
        gcode.run_script("START NAME=x\nEND\nWAIT ON=x")
        observe("after-api")
        gcode.run_script("ORD")
        observe("after-macro")
        return reactor.NEVER
    reactor.register_timer(external, reactor.monotonic() + .015)
    _run_print(env, vsd)
    observe("print-ended")

    assert ("before", "sd_print", "sd_print", True) in log
    assert ("after-api", "sd_print", "sd_print", True) in log
    assert ("after-macro", "sd_print", "sd_print", True) in log
    assert ("print-ended", "sd_print", "sd_print", False) in log
    assert "m117" in log and states == ["start", "complete"]
    # The transient runs executed and completed through their own context.
    api_run = manager.runs["api_run"]
    macro_run = manager.runs["macro_ord"]
    for run in (api_run, macro_run):
        assert all(r.state == "completed" for r in run.routines.values())
    assert manager.runs["sd_print"].routines["sd_print:0"].state == "completed"

    # After the print ended it stays reported until the next run starts.
    assert not execute(env, "START NAME=y\nEND\nWAIT ON=y")
    assert manager.get_status()["run_id"].startswith("api_run")
