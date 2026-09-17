# Shared fixtures and harnesses for gco-routines Klippy integration tests

import os
import sys
from types import SimpleNamespace
import pytest

# Run behavioral tests against the exact checkout captured from the target Pi.
TARGET_KLIPPY = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "vendor", "klipper", "klippy")
)
EXTRA_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "klippy_extra")
)
REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)

for p in [TARGET_KLIPPY, EXTRA_PATH, REPO_ROOT]:
    if p not in sys.path:
        sys.path.insert(0, p)

import reactor
import klippy
import gco_routines
from extras import gcode_macro

class TestKlippyEnv:
    def __init__(self, enable_gco=True):
        self.r_fd, self.w_fd = os.pipe()
        self.reactor = reactor.SelectReactor()
        # Mock dummy config file to prevent _connect KeyError
        self.printer = klippy.Printer(self.reactor, None, {'gcode_fd': self.w_fd, 'debuginput': True})
        # Unregister default _connect callback to prevent loading non-existent config
        for t in list(self.reactor._timers):
            cb = getattr(t, 'underlying_callback', None) or getattr(t, 'callback', None)
            if cb == self.printer._connect:
                self.reactor.unregister_timer(t)

        # The real manager deliberately loads/hooks gcode_macro before later
        # macro sections.  Seed the genuine Klipper singleton without needing
        # a complete PrinterConfig in this focused harness.
        macro_cfg = SimpleNamespace(get_printer=lambda: self.printer)
        self.printer.add_object(
            "gcode_macro", gcode_macro.PrinterGCodeMacro(macro_cfg))

        # Load gco_routines manager unless the test deliberately models an
        # unmodified upstream Klipper installation.
        self.manager = None
        if enable_gco:
            self.cfg = SimpleNamespace(
                get_printer=lambda: self.printer,
                error=lambda msg: Exception(msg)
            )
            self.manager = gco_routines.load_config(self.cfg)
        self.gcode = self.printer.lookup_object('gcode')
        self.gcode.output_callbacks.clear()
        self.printer.send_event('klippy:ready')

    def close(self):
        try:
            os.close(self.r_fd)
            os.close(self.w_fd)
        except OSError:
            pass

@pytest.fixture
def klippy_env():
    env = TestKlippyEnv()
    yield env
    env.close()


@pytest.fixture
def stock_klippy_env():
    env = TestKlippyEnv(enable_gco=False)
    yield env
    env.close()
