"""Focused adapter tests; full scheduling uses the pinned Klipper reactor."""
import pytest

pytest.importorskip("greenlet")

from types import SimpleNamespace

from tests.fakes_klippy import FakePrinter
from klippy_extra.gco_routines import GcoRoutinesManager
from klippy_extra.gco_routines.integration import (
    CompatibilityError, IntegrationAdapter, validate_compatibility,
)


class RuntimeStub:
    def __init__(self, printer):
        self.printer = printer
        self.runs = {}
        self.cancelled = []

    def cancel_active_runs(self, message):
        self.cancelled.append(message)


def test_fake_dispatcher_opt_in_is_compatible():
    printer = FakePrinter()
    report = validate_compatibility(printer.lookup_object("gcode"))
    assert report["known_baseline"]


def test_unknown_dispatcher_fails_closed():
    printer = FakePrinter()
    printer.lookup_object("gcode").gco_routines_compatibility = False
    with pytest.raises(CompatibilityError):
        IntegrationAdapter(printer, RuntimeStub(printer)).install()


def test_adapter_keeps_ordinary_dispatch_and_rejects_tty_controls():
    printer = FakePrinter()
    GcoRoutinesManager(SimpleNamespace(
        get_printer=lambda: printer,
        error=lambda message: RuntimeError(message),
    ))
    seen = []
    printer.lookup_object("gcode").register_command("M117", lambda g: seen.append(g.get_commandline()))
    printer.lookup_object("gcode").run_script("M117 hello")
    assert seen == ["M117 hello"]
    # A direct _process_commands call has need_ack=True and represents PTY input.
    with pytest.raises(RuntimeError, match="complete file/API"):
        printer.lookup_object("gcode")._process_commands(["WAIT"], need_ack=True)
    with pytest.raises(RuntimeError, match="E_TRANSPORT"):
        printer.lookup_object("gcode")._process_commands(
            ["N42 WAIT"], need_ack=True)


def test_lifecycle_event_cancels_runs():
    printer = FakePrinter()
    runtime = RuntimeStub(printer)
    IntegrationAdapter(printer, runtime).install()
    printer.send_event("klippy:shutdown")
    assert runtime.cancelled
