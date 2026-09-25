"""Small, fail-closed compatibility layer around Klipper's live dispatcher."""
from __future__ import annotations

import functools
import hashlib
import inspect
import logging
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, Optional, Tuple

import greenlet
import jinja2
from .program import BlockCollector, ProgramError, parse_program, preflight_file
from .runtime import ContractError
from .templates import LiveGetStatusWrapper, OrderedTemplateError


class CompatibilityError(RuntimeError):
    pass


# Extension contract violations (source structure, run/routine lifecycle,
# dependencies, results and managed template evaluation) are user-facing
# G-code errors.  Klipper's dispatcher and webhooks treat any other exception
# as an internal error and invoke a printer shutdown, so these must be
# converted before they reach either.  Genuine internal errors are *not*
# converted: they propagate so Klipper logs a traceback as for any extra bug.
CONTRACT_ERRORS = (ContractError, ProgramError, OrderedTemplateError,
                   jinja2.TemplateError)


@contextmanager
def command_errors(gcode):
    """Report extension contract errors through Klipper's command-error path."""
    error = gcode.error
    try:
        yield
    except CONTRACT_ERRORS as exc:
        if isinstance(exc, error):
            raise
        raise error(str(exc)) from exc


class _RecoveryScript(str):
    """A script rendered by Klipper's configured virtual-SD error handler."""


class _ErrorTemplate:
    def __init__(self, original):
        self.original = original

    def render(self, *args, **kwargs):
        return _RecoveryScript(self.original.render(*args, **kwargs))


# c0c7ef2 contains the local CAN changes; ad425fc is the upstream pin.
KNOWN_GCODE_SHA256 = frozenset({
    "7cd92950767a06c9778d540360fe3ce23e51ec1a6b0423c3defc96d228bf96cc",
    "a2bcd6949b4263f608eaa71ba1cdfb713553542b7b90de739241b624f06415ae",
})


def _accepts(func, *names):
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False
    return all(name in params for name in names)


def _source_hash(obj):
    try:
        path = inspect.getsourcefile(obj.__class__) or inspect.getsourcefile(obj)
        if not path or not os.path.isfile(path):
            return None
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except (OSError, TypeError):
        return None


def validate_compatibility(gcode, *, strict=True, virtual_sd=None):
    signatures = {
        "_process_commands": _accepts(getattr(gcode, "_process_commands", None),
                                       "commands", "need_ack"),
        "run_script": _accepts(getattr(gcode, "run_script", None), "script"),
        "run_script_from_command": _accepts(
            getattr(gcode, "run_script_from_command", None), "script"),
        "get_mutex": callable(getattr(gcode, "get_mutex", None)),
        "register_command": _accepts(getattr(gcode, "register_command", None),
                                      "cmd", "func"),
    }
    if virtual_sd is not None:
        signatures["virtual_sdcard.cmd_SDCARD_PRINT_FILE"] = _accepts(
            getattr(virtual_sd, "cmd_SDCARD_PRINT_FILE", None), "gcmd")
    source_sha256 = _source_hash(gcode)
    known = source_sha256 in KNOWN_GCODE_SHA256 or bool(
        getattr(gcode, "gco_routines_compatibility", False))
    report = {"source_sha256": source_sha256, "known_baseline": known,
              "signatures": signatures}
    if strict and (not known or not all(signatures.values())):
        missing = [k for k, v in signatures.items() if not v]
        text = ", missing=" + ",".join(missing) if missing else ""
        raise CompatibilityError("Unsupported Klipper GCodeDispatch baseline" + text)
    return report


