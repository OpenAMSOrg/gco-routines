#!/usr/bin/env python3
"""Measure how long SDCARD_PRINT_FILE holds Klipper's reactor for one file.

Runs the vendored Klipper reactor with gco-routines loaded, starts
SDCARD_PRINT_FILE on the given file from a reactor callback (as Moonraker's
job start does through the API), and records the longest gap between the
ticks of a 10 ms timer: the time no timer could run. On the 2026-11-13
incident a Raspberry Pi showed a 4.4 s gap for a 66 MB OrcaSlicer file, and
the AMS units' 4 s host watchdogs fired.

    python tools/reactor_stall_repro.py path/to/file.gcode
"""
import os
import sys
import time
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for path in (os.path.join(ROOT, "vendor", "klipper", "klippy"),
             os.path.join(ROOT, "klippy_extra"), ROOT):
    sys.path.insert(0, path)

import reactor                                         # noqa: E402
import klippy                                          # noqa: E402
import gco_routines                                    # noqa: E402
from extras import gcode_macro, virtual_sdcard         # noqa: E402


class _Executor:
    """virtual_sdcard's file I/O executor, inline (it is not the subject)."""

    def submit(self, func, *args, **kwargs):
        return func(*args, **kwargs)

    def wrap_obj(self, value):
        return value


class _Stats:
    filename = None

    def set_current_file(self, filename):
        self.filename = filename

    def reset(self):
        self.filename = None


class _Load:
    def __init__(self, filename):
        self.filename = filename

    def get(self, name, default=None):
        return self.filename if name == "FILENAME" else default

    def respond_raw(self, message):
        pass

    def error(self, message):
        return RuntimeError(message)


def measure(filepath):
    r_fd, w_fd = os.pipe()
    react = reactor.SelectReactor()
    printer = klippy.Printer(react, None, {"gcode_fd": w_fd,
                                           "debuginput": True})
    for timer in list(react._timers):
        callback = (getattr(timer, "underlying_callback", None)
                    or getattr(timer, "callback", None))
        if callback == printer._connect:
            react.unregister_timer(timer)
    printer.add_object("gcode_macro", gcode_macro.PrinterGCodeMacro(
        SimpleNamespace(get_printer=lambda: printer)))
    manager = gco_routines.load_config(SimpleNamespace(
        get_printer=lambda: printer, error=Exception))
    printer.send_event("klippy:ready")
    vsd = virtual_sdcard.VirtualSD.__new__(virtual_sdcard.VirtualSD)
    vsd.printer, vsd.gcode, vsd.reactor = printer, printer.lookup_object(
        "gcode"), react
    vsd.sdcard_dirname = os.path.dirname(os.path.abspath(filepath))
    vsd.current_file = None
    vsd.file_position = vsd.file_size = 0
    vsd.work_timer = None
    vsd.must_pause_work = vsd.cmd_from_sd = False
    vsd.executor = _Executor()
    vsd.print_stats = _Stats()
    vsd.do_resume = lambda: None
    printer.add_object("virtual_sdcard", vsd)
    manager.adapter._hook_virtual_sdcard(vsd)

    state = {"last": None, "worst": 0.0, "done": False}

    def tick(eventtime):
        now = time.perf_counter()
        if state["last"] is not None:
            state["worst"] = max(state["worst"], now - state["last"])
        state["last"] = now
        if state["done"]:
            react.end()
            return react.NEVER
        return eventtime + 0.010

    def start(eventtime):
        began = time.perf_counter()
        vsd.cmd_SDCARD_PRINT_FILE(_Load(os.path.basename(filepath)))
        state["load_s"] = time.perf_counter() - began
        state["done"] = True

    react.register_timer(tick, react.NOW)
    react.register_callback(start, react.monotonic() + 0.1)
    react.run()
    os.close(r_fd)
    os.close(w_fd)
    return state


if __name__ == "__main__":
    result = measure(sys.argv[1])
    print("SDCARD_PRINT_FILE took %.3f s; the reactor's longest gap was "
          "%.3f s" % (result["load_s"], result["worst"]))
