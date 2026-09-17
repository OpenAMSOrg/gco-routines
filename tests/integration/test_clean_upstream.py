"""Immutable-checkout and cross-baseline integration tests."""

import hashlib
import json
import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("checkout,pin_name", [
    ("klipper", "klipper-target-pin.json"),
    ("klipper-upstream", "klipper-upstream-pin.json"),
])
def test_pinned_klipper_hashes_match(checkout, pin_name):
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    with open(os.path.join(root, "evidence", pin_name)) as source:
        pin = json.load(source)
    checkout_root = os.path.join(root, "vendor", checkout)
    assert subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=checkout_root, text=True
    ).strip() == pin["commit"]
    for rel_path, expected_hash in pin["selected_file_sha256"].items():
        full_path = os.path.join(checkout_root, rel_path)
        with open(full_path, "rb") as source:
            actual_hash = hashlib.sha256(source.read()).hexdigest()
        assert actual_hash == expected_hash, rel_path


@pytest.mark.parametrize("checkout", ["klipper", "klipper-upstream"])
def test_zero_tracked_file_modifications(checkout):
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    checkout_root = os.path.join(root, "vendor", checkout)
    result = subprocess.run(
        ["git", "diff", "--exit-code"], cwd=checkout_root,
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_current_upstream_runs_managed_api_smoke():
    """Run the extra in a fresh interpreter against the upstream pin."""
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    upstream = os.path.join(root, "vendor", "klipper-upstream", "klippy")
    extra = os.path.join(root, "klippy_extra")
    script = r'''
import os, sys
from types import SimpleNamespace
sys.path[:0] = [%r, %r, %r]
import reactor, klippy, gco_routines
from extras import gcode_macro
rfd, wfd = os.pipe()
r = reactor.SelectReactor()
p = klippy.Printer(r, None, {"gcode_fd": wfd, "debuginput": True})
for timer in list(r._timers):
    callback = (getattr(timer, "underlying_callback", None)
                or getattr(timer, "callback", None))
    if callback == p._connect:
        r.unregister_timer(timer)
p.add_object("gcode_macro", gcode_macro.PrinterGCodeMacro(
    SimpleNamespace(get_printer=lambda: p)))
m = gco_routines.load_config(SimpleNamespace(
    get_printer=lambda: p, error=lambda message: RuntimeError(message)))
g = p.lookup_object("gcode")
p.send_event("klippy:ready")
events = []
def bg(gcmd):
    events.append("bg-start")
    r.pause(r.monotonic() + .005)
    events.append("bg-end")
g.register_command("BG", bg)
g.register_command("FG", lambda gcmd: events.append("fg"))
def invoke(eventtime):
    try:
        g.run_script("START NAME=x\nBG\nEND\nFG\nWAIT ON=x")
    finally:
        r.end()
    return r.NEVER
r.register_callback(invoke)
r.run()
assert events == ["fg", "bg-start", "bg-end"], events
assert all(item["state"] == "completed"
           for item in m.get_status()["routines"])
os.close(rfd); os.close(wfd)
''' % (upstream, extra, root)
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=root,
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_current_upstream_runs_oams_macro_serial_fallback():
    """The portable macro must not dispatch controls when the extra is absent."""
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    upstream = os.path.join(root, "vendor", "klipper-upstream", "klippy")
    macro_path = os.path.join(root, "config", "oams_macros.cfg")
    script = r'''
import os, sys
from types import SimpleNamespace
sys.path[:0] = [%r, %r]
import reactor, klippy, configfile
from extras import gcode_macro
rfd, wfd = os.pipe()
r = reactor.SelectReactor()
p = klippy.Printer(r, None, {"gcode_fd": wfd, "debuginput": True})
for timer in list(r._timers):
    callback = (getattr(timer, "underlying_callback", None)
                or getattr(timer, "callback", None))
    if callback == p._connect:
        r.unregister_timer(timer)
p.add_object("gcode_macro", gcode_macro.PrinterGCodeMacro(
    SimpleNamespace(get_printer=lambda: p)))
g = p.lookup_object("gcode")
g.output_callbacks.clear()
state = {
    "oams_manager": {"current_group": None},
    "toolhead": {"homed_axes": "xyz"},
    "extruder": {"can_extrude": True, "temperature": 220.0},
    "pause_resume": {"is_paused": False},
    "exclude_object": {"current_object": "", "excluded_objects": []},
    "configfile": {"settings": {
        "filament_group t0": {}, "filament_group t1": {},
        "filament_group t2": {}, "filament_group t3": {},
    }},
    "filament_switch_sensor extruder_in": {"filament_detected": True},
    "filament_switch_sensor extruder_out": {"filament_detected": False},
}
for name, values in state.items():
    p.add_object(name, SimpleNamespace(
        get_status=lambda eventtime, values=values: dict(values)))
parsed = configfile.ConfigFileReader().build_fileconfig(
    open(%r).read(), %r)
for section in parsed.sections():
    cfg = configfile.ConfigWrapper(p, parsed, {}, section)
    p.add_object(section, gcode_macro.GCodeMacro(cfg))
events = []
for command in ("RESPOND", "SAVE_GCODE_STATE", "RESTORE_GCODE_STATE",
                "SET_STEPPER_ENABLE", "M83", "G1", "M400", "G4"):
    g.register_command(command, lambda gcmd: events.append(gcmd.get_command()))
def load(gcmd):
    events.append("load")
    state["oams_manager"]["current_group"] = gcmd.get("GROUP")
g.register_command("OAMSM_LOAD_FILAMENT", load)
g.register_command("CLEAN_NOZZLE", lambda gcmd: events.append("clean"))
g.register_command("PAUSE", lambda gcmd: events.append("pause"))
p.send_event("klippy:ready")
assert all(command not in g.ready_gcode_handlers
           for command in ("START", "END", "WAIT"))
errors = []
def invoke(eventtime):
    try:
        g.is_fileinput = False
        p.lookup_object("gcode_io").is_fileinput = False
        g.run_script("T1")
    except Exception as exc:
        errors.append(exc)
    finally:
        r.end()
r.register_callback(invoke)
r.run()
assert not errors, errors
assert events.index("load") < events.index("clean"), events
assert state["oams_manager"]["current_group"] == "T1"
assert "G1" in events
os.close(rfd); os.close(wfd)
''' % (upstream, root, macro_path, macro_path)
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=root,
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_installer_adds_and_removes_only_the_extra_symlink(tmp_path):
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    checkout = tmp_path / "klipper"
    extras = checkout / "klippy" / "extras"
    extras.mkdir(parents=True)
    (extras / "gcode_macro.py").write_text("# fixture\n")
    subprocess.run(["git", "init", "-q"], cwd=checkout, check=True)
    subprocess.run(["git", "add", "klippy/extras/gcode_macro.py"],
                   cwd=checkout, check=True)
    subprocess.run([
        "git", "-c", "user.name=tests", "-c", "user.email=tests@example.invalid",
        "commit", "-qm", "fixture",
    ], cwd=checkout, check=True)
    installer = os.path.join(root, "tools", "install_gco_routines.py")
    subprocess.run([sys.executable, installer, "--klipper", str(checkout)],
                   check=True, capture_output=True, text=True)
    destination = extras / "gco_routines"
    assert destination.is_symlink()
    assert subprocess.run(["git", "diff", "--exit-code"], cwd=checkout).returncode == 0
    subprocess.run([
        sys.executable, installer, "--klipper", str(checkout), "--uninstall",
    ], check=True, capture_output=True, text=True)
    assert not destination.exists() and not destination.is_symlink()
    assert subprocess.run(["git", "diff", "--exit-code"], cwd=checkout).returncode == 0
