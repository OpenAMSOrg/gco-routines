"""Tiny Klipper-shaped fakes for adapter tests.

These are intentionally limited to dispatcher/lifecycle seams; they are not a
replacement for the pinned Klipper reactor in integration tests.
"""
from __future__ import annotations

import re
from types import SimpleNamespace


class FakeMutex:
    def __init__(self):
        self.locked = False

    def test(self):
        return self.locked

    def __enter__(self):
        if self.locked:
            raise RuntimeError("fake mutex would block")
        self.locked = True
        return self

    def __exit__(self, *args):
        self.locked = False


class FakeCompletion:
    def __init__(self):
        self.done = False
        self.value = None

    def complete(self, value=True):
        self.done, self.value = True, value

    def wait(self, *args, **kwargs):
        return self.value


class FakeReactor:
    NOW = 0.0
    NEVER = 10**20

    def __init__(self):
        self.callbacks = []

    def mutex(self):
        return FakeMutex()

    def completion(self):
        return FakeCompletion()

    def register_callback(self, callback, *args, **kwargs):
        self.callbacks.append(callback)
        return FakeCompletion()

    def monotonic(self):
        return 0.0

    def pause(self, *args, **kwargs):
        return None


class FakeGCodeCommand:
    def __init__(self, gcode, commandline, params=None, need_ack=False):
        self.gcode = gcode
        self._commandline = commandline
        self._command = commandline.split()[0].upper() if commandline else ""
        self._params = {str(k).upper(): str(v) for k, v in (params or {}).items()}
        self._need_ack = need_ack

    def get_command(self):
        return self._command

    def get_commandline(self):
        return self._commandline

    def get_command_parameters(self):
        return dict(self._params)

    def get_raw_command_parameters(self):
        return self._commandline[len(self._command):].strip()

    _missing = object()

    def get(self, name, default=_missing, **kwargs):
        key = str(name).upper()
        if key not in self._params:
            if default is self._missing:
                raise RuntimeError("missing " + key)
            return default
        return self._params[key]

    def error(self, message):
        return RuntimeError(message)


class FakeGCode:
    gco_routines_compatibility = True
    error = RuntimeError

    def __init__(self, printer):
        self.printer = printer
        self.mutex = FakeMutex()
        self.base_gcode_handlers = {}
        self.ready_gcode_handlers = {}
        self.gcode_handlers = self.ready_gcode_handlers
        self.gcode_help = {}
        self.output_callbacks = []
        self.gcode_io = None

    def get_mutex(self):
        return self.mutex

    def register_command(self, cmd, func, when_not_ready=False, desc=None):
        cmd = cmd.upper()
        if func is None:
            old = self.ready_gcode_handlers.pop(cmd, None)
            self.base_gcode_handlers.pop(cmd, None)
            return old
        if cmd in self.ready_gcode_handlers:
            raise RuntimeError("gcode command already registered")
        self.ready_gcode_handlers[cmd] = func
        if when_not_ready:
            self.base_gcode_handlers[cmd] = func
        if desc:
            self.gcode_help[cmd] = desc

    def run_script_from_command(self, script):
        return self._process_commands(script.splitlines(), need_ack=False)

    def run_script(self, script):
        with self.mutex:
            return self._process_commands(script.splitlines(), need_ack=False)

    def _process_commands(self, commands, need_ack=True):
        for line in commands:
            parts = line.strip().split()
            if not parts:
                continue
            if re.fullmatch(r"N\d+", parts[0], re.IGNORECASE):
                parts = parts[1:]
                if not parts:
                    continue
            cmd = parts[0].upper()
            params = {}
            for part in parts[1:]:
                if "=" in part:
                    key, value = part.split("=", 1)
                    params[key] = value
            gcmd = FakeGCodeCommand(self, line, params, need_ack)
            handler = self.gcode_handlers.get(cmd)
            if handler is not None:
                handler(gcmd)

    def respond_info(self, *args, **kwargs):
        return None

    def respond_raw(self, *args, **kwargs):
        return None


class FakePrinter:
    def __init__(self):
        self.reactor = FakeReactor()
        self.objects = {}
        self.events = {}
        self.objects["gcode"] = FakeGCode(self)

    def get_reactor(self):
        return self.reactor

    def lookup_object(self, name, default=None):
        return self.objects.get(name, default)

    def add_object(self, name, obj):
        self.objects[name] = obj

    def register_event_handler(self, name, callback):
        self.events.setdefault(name, []).append(callback)

    def send_event(self, name, *args):
        for callback in self.events.get(name, []):
            callback(*args)

    def config_error(self, message):
        return RuntimeError(message)
