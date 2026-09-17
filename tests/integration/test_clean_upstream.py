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
