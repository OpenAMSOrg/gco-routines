"""Optional, hardware-free diagnostic extra. Never installed with production.

The probe yields on Klipper's reactor, records structured observations, and
exercises the public driver reply/detail hooks. It never controls a device or
alters the routine scheduler. Client-side tests, not this journal, judge passes.
"""
import copy
import math
import re


class GcoRoutinesTest:
    protocol_version = 1
    max_events = 256

    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.manager = self.printer.lookup_object("gco_routines", None)
        if self.manager is None:
            raise config.error("Load [gco_routines] before [gco_routines_test]")
        self.events = []
        self.active = 0
        self.epoch = 0
        self.value = 0
        gcode = self.printer.lookup_object("gcode")
        gcode.register_command("_GCO_TEST_RESET", self.reset)
        gcode.register_command("_GCO_TEST_PROBE", self.probe)

    def reset(self, gcmd):
        if self.active:
            raise gcmd.error("Test probes are still unwinding; cannot reset")
        self.events = []
        self.value = 0
        self.epoch += 1

    def record(self, label, phase, value):
        self.events.append(dict(sequence=len(self.events), label=label,
                                phase=phase, value=value,
                                time=self.reactor.monotonic()))

    def probe(self, gcmd):
        label = gcmd.get("LABEL")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", label):
            raise gcmd.error("Invalid test probe LABEL")
        milliseconds = gcmd.get_float("MS", 0., minval=0., maxval=10000.)
        if not math.isfinite(milliseconds):
            raise gcmd.error("Test probe MS must be finite")
        value = gcmd.get_int("VALUE", 0, minval=-1000000, maxval=1000000)
        fail = gcmd.get_int("FAIL", 0, minval=0, maxval=1)
        # Reserve both events, including completions of already active probes.
        if len(self.events) + self.active + 2 > self.max_events:
            raise gcmd.error("Test journal full; reset between cases")
        self.active += 1
        self.value = value
        self.record(label, "begin", value)
        try:
            self.manager.driver_api.set_detail(
                gcmd, device="test_probe", phase="waiting", label=label)
            if milliseconds:
                self.reactor.pause(self.reactor.monotonic() + milliseconds / 1000.)
            if fail:
                self.record(label, "fail", value)
                raise gcmd.error("GCO_TEST_EXPECTED_FAILURE:" + label)
            self.record(label, "end", value)
            self.manager.driver_api.set_reply(gcmd, {"value": value, "label": label})
            self.manager.driver_api.set_detail(
                gcmd, device="test_probe", phase="done", label=label)
        finally:
            self.active -= 1

    def get_status(self, eventtime):
        return dict(protocol_version=self.protocol_version, epoch=self.epoch,
                    active=self.active, value=self.value,
                    events=copy.deepcopy(self.events))


def load_config(config):
    return GcoRoutinesTest(config)
