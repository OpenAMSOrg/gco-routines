"""Run the exact shipped printer diagnostics on pinned, real Klippy (no MCU)."""
from pathlib import Path
from types import SimpleNamespace

import configfile
import pytest
from extras import gcode_macro
from printer_tests.gco_routines_test import GcoRoutinesTest
from printer_tests.suite import CASES, probe, verify
from test_review_regressions import execute

ROOT = Path(__file__).parents[2]


@pytest.fixture
def diagnostic(klippy_env):
    env = klippy_env
    diagnostic = GcoRoutinesTest(SimpleNamespace(get_printer=lambda: env.printer, error=RuntimeError))
    env.printer.add_object("gco_routines_test", diagnostic)
    path = ROOT / "printer_tests/macros.cfg"
    parsed = configfile.ConfigFileReader().build_fileconfig(path.read_text(), str(path))
    for section in parsed.sections():
        if section.startswith("gcode_macro "):
            cfg = configfile.ConfigWrapper(env.printer, parsed, {}, section)
            env.printer.add_object(section, gcode_macro.GCodeMacro(cfg))
    return env, diagnostic


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_shipped_no_motion_cases(diagnostic, case):
    env, diagnostic = diagnostic
    errors_external = []
    if case.external:
        def external(eventtime):
            try:
                env.gcode.run_script(probe("external"))
            except Exception as exc:
                errors_external.append(exc)
            return env.reactor.NEVER
        env.reactor.register_timer(external, env.reactor.monotonic() + .05)
    errors = execute(env, case.script)
    assert not errors_external
    assert diagnostic.active == 0
    verify(case, diagnostic.events, env.manager.get_status(), "\n".join(map(str, errors)))


def test_probe_status_is_detached_and_reply_has_correct_frame(diagnostic):
    env, diagnostic = diagnostic
    assert not execute(env, "WAIT\n" + probe("one", value=4))
    status = diagnostic.get_status(0)
    status["events"][0]["label"] = "mutated"
    assert diagnostic.events[0]["label"] == "one"


def test_suite_can_continue_after_expected_errors_without_restart(diagnostic):
    env, diagnostic = diagnostic
    for case in CASES:
        if case.external:  # Its overlapping second request is tested above.
            continue
        assert not execute(env, "_GCO_TEST_RESET")
        errors = execute(env, case.script)
        assert diagnostic.active == 0
        verify(case, diagnostic.events, env.manager.get_status(), "\n".join(map(str, errors)))


@pytest.mark.parametrize("source", [
    "_GCO_TEST_PROBE LABEL=bad-name", "_GCO_TEST_PROBE LABEL=bad MS=nan",
    "_GCO_TEST_PROBE LABEL=bad MS=inf", "_GCO_TEST_PROBE LABEL=bad MS=10001",
])
def test_probe_rejects_unbounded_or_unsafe_parameters(diagnostic, source):
    env, diagnostic = diagnostic
    assert execute(env, source)
    assert diagnostic.events == []


def test_reset_cannot_discard_pending_completion(diagnostic):
    env, diagnostic = diagnostic
    diagnostic.active = 1
    assert execute(env, "_GCO_TEST_RESET")
    assert diagnostic.epoch == 0


def test_diagnostic_requires_real_plugin_first(stock_klippy_env):
    with pytest.raises(RuntimeError, match="before"):
        GcoRoutinesTest(SimpleNamespace(get_printer=lambda: stock_klippy_env.printer, error=RuntimeError))


def hardware_fixture(diagnostic, group=None, print_state="standby"):
    env, diagnostic = diagnostic
    path = ROOT / "printer_tests/hardware.cfg"
    parsed = configfile.ConfigFileReader().build_fileconfig(path.read_text(), str(path))
    for section in parsed.sections():
        cfg = configfile.ConfigWrapper(env.printer, parsed, {}, section)
        env.printer.add_object(section, gcode_macro.GCodeMacro(cfg))
    state = {"current_group": group}
    env.printer.add_object("oams_manager", SimpleNamespace(get_status=lambda t: dict(state)))
    env.printer.add_object("print_stats", SimpleNamespace(
        get_status=lambda t: {"state": print_state}))
    env.printer.add_object("pause_resume", SimpleNamespace(
        get_status=lambda t: {"is_paused": False}))
    seen = []
    def load(g):
        seen.append("load")
        state["current_group"] = "T1"
    def unload(g):
        seen.append("unload")
        state["current_group"] = None
    env.gcode.register_command("T1", load)
    env.gcode.register_command("SAFE_UNLOAD_FILAMENT", unload)
    env.gcode.register_command("RESPOND", lambda g: seen.append("done"))
    return env, state, seen