class RunAdmissionMutex:
    """Keep Klipper's G-code serialization while a managed run executes.

    Klipper's real mutex is held for as long as *any* greenlet of the owning
    run is inside.  The admission rule is:

    - a greenlet already inside may re-enter (depth counted);
    - any other routine of the owner's run (not only a child of the owner)
      that is still running or waiting is admitted without queueing;
    - every unrelated greenlet queues on Klipper's real mutex.

    When the owner leaves while other greenlets of its run are still inside
    (for example a virtual-SD line finishes while a child's command is in
    flight), ownership passes to one of them.  The real mutex is released
    only when the last greenlet of the run leaves, so an unrelated request
    never runs while a routine command is in flight.  ``test()`` still
    reports the real mutex state.
    """
    def __init__(self, original, runtime):
        self.original = original
        self.runtime = runtime
        self.owner_greenlet = None
        self.owner_run = None
        self.owner_routine_id = None
        # Every greenlet currently inside (owner included) -> nesting depth.
        self._inside = {}
        self.lock = self.__enter__
        self.unlock = self.__exit__

    def test(self):
        return self.original.test()

    def holds(self, current=None):
        """True if ``current`` (default: this greenlet) is inside the mutex."""
        if current is None:
            current = greenlet.getcurrent()
        return current in self._inside

    def inside(self):
        """Greenlets currently inside, owner included."""
        return tuple(self._inside)

    def _routine_for(self, current):
        for run in getattr(self.runtime, "runs", {}).values():
            routine = run.get_routine_by_greenlet(current)
            if routine is not None:
                return run, routine
        return None, None

    def _admitted(self, current):
        if self.owner_greenlet is None or self.owner_run is None:
            return False
        run, routine = self._routine_for(current)
        return (run is self.owner_run and routine is not None
                and routine.id != self.owner_routine_id
                and routine.state in ("running", "waiting"))

    def _set_owner(self, current):
        self.owner_greenlet = current
        self.owner_run, routine = self._routine_for(current)
        self.owner_routine_id = routine.id if routine is not None else None

    def __enter__(self):
        current = greenlet.getcurrent()
        depth = self._inside.get(current, 0)
        if depth:
            # Managed line boundaries may be dispatched from a greenlet that
            # is already inside (the outer API/macro/SD request, or a routine
            # running a helper).  Klipper's mutex is not reentrant, so count
            # the nesting and let only the outermost exit leave.
            self._inside[current] = depth + 1
            return self
        if self._admitted(current):
            self._inside[current] = 1
            return self
        self.original.__enter__()
        # The real mutex is only handed over once every greenlet of the
        # previous owner run has left, so nothing else is inside now.
        self._inside = {current: 1}
        self._set_owner(current)
        return self

    def __exit__(self, exc_type=None, exc_val=None, exc_tb=None):
        current = greenlet.getcurrent()
        depth = self._inside.get(current, 0)
        if not depth:
            if not self._inside:
                # Untracked exit with nothing admitted: keep pass-through.
                return self.original.__exit__(exc_type, exc_val, exc_tb)
            logging.warning("gco-routines: ignoring G-code mutex exit from a "
                            "greenlet that is not inside")
            return False
        if depth > 1:
            self._inside[current] = depth - 1
            return False
        del self._inside[current]
        if self._inside:
            if current is self.owner_greenlet:
                # Another greenlet of the same run is still executing a
                # command: transfer ownership within the run, do not release.
                successor = next(iter(self._inside))
                _run, routine = self._routine_for(successor)
                self.owner_greenlet = successor
                self.owner_routine_id = routine.id if routine is not None else None
            return False
        self.owner_greenlet = self.owner_run = self.owner_routine_id = None
        return self.original.__exit__(exc_type, exc_val, exc_tb)


@dataclass
class _SourceState:
    collector: BlockCollector
    line_number: int = 0
    persistent: bool = False
    run_id: Optional[str] = None


class ManagedFileWrapper:
    """Join admitted routines before VirtualSD is allowed to observe EOF."""
    def __init__(self, raw_file, runtime, collector, source_id=None, printer=None):
        self.raw_file = raw_file
        self.runtime = runtime
        self.collector = collector
        self.source_id = source_id
        self.printer = printer
        self._eof_joined = False

    def read(self, size=-1):
        data = self.raw_file.read(size)
        if not data and not self._eof_joined:
            try:
                self.collector.assert_closed()
                run = self.runtime.runs.get(self.source_id)
                if run is None:
                    self.runtime.finish_active_run()
                else:
                    with self.runtime.default_context(run):
                        self.runtime.finish_active_run()
            except Exception as e:
                if self.printer is not None:
                    vsd = self.printer.lookup_object("virtual_sdcard", None)
                    if vsd is not None and hasattr(vsd, "print_stats"):
                        vsd.print_stats.note_error(str(e))
                        template = getattr(vsd, "on_error_gcode", None)
                        if template is not None:
                            try:
                                vsd.gcode.run_script(template.render())
                            except Exception:
                                logging.exception("gco-routines EOF error cleanup")
                raise
            self._eof_joined = True
        return data

    def seek(self, offset, whence=0):
        self._eof_joined = False
        return self.raw_file.seek(offset, whence)

    def tell(self):
        return self.raw_file.tell()

    def close(self):
        return self.raw_file.close()

    def __iter__(self):
        return iter(self.raw_file)

    def __getattr__(self, name):
        return getattr(self.raw_file, name)


