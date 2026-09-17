"""Selected upstream implementations transcribed from browser-readable source.

NOT a downloaded or pinned Klipper checkout. Omitted methods are not supplied.
Sources inspected 2026-09-17:
  https://raw.githubusercontent.com/Klipper3d/klipper/master/klippy/gcode.py
  https://raw.githubusercontent.com/Klipper3d/klipper/master/klippy/reactor.py

Klipper portions: Copyright (C) 2016-2026 Kevin O'Connor.
Distributed under the GNU GPLv3 license; see ../COPYING.
"""
import greenlet
import logging
import re
import shlex

class CommandError(Exception):
    pass

class GCodeCommand:
    error = CommandError
    def __init__(self, gcode, command, commandline, params, need_ack):
        self._command = command
        self._commandline = commandline
        self._params = params
        self._need_ack = need_ack
        self.respond_info = gcode.respond_info
        self.respond_raw = gcode.respond_raw
    def get_command(self):
        return self._command
    def get_commandline(self):
        return self._commandline
    def get_command_parameters(self):
        return self._params
    def get_raw_command_parameters(self):
        command = self._command
        origline = self._commandline
        param_start = len(command)
        param_end = len(origline)
        if origline[:param_start].upper() != command:
            param_start += origline.upper().find(command)
            end = origline.rfind('*')
            if end >= 0 and origline[end+1:].isdigit():
                param_end = end
        if origline[param_start:param_start+1].isspace():
            param_start += 1
        return origline[param_start:param_end]
    def ack(self, msg=None):
        if not self._need_ack:
            return False
        ok_msg = "ok"
        if msg:
            ok_msg = "ok %s" % (msg,)
        self.respond_raw(ok_msg)
        self._need_ack = False
        return True

class DispatchMethods:
    """Only the methods below are excerpted from GCodeDispatch."""
    error = CommandError
    args_r = re.compile('([A-Z_]+|[A-Z*])')
    def _process_commands(self, commands, need_ack=True):
        for line in commands:
            line = origline = line.strip()
            cpos = line.find(';')
            if cpos >= 0:
                line = line[:cpos]
            parts = self.args_r.split(line.upper())
            if ''.join(parts[:2]) == 'N':
                cmd = ''.join(parts[3:5]).strip()
            else:
                cmd = ''.join(parts[:3]).strip()
            params = { parts[i]: parts[i+1].strip()
                       for i in range(1, len(parts), 2) }
            gcmd = GCodeCommand(self, cmd, origline, params, need_ack)
            handler = self.gcode_handlers.get(cmd, self.cmd_default)
            try:
                handler(gcmd)
            except self.error as e:
                self._respond_error(str(e))
                self.printer.send_event("gcode:command_error")
                if not need_ack:
                    raise
            except:
                msg = 'Internal error on command:"%s"' % (cmd,)
                logging.exception(msg)
                self.printer.invoke_shutdown(msg)
                self._respond_error(msg)
                if not need_ack:
                    raise
            gcmd.ack()
    def run_script_from_command(self, script):
        self._process_commands(script.split('\n'), need_ack=False)
    def run_script(self, script):
        with self.mutex:
            self._process_commands(script.split('\n'), need_ack=False)
    def _get_extended_params(self, gcmd):
        rawparams = gcmd.get_raw_command_parameters()
        s = shlex.shlex(rawparams, posix=True)
        s.whitespace_split = True
        s.commenters = '#;'
        try:
            eparams = [earg.split('=', 1) for earg in s]
            eparams = { k.upper(): v for k, v in eparams }
        except ValueError as e:
            raise self.error("Malformed command '%s'"
                             % (gcmd.get_commandline(),))
        gcmd._params.clear()
        gcmd._params.update(eparams)
        return gcmd

_NOW = 0.
_NEVER = 9999999999999999.

class ReactorCompletion:
    class sentinel: pass
    def __init__(self, reactor):
        self.reactor = reactor
        self.result = self.sentinel
        self.waiting = []
    def test(self):
        return self.result is not self.sentinel
    def complete(self, result):
        self.result = result
        for wait in self.waiting:
            self.reactor.update_timer(wait.timer, self.reactor.NOW)
    def wait(self, waketime=_NEVER, waketime_result=None):
        if self.result is self.sentinel:
            wait = greenlet.getcurrent()
            self.waiting.append(wait)
            self.reactor.pause(waketime)
            self.waiting.remove(wait)
            if self.result is self.sentinel:
                return waketime_result
        return self.result

class ReactorMutex:
    def __init__(self, reactor, is_locked):
        self.reactor = reactor
        self.is_locked = is_locked
        self.next_pending = False
        self.queue = []
        self.lock = self.__enter__
        self.unlock = self.__exit__
    def test(self):
        return self.is_locked
    def __enter__(self):
        if not self.is_locked:
            self.is_locked = True
            return
        g = greenlet.getcurrent()
        self.queue.append(g)
        while 1:
            self.reactor.pause(self.reactor.NEVER)
            if self.next_pending and self.queue[0] is g:
                self.next_pending = False
                self.queue.pop(0)
                return
    def __exit__(self, type=None, value=None, tb=None):
        if not self.queue:
            self.is_locked = False
            return
        self.next_pending = True
        self.reactor.update_timer(self.queue[0].timer, self.reactor.NOW)

class MacroCommandMethod:
    """GCodeMacro.cmd, with construction supplied by the test harness."""
    def cmd(self, gcmd):
        if self.in_script:
            raise gcmd.error("Macro %s called recursively" % (self.alias,))
        kwparams = dict(self.variables)
        kwparams.update(self.template.create_template_context())
        kwparams['params'] = gcmd.get_command_parameters()
        kwparams['rawparams'] = gcmd.get_raw_command_parameters()
        self.in_script = True
        try:
            self.template.run_gcode_from_command(kwparams)
        finally:
            self.in_script = False

class GetStatusWrapper:
    def __init__(self, printer, eventtime=None):
        self.printer = printer
        self.eventtime = eventtime
        self.cache = {}
    def __getitem__(self, val):
        sval = str(val).strip()
        if sval in self.cache:
            return self.cache[sval]
        po = self.printer.lookup_object(sval, None)
        if po is None or not hasattr(po, 'get_status'):
            raise KeyError(val)
        reactor = self.printer.get_reactor()
        if self.eventtime is None:
            self.eventtime = reactor.monotonic()
        with reactor.assert_no_pause():
            sts = po.get_status(self.eventtime)
        import copy
        self.cache[sval] = res = copy.deepcopy(sts)
        return res