def test_hardware_macros_compile_and_reject_other_loaded_bays(diagnostic):
    env, state, seen = hardware_fixture(diagnostic, group="other_bay")
    errors = execute(env, "GCO_TEST_FILAMENT CONFIRM=1")
    assert errors and "another bay" in str(errors[0])
    assert not seen


def test_filament_fixture_only_calls_tone_and_checks_fresh_final_state(diagnostic):
    env, state, seen = hardware_fixture(diagnostic)
    assert not execute(env, "GCO_TEST_FILAMENT CONFIRM=1")
    assert seen == ["load", "unload", "load", "done"]
    assert state["current_group"] == "T1"


def test_failed_unload_prevents_filament_fixture_reload(diagnostic):
    env, state, seen = hardware_fixture(diagnostic)
    env.gcode.register_command("SAFE_UNLOAD_FILAMENT", None)
    def fail(g):
        seen.append("failed_unload")
        raise g.error("Unload failed")
    env.gcode.register_command("SAFE_UNLOAD_FILAMENT", fail)
    assert execute(env, "GCO_TEST_FILAMENT CONFIRM=1")
    assert seen == ["load", "failed_unload"]


@pytest.mark.parametrize("command", ["GCO_TEST_FILAMENT", "GCO_TEST_MOTION", "GCO_TEST_HEATER"])
def test_hardware_tests_reject_active_print_before_device_commands(diagnostic, command):
    env, state, seen = hardware_fixture(diagnostic, print_state="printing")
    assert execute(env, command + " CONFIRM=1")
    assert not seen


def test_filament_fixture_requires_explicit_confirmation(diagnostic):
    env, state, seen = hardware_fixture(diagnostic)
    assert execute(env, "GCO_TEST_FILAMENT")
    assert not seen


def test_all_default_cases_are_device_free():
    allowed = {"START", "END", "WAIT", "_GCO_TEST_PROBE"}
    for case in CASES:
        for line in case.script.splitlines():
            command = line.split()[0].upper()
            assert command in allowed or command.startswith("GCO_TEST_") or command == "N1"
    hardware = (ROOT / "printer_tests/hardware.cfg").read_text()
    import re
    assert set(re.findall(r"^\s+(T\d+)\s*$", hardware, re.M)) == {"T1"}


@pytest.mark.parametrize("filename", ["eof_join.gcode", "child_failure.gcode", "lifecycle.gcode"])
def test_shipped_files_on_real_virtual_sd_worker(diagnostic, filename):
    from test_virtual_sd_lifecycle import _virtual_sd, _LoadCommand
    env, diagnostic = diagnostic
    env.gcode.is_fileinput = False
    env.printer.lookup_object('gcode_io').is_fileinput = False
    vsd = _virtual_sd(env, ROOT / "printer_tests/files")
    env.manager.adapter._hooked_vsd.remove(id(vsd))
    vsd.on_error_gcode = SimpleNamespace(render=lambda: "")
    env.manager.adapter._hook_virtual_sdcard(vsd)
    states = []
    vsd.print_stats.note_start = lambda: states.append("start")
    def complete():
        assert diagnostic.active == 0
        assert any(e["phase"] == "end" for e in diagnostic.events)
        states.append("complete")
    vsd.print_stats.note_complete = complete
    vsd.print_stats.note_pause = lambda: states.append("paused")
    vsd.print_stats.note_error = lambda error: states.append("error")
    vsd.cmd_SDCARD_PRINT_FILE(_LoadCommand(filename))
    vsd.work_timer = env.reactor.register_timer(vsd.work_handler, env.reactor.NOW)
    deadline = env.reactor.monotonic() + 15
    def finished(t):
        if vsd.work_timer is None or t >= deadline:
            env.reactor.end()
            return env.reactor.NEVER
        return t + .02
    env.reactor.register_timer(finished, env.reactor.monotonic() + .02)
    env.reactor.run()
    assert vsd.work_timer is None, "Fixture did not terminate"
    assert not any(e["label"] == "FORBIDDEN" for e in diagnostic.events)
    if filename == "child_failure.gcode":
        assert "complete" not in states and states[-1] == "error"
    else:
        assert states == ["start", "complete"]
        assert all(r["state"] == "completed" for r in env.manager.get_status()["routines"])


def test_shipped_bad_file_is_rejected_before_prefix(diagnostic):
    from test_virtual_sd_lifecycle import _virtual_sd, _LoadCommand
    env, diagnostic = diagnostic
    vsd = _virtual_sd(env, ROOT / "printer_tests/files")
    with pytest.raises(env.gcode.error, match="E_UNCLOSED_START"):
        vsd.cmd_M23(_LoadCommand("rejected_unclosed.gcode"))
    assert not diagnostic.events and vsd.current_file is None