class IntegrationAdapter:
    def __init__(self, printer, runtime, *, strict=True):
        self.printer = printer
        self.runtime = runtime
        self.gcode = printer.lookup_object("gcode")
        self.reactor = printer.get_reactor()
        self.strict = strict
        self.compatibility = None
        self.installed = False
        self._source_modes = {}
        self._recovery_depth = {}
        self._sd_sources: Dict[Tuple[Any, Any], _SourceState] = {}
        self._wrapped_handlers = set()
        self._orig_process_commands = None
        self._orig_run_script = None
        self._orig_run_script_from_command = None
        self._owner_mutex = None
        self._hooked_vsd = set()
        self._sd_workers = set()
        self._hooked_pause = set()
        self._hooked_macros = set()
        self._macro_loader_hooked = set()

    def install(self):
        if self.installed:
            return self
        self.compatibility = validate_compatibility(
            self.gcode, strict=self.strict,
            virtual_sd=self.printer.lookup_object("virtual_sdcard", None))
        original_mutex = self.gcode.get_mutex()
        owner_mutex = RunAdmissionMutex(original_mutex, self.runtime)
        self._owner_mutex = owner_mutex
        self.gcode.mutex = owner_mutex
        # GCodeIO caches this field in both pinned trees.
        gcode_io = getattr(self.gcode, "gcode_io", None)
        if gcode_io is None:
            gcode_io = self.printer.lookup_object("gcode_io", None)
        if gcode_io is not None and hasattr(gcode_io, "gcode_mutex"):
            gcode_io.gcode_mutex = owner_mutex
        self._orig_process_commands = self.gcode._process_commands
        self._orig_run_script = self.gcode.run_script
        self._orig_run_script_from_command = self.gcode.run_script_from_command
        self.gcode._process_commands = self._wrapped_process_commands
        self.gcode.run_script = self._wrapped_run_script
        self.gcode.run_script_from_command = self._wrapped_run_script_from_command
        self._install_handler_frames()
        self._install_existing_objects()
        register = getattr(self.printer, "register_event_handler", None)
        if callable(register):
            register("klippy:connect", self._handle_connect)
            register("klippy:shutdown", self._handle_shutdown)
            register("klippy:disconnect", self._handle_shutdown)
            register("virtual_sdcard:reset_file", self._handle_reset)
            register("gcode:debuginput_exit", self._handle_file_input_exit)
        self.installed = True
        return self

    def _install_existing_objects(self):
        gcode_io = self.printer.lookup_object("gcode_io", None)
        if gcode_io is not None and self._owner_mutex is not None \
                and hasattr(gcode_io, "gcode_mutex"):
            gcode_io.gcode_mutex = self._owner_mutex
        self._hook_virtual_sdcard(self.printer.lookup_object("virtual_sdcard", None))
        self._hook_pause_resume(self.printer.lookup_object("pause_resume", None))
        self._hook_gcode_macro(self.printer.lookup_object("gcode_macro", None))

    def _handle_connect(self, *args):
        # Objects loaded after [gco_routines] are hooked at connect; an
        # unsupported one is a configuration error, not an internal error.
        try:
            self._install_existing_objects()
        except CompatibilityError as exc:
            config_error = getattr(self.printer, "config_error", None)
            if config_error is None:
                raise
            raise config_error(str(exc)) from exc

    def _handle_shutdown(self, *args):
        self.runtime.cancel_active_runs("Klipper shutdown/disconnect")

    def _handle_reset(self, *args):
        self.runtime.cancel_active_runs("Virtual SD file reset")
        self._sd_sources.clear()

    def _handle_file_input_exit(self, *args):
        for state in tuple(self._sd_sources.values()):
            if state.run_id == "file_input":
                state.collector.assert_closed()
        self.runtime.join_active_run()
        return True

    def _install_handler_frames(self):
        for mapping in (getattr(self.gcode, "base_gcode_handlers", {}),
                        getattr(self.gcode, "ready_gcode_handlers", {})):
            for cmd, handler in list(mapping.items()):
                mapping[cmd] = self._frame_handler(handler)
        original = getattr(self.gcode, "register_command", None)
        if not callable(original) or getattr(original, "_gco_wrapper", False):
            return
        @functools.wraps(original)
        def register(cmd, func, when_not_ready=False, desc=None):
            result = original(cmd, func, when_not_ready, desc)
            if func is not None:
                for mapping in (getattr(self.gcode, "base_gcode_handlers", {}),
                                getattr(self.gcode, "ready_gcode_handlers", {})):
                    if cmd in mapping:
                        mapping[cmd] = self._frame_handler(mapping[cmd])
            return result
        register._gco_wrapper = True
        self.gcode.register_command = register

    def _frame_handler(self, handler: Callable):
        if getattr(handler, "_gco_frame_wrapper", False):
            return handler
        @functools.wraps(handler)
        def wrapped(gcmd, *args, **kwargs):
            begin = getattr(self.runtime, "begin_command_frame", None)
            end = getattr(self.runtime, "end_command_frame", None)
            frame = begin(gcmd) if callable(begin) else None
            try:
                # A device handler may violate a result contract through the
                # driver API; report that as a command error, not a shutdown.
                with command_errors(self.gcode):
                    if gcmd.get_command() in ("PAUSE", "RESUME", "CLEAR_PAUSE", "CANCEL_PRINT", "M112"):
                        with self._recovery_context():
                            return handler(gcmd, *args, **kwargs)
                    return handler(gcmd, *args, **kwargs)
            finally:
                if callable(end):
                    end(frame)
        wrapped._gco_frame_wrapper = True
        wrapped._gco_original = handler
        return wrapped

    @contextmanager
    def _recovery_context(self):
        key = greenlet.getcurrent()
        self._recovery_depth[key] = self._recovery_depth.get(key, 0) + 1
        try:
            yield
        finally:
            self._recovery_depth[key] -= 1
            if not self._recovery_depth[key]:
                del self._recovery_depth[key]

    def _in_recovery(self):
        return bool(self._recovery_depth.get(greenlet.getcurrent()))

    def _push_mode(self, mode):
        key = greenlet.getcurrent()
        self._source_modes.setdefault(key, []).append(mode)

    def _pop_mode(self):
        key = greenlet.getcurrent()
        stack = self._source_modes.get(key, [])
        if stack:
            stack.pop()
        if not stack:
            self._source_modes.pop(key, None)

    def _mode(self):
        stack = self._source_modes.get(greenlet.getcurrent(), [])
        return stack[-1] if stack else "interactive"

    @staticmethod
    def _reserved_head(line):
        clean = str(line).split(";", 1)[0].strip()
        transport = re.match(r"^N\d+\s*(START|END|WAIT)\b", clean,
                             re.IGNORECASE | re.ASCII)
        if transport is not None:
            return "TRANSPORT"
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\b", clean, re.ASCII)
        if match is None:
            return None
        head = match.group(1).upper()
        return head if head in ("START", "END", "WAIT") else None

    def _bound_context(self):
        finder = getattr(self.runtime, "get_bound_context", None)
        return finder() if callable(finder) else (None, None)

    def _adopt_mutex_owner(self, run, routine):
        mutex = self._owner_mutex
        current = greenlet.getcurrent()
        if mutex is not None and mutex.owner_greenlet is current:
            mutex.owner_run = run
            mutex.owner_routine_id = routine.id

    def _is_sd_source(self):
        try:
            vsd = self.printer.lookup_object("virtual_sdcard", None)
            return (vsd is not None and bool(vsd.is_cmd_from_sd())
                    and (id(vsd) not in self._hooked_vsd
                         or greenlet.getcurrent() in self._sd_workers))
        except (AttributeError, TypeError):
            return False

    def _source_state(self, persistent, source_name="sd"):
        if persistent:
            vsd = self.printer.lookup_object("virtual_sdcard", None)
            if source_name == "file_input":
                key = ("file_input", id(self.printer))
            else:
                current = getattr(vsd, "current_file", None)
                key = (id(vsd), id(current) if current is not None else None)
            state = self._sd_sources.get(key)
            if state is None:
                state = _SourceState(
                    BlockCollector(), persistent=True,
                    run_id=("file_input" if source_name == "file_input" else
                            getattr(self.runtime.get_current_run(), "run_id", None)))
                self._sd_sources[key] = state
            return state
        return _SourceState(BlockCollector())

    def _wrapped_run_script(self, script):
        if isinstance(script, _RecoveryScript):
            with self._recovery_context():
                return self._orig_run_script(script)
        # Webhooks and the virtual-SD worker call run_script() directly, so this
        # is a dispatcher seam: contract errors must become command errors.
        with command_errors(self.gcode):
            return self._run_managed_script(script)

    def _run_managed_script(self, script):
        # VirtualSD calls run_script() once per physical source line. A public
        # API call supplies a complete source in one call. Calls made by a
        # command handler are internal and may not manufacture control lines.
        is_sd = self._is_sd_source()
        bound_run, bound_routine = self._bound_context()
        has_controls = any(
            self._reserved_head(line) is not None
            for line in str(script).splitlines()
        )
        if bound_run is not None:
            mode = "internal"
            run = bound_run
        elif is_sd:
            mode = "sd"
            state = self._source_state(True)
            run = self.runtime.runs[state.run_id]
        elif has_controls:
            # Validate a complete submission before admitting its first effect.
            # Wait for unrelated requests before creating/replacing a run.
            parse_program(script, source_id="api")
            with self.gcode.get_mutex():
                run = self.runtime.start_run("api_run")
                with self.runtime.default_context(run) as routine:
                    self._adopt_mutex_owner(run, routine)
                    self._push_mode("api")
                    try:
                        return self._orig_run_script(script)
                    except Exception as exc:
                        run.cancel_run(str(exc))
                        raise
                    finally:
                        self._pop_mode()
        else:
            mode = "ordinary_api"
            run = None

        self._push_mode(mode)
        try:
            if run is None:
                return self._orig_run_script(script)
            if bound_routine is not None:
                return self._orig_run_script(script)
            with self.runtime.default_context(run) as routine:
                # run_script() acquires the mutex after this binding, so the
                # owner-aware lock records the correct run automatically.
                return self._orig_run_script(script)
        except Exception as exc:
            if mode == "api" and run is not None:
                run.cancel_run(str(exc))
            raise
        finally:
            self._pop_mode()

    def _wrapped_run_script_from_command(self, script):
        self._push_mode("internal")
        try:
            return self._orig_run_script_from_command(script)
        finally:
            self._pop_mode()

    def dispatch_managed_line(self, line, run=None, routine=None):
        """Dispatch one compiler/child-owned literal boundary."""
        mutex = self._owner_mutex
        if (mutex is not None and run is not None and routine is not None
                and routine.id != run.default_id and not mutex.holds()):
            # A child's own command boundary.  Decide pause suspension and
            # admit the command under the G-code mutex, atomically, so a
            # PAUSE accepted while this child queued is honored.
            pause_blocks = getattr(self.runtime, "pause_blocks", None)
            while True:
                with mutex:
                    if not callable(pause_blocks) or not pause_blocks(run, routine):
                        return self._dispatch_literal(line)
                # Suspend only outside the mutex: a suspended routine must
                # never keep Klipper from accepting RESUME or CANCEL_PRINT.
                self.runtime.suspend_for_pause(run, routine)
        # Nested boundaries (inside a command already executing) and the
        # default routine are not suspended, as in stock Klipper.
        return self._dispatch_literal(line)

    def _dispatch_literal(self, line):
        self._push_mode("managed_literal")
        try:
            return self._orig_run_script(line)
        finally:
            self._pop_mode()

    def _dispatch_control(self, state, line):
        status, payload = state.collector.feed_line(line, state.line_number)
        if status == "COLLECTING":
            return None
        if status == "SPAWN":
            name, body = payload
            source = {"line": state.line_number, "source": state.run_id}
            self.runtime.spawn_routine_from_commands(name, body, source=source)
            return None
        if status == "WAIT":
            self.runtime.wait_for_routine(self.runtime.get_current_routine(), payload)
            return None
        return payload

    def _wrapped_process_commands(self, commands: Iterable[str], need_ack=True):
        # Klipper's GCodeIO and run_script() call this directly; contract
        # errors raised while admitting a source must become command errors.
        with command_errors(self.gcode):
            return self._process_managed_commands(commands, need_ack)

    def _process_managed_commands(self, commands: Iterable[str], need_ack=True):
        commands = list(commands)
        is_sd = self._is_sd_source()
        gcode_io = self.printer.lookup_object("gcode_io", None)
        is_file_input = bool(getattr(gcode_io, "is_fileinput", False))
        mode = self._mode()
        if self._in_recovery():
            if any(self._reserved_head(line) is not None for line in commands):
                raise self.gcode.error("Recovery scripts cannot start managed routines")
            return self._orig_process_commands(commands, need_ack=need_ack)

        def dispatch_checked(lines):
            result = None
            for line in lines:
                run, routine = self._bound_context()
                if routine is not None:
                    self.runtime.check_execution(run, routine)
                try:
                    result = self._orig_process_commands([line], need_ack=need_ack)
                except Exception as exc:
                    if run is not None:
                        run.fail_routine(routine.id, str(exc))
                    raise
            return result

        if mode == "internal":
            if any(self._reserved_head(line) is not None for line in commands):
                raise self.gcode.error(
                    "E_GENERATED_CONTROL: legacy/internal rendering may not "
                    "generate START, END, or WAIT; macros using literal "
                    "controls must declare render_mode: ordered")
            return dispatch_checked(commands)

        if mode == "ordinary_api":
            return self._orig_process_commands(commands, need_ack=need_ack)

        if mode == "interactive" and not (is_sd or is_file_input):
            # Let Klipper invoke the registered diagnostic handlers so its
            # normal per-line error reporting and acknowledgement behavior is
            # preserved for serial/PTY ingress.
            return self._orig_process_commands(commands, need_ack=need_ack)

        # Child/compiler lines are never physical SD input, even while the
        # virtual-SD caller is suspended with its cmd_from_sd flag set.
        persistent = mode == "sd" or (mode == "interactive" and (is_sd or is_file_input))
        source_name = "sd" if is_sd else "file_input"
        state = self._source_state(persistent, source_name)
        run, routine = self._bound_context()
        if run is None:
            if state.run_id and state.run_id in getattr(self.runtime, "runs", {}):
                run = self.runtime.runs[state.run_id]
            else:
                run = self.runtime.start_run(
                    source_name if persistent else "api_run", persistent=persistent)
                state.run_id = run.run_id

        def process_in_order():
            result = None
            for line in commands:
                self.runtime.check_execution(run, routine or run.get_routine(run.default_id))
                state.line_number += 1
                passthrough = self._dispatch_control(state, line)
                if passthrough is not None:
                    # Do not accumulate ordinary lines: a following WAIT must
                    # observe the command and any cooperative yields before it.
                    result = dispatch_checked([passthrough])
            if not persistent:
                state.collector.assert_closed()
            if mode == "api":
                self.runtime.finish_active_run()
            return result

        try:
            if routine is not None:
                return process_in_order()
            with self.runtime.default_context(run) as routine:
                # GCodeIO acquires the mutex before _process_commands().
                self._adopt_mutex_owner(run, routine)
                return process_in_order()
        except Exception as exc:
            # Fail the run for every error type; only contract errors are
            # converted (by command_errors), internal errors keep their type.
            run.fail_routine((routine.id if routine else run.default_id),
                             str(exc) or type(exc).__name__)
            raise

    def _hook_virtual_sdcard(self, vsd):
        if vsd is None or id(vsd) in self._hooked_vsd:
            return
        original = getattr(vsd, "_load_file", None)
        if not callable(original):
            if self.strict:
                raise CompatibilityError("virtual_sdcard lacks _load_file")
            return

        def resolve_preflight(filename, check_subdirs):
            files = vsd.get_file_list(check_subdirs)
            by_lower = {name.lower(): name for name, _size in files}
            selected = filename
            if selected not in by_lower.values():
                selected = by_lower.get(selected.lower(), selected)
            path = os.path.realpath(os.path.join(vsd.sdcard_dirname, selected))
            root = os.path.realpath(vsd.sdcard_dirname)
            try:
                inside = os.path.commonpath((root, path)) == root and path != root
            except ValueError:
                inside = False
            if not inside:
                raise self.gcode.error("Virtual SD path escapes configured root")
            preflight_file(path)

        @functools.wraps(original)
        def load_file(gcmd, filename, check_subdirs=False):
            # Reached from M23/SDCARD_PRINT_FILE handlers: a rejected source or
            # run must be a command error, never an internal-error shutdown.
            with command_errors(self.gcode):
                return load_managed_file(gcmd, filename, check_subdirs)

        def load_managed_file(gcmd, filename, check_subdirs):
            resolve_preflight(filename, check_subdirs)
            result = original(gcmd, filename, check_subdirs=check_subdirs)
            run = self.runtime.start_run("sd_print", persistent=True)
            self._sd_sources.clear()
            current = getattr(vsd, "current_file", None)
            if current is not None:
                state = _SourceState(BlockCollector(), persistent=True,
                                     run_id=run.run_id)
                wrapped = ManagedFileWrapper(
                    current, self.runtime, state.collector,
                    source_id=state.run_id, printer=self.printer)
                vsd.current_file = wrapped
                self._sd_sources[(id(vsd), id(wrapped))] = state
            return result
        vsd._load_file = load_file
        error_template = getattr(vsd, "on_error_gcode", None)
        if error_template is not None:
            vsd.on_error_gcode = _ErrorTemplate(error_template)
        self._hooked_vsd.add(id(vsd))
        original_work = getattr(vsd, "work_handler", None)
        if callable(original_work):
            @functools.wraps(original_work)
            def work_handler(eventtime):
                worker = greenlet.getcurrent()
                self._sd_workers.add(worker)
                source = self._source_state(True)
                run = self.runtime.runs.get(source.run_id)
                saved_stats = {}
                for name in ("note_complete", "note_pause"):
                    original_note = getattr(vsd.print_stats, name, None)
                    if original_note is None:
                        continue
                    saved_stats[name] = original_note
                    def note(*args, _note=original_note, **kwargs):
                        if run is not None and run.fault:
                            if not run.is_cancelled:
                                vsd.print_stats.note_error(run.fault)
                            return
                        return _note(*args, **kwargs)
                    setattr(vsd.print_stats, name, note)
                try:
                    return original_work(eventtime)
                finally:
                    for name, original_note in saved_stats.items():
                        setattr(vsd.print_stats, name, original_note)
                    self._sd_workers.discard(worker)
                    # Klipper catches read errors and subsequently note_pause()
                    # overwrites note_error(). Publish the run failure last.
                    if run is not None and run.fault and not run.is_cancelled:
                        vsd.print_stats.note_error(run.fault)
            vsd.work_handler = work_handler
        reset = getattr(vsd, "_reset_file", None)
        if callable(reset):
            @functools.wraps(reset)
            def reset_file(*args, **kwargs):
                self.runtime.cancel_active_runs("Virtual SD file reset")
                self._sd_sources.clear()
                return reset(*args, **kwargs)
            vsd._reset_file = reset_file

    def _hook_pause_resume(self, pause_resume):
        if pause_resume is None or id(pause_resume) in self._hooked_pause:
            return
        cancel = getattr(pause_resume, "cmd_CANCEL_PRINT", None)
        if callable(cancel):
            @functools.wraps(cancel)
            def wrapped_cancel(gcmd, *args, **kwargs):
                self.runtime.cancel_active_runs("Print cancelled")
                try:
                    return cancel(gcmd, *args, **kwargs)
                finally:
                    self.runtime.set_paused(False)
            pause_resume.cmd_CANCEL_PRINT = wrapped_cancel
            self._replace_registered_handler("CANCEL_PRINT", wrapped_cancel,
                                             getattr(pause_resume, "cmd_CANCEL_PRINT_help", None))
        for command, attr, paused in (
                ("PAUSE", "cmd_PAUSE", True),
                ("RESUME", "cmd_RESUME", False),
                ("CLEAR_PAUSE", "cmd_CLEAR_PAUSE", False)):
            original = getattr(pause_resume, attr, None)
            if not callable(original):
                continue
            @functools.wraps(original)
            def lifecycle(gcmd, *args, _original=original,
                          _paused=paused, **kwargs):
                result = _original(gcmd, *args, **kwargs)
                self.runtime.set_paused(_paused)
                return result
            setattr(pause_resume, attr, lifecycle)
            self._replace_registered_handler(
                command, lifecycle, getattr(pause_resume, attr + "_help", None))

        self._hooked_pause.add(id(pause_resume))
        webhooks = self.printer.lookup_object("webhooks", None)
        endpoints = getattr(webhooks, "_endpoints", {}) if webhooks else {}
        for path, paused in (("pause_resume/pause", True),
                             ("pause_resume/resume", False),
                             ("pause_resume/cancel", False)):
            endpoint = endpoints.get(path)
            if not callable(endpoint) or getattr(endpoint, "_gco_wrapper", False):
                continue
            @functools.wraps(endpoint)
            def wrapped_endpoint(request, _endpoint=endpoint,
                                 _path=path, _paused=paused):
                if _path.endswith("cancel"):
                    self.runtime.cancel_active_runs("Print cancelled via API")
                result = _endpoint(request)
                self.runtime.set_paused(_paused)
                return result
            wrapped_endpoint._gco_wrapper = True
            endpoints[path] = wrapped_endpoint

    def _hook_gcode_macro(self, macro_manager):
        if macro_manager is None:
            return
        compiler = getattr(self.runtime, "ordered_macro_compiler", None)
        if compiler is None:
            return

        lookup = getattr(self.printer, "lookup_objects", None)
        if callable(lookup):
            for name, obj in lookup("gcode_macro"):
                if name == "gcode_macro":
                    continue
                template = getattr(obj, "template", None)
                if template is not None and not hasattr(template, "gco_source"):
                    raise CompatibilityError(
                        "[gco_routines] must be loaded before [gcode_macro %s] "
                        "so literal controls can be validated" %
                        getattr(obj, "alias", name))

        if id(macro_manager) not in self._macro_loader_hooked:
            original_load = macro_manager.load_template
            @functools.wraps(original_load)
            def load_template(config, option, default=None):
                # PrinterGCodeMacro.load_template() is a shared Jinja service:
                # display menus, delayed_gcode, idle_timeout, and other extras
                # use it for text that is not a complete macro command source.
                # A menu label such as "Start printing" must therefore remain
                # ordinary template text, not be diagnosed as malformed START.
                section = config.get_name().split(None, 1)[0].lower()
                if section != "gcode_macro" or option.lower() != "gcode":
                    return original_load(config, option, default)
                if default is None:
                    source = config.get(option)
                else:
                    source = config.get(option, default)
                mode = config.get("render_mode", "legacy")
                if mode not in ("legacy", "ordered"):
                    raise config.error(
                        "Option 'render_mode' in section '%s' must be "
                        "'legacy' or 'ordered'" % config.get_name())
                template = original_load(config, option, default)
                name = "%s:%s" % (config.get_name(), option)
                template.gco_source = source
                template.gco_render_mode = mode
                template.gco_runner = None
                # Selection belongs to the macro configuration, never to its
                # source text or caller. Do not parse legacy templates with
                # the managed frontend (even if they contain control text).
                if mode == "ordered":
                    try:
                        template.gco_runner = compiler.compile(source, filename=name)
                    except OrderedTemplateError as exc:
                        raise config.error("%s: %s" % (name, exc)) from exc
                return template
            macro_manager.load_template = load_template
            self._macro_loader_hooked.add(id(macro_manager))

        try:
            from extras import gcode_macro as macro_module
        except ImportError:
            return
        macro_class = macro_module.GCodeMacro
        current = macro_class.cmd
        if getattr(current, "_gco_class_wrapper", False):
            return
        original_cmd = current

        @functools.wraps(original_cmd)
        def macro_cmd(obj, gcmd):
            manager = obj.printer.lookup_object("gco_routines", None)
            runner = getattr(getattr(obj, "template", None), "gco_runner", None)
            if manager is not None and manager.adapter._in_recovery():
                if runner is not None:
                    raise manager.gcode.error("Recovery macros cannot start managed routines")
                return original_cmd(obj, gcmd)
            if manager is None or (runner is None and
                                   manager.get_bound_context()[1] is None):
                return original_cmd(obj, gcmd)
            # Macro handlers run inside Klipper's dispatcher: report contract
            # errors (including starting a run) as command errors only.
            with command_errors(manager.gcode):
                return managed_macro_cmd(manager, runner, obj, gcmd)

        def managed_macro_cmd(manager, runner, obj, gcmd):
            run, routine = manager.get_bound_context()
            owns_run = False
            if routine is None:
                run = manager.start_run("macro_%s" % obj.alias.lower())
                context = manager.default_context(run)
                routine = context.__enter__()
                owns_run = True
                manager.adapter._adopt_mutex_owner(run, routine)
            else:
                context = None

            entered = False
            try:
                # Managed recursion is tracked per routine.  A genuine
                # recursive call is a command error ("Macro X called
                # recursively ..."), as in stock Klipper, never an internal
                # error; it also unwinds an owned run like any other failure.
                manager.recursion_tracker.enter(routine.id, obj.alias)
                entered = True
                params = dict(obj.variables)
                params.update(obj.template.create_template_context())
                params["params"] = gcmd.get_command_parameters()
                params["rawparams"] = gcmd.get_raw_command_parameters()
                if runner is not None:
                    if runner.children and routine.id != run.default_id:
                        raise manager.gcode.error(
                            "Nested spawning is not permitted in v0.2; "
                            "macro %s owns background routines" % obj.alias)
                    params["printer"] = LiveGetStatusWrapper(obj.printer)
                    result = runner.execute(manager, params, routine)
                else:
                    # Preserve stock whole-template rendering for legacy
                    # macros, but recursion ownership is per routine so two
                    # admitted routines may invoke the same helper concurrently.
                    result = obj.template.run_gcode_from_command(params)
                if owns_run:
                    manager.finish_active_run()
                return result
            except Exception as exc:
                # Cancel an owned run for every error type.  Only contract
                # errors become command errors (see command_errors); genuine
                # internal errors propagate for Klipper to log and handle.
                if owns_run:
                    run.cancel_run(str(exc) or type(exc).__name__)
                raise
            finally:
                if entered:
                    manager.recursion_tracker.exit(routine.id, obj.alias)
                if context is not None:
                    context.__exit__(None, None, None)

        macro_cmd._gco_class_wrapper = True
        macro_cmd._gco_original = original_cmd
        macro_class.cmd = macro_cmd

    def _replace_registered_handler(self, command, handler, desc=None):
        """Replace a handler captured by Klipper before an object hook ran."""
        register = getattr(self.gcode, "register_command", None)
        if not callable(register):
            return
        ready = getattr(self.gcode, "ready_gcode_handlers", {})
        base = getattr(self.gcode, "base_gcode_handlers", {})
        if command not in ready and command not in base:
            return
        try:
            register(command, None)
            register(command, handler, desc=desc)
        except Exception:
            # Do not hide a real dispatcher incompatibility.
            raise CompatibilityError("Unable to replace registered %s handler" % command)
