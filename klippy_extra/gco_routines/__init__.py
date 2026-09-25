# Main Klipper extra entry point for gco-routines
#
# Implements START / END / WAIT with no upstream tracked-file edits.

import greenlet
import re
from contextlib import contextmanager
from typing import Optional, Dict, Any, List

from .runtime import Run, Routine, ContractError, freeze, thaw
from .driver_api import DriverAPI
from .integration import IntegrationAdapter, CompatibilityError
from .templates import MacroRecursionTracker, OrderedTemplateCompiler

RESERVED_COMMANDS = ["START", "END", "WAIT"]

class GcoRoutinesManager:
    def __init__(self, config):
        self.config = config
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.runs: Dict[str, Run] = {}
        self.active_run_id: Optional[str] = None
        self._run_sequence = 0
        self.admissions_paused = False
        # Routines suspended at a command boundary: routine id -> (run, routine).
        self._suspended = {}
        self.recursion_tracker = MacroRecursionTracker()
        self.driver_api = DriverAPI(self)
        self._command_frames = {}

        # Load the macro manager before later [gcode_macro ...] sections so the
        # adapter can retain their raw source. Legacy macros stay on the stock
        # render-entire-template path.
        self.macro_manager = None
        load_object = getattr(self.printer, "load_object", None)
        if callable(load_object) and hasattr(config, "getsection"):
            self.macro_manager = load_object(config, "gcode_macro")
        if self.macro_manager is None and hasattr(self.printer, "lookup_object"):
            self.macro_manager = self.printer.lookup_object("gcode_macro", None)
        self.ordered_macro_compiler = (
            OrderedTemplateCompiler(self.macro_manager.env)
            if self.macro_manager is not None and hasattr(self.macro_manager, "env")
            else None
        )

        # 1. Preflight command collision check
        self._check_command_collisions()

        # 2. Register gcode commands
        self._register_reserved_commands()

        # 3. Install live method wrappers
        self.adapter = IntegrationAdapter(self.printer, self)
        self.adapter.install()

        # 4. Register printer object for status observability
        self.printer.add_object('gco_routines', self)

        # Initialize default run
        self.start_run("run_default")

    def _register_reserved_commands(self):
        """Preflight every reservation before mutating the live dispatcher."""
        handlers = getattr(self.gcode, "ready_gcode_handlers", {})
        base = getattr(self.gcode, "base_gcode_handlers", {})
        mux = getattr(self.gcode, "mux_commands", {})
        conflicts = [cmd for cmd in RESERVED_COMMANDS
                     if cmd in handlers or cmd in base or cmd in mux]
        if conflicts:
            raise self.config.error(
                "Reserved gco-routine command collision: " + ", ".join(conflicts))
        specs = [
            ("START", self.cmd_START, "Start background gco-routine"),
            ("END", self.cmd_END, "End background gco-routine block"),
            ("WAIT", self.cmd_WAIT, "Wait for gco-routines to complete"),
        ]
        registered = []
        try:
            for cmd, handler, desc in specs:
                self.gcode.register_command(cmd, handler, desc=desc)
                registered.append(cmd)
        except Exception:
            # Best-effort rollback is safe because preflight guarantees these
            # names were absent.  Never leave a half-enabled extension.
            for cmd in registered:
                try:
                    self.gcode.register_command(cmd, None)
                except Exception:
                    pass
            raise

    def begin_command_frame(self, gcmd):
        """Create a per-dispatch result frame, isolated across nested calls."""
        try:
            import greenlet as _greenlet
            key = _greenlet.getcurrent()
        except ImportError:  # pragma: no cover
            key = id(self)
        stack = self._command_frames.setdefault(key, [])
        frame = {"gcmd": gcmd, "reply": {}}
        stack.append(frame)
        run, routine = self.get_bound_context()
        frame["routine"] = routine
        if routine is not None:
            routine.reply = {}
            routine.command = getattr(gcmd, "get_commandline", lambda: None)()
            routine.detail = None
            run.revision += 1
        return (key, frame)

    def end_command_frame(self, token):
        if token is None:
            return
        key, frame = token
        stack = self._command_frames.get(key, [])
        if stack and stack[-1] is frame:
            stack.pop()
        if not stack:
            self._command_frames.pop(key, None)
        routine = frame.get("routine")
        if routine is not None:
            # Publish the command that just returned, not its last subcommand.
            # This also handles managed helpers whose children had live replies.
            routine.reply = dict(frame["reply"])

    def _current_frame(self, gcmd=None):
        try:
            import greenlet as _greenlet
            key = _greenlet.getcurrent()
        except ImportError:  # pragma: no cover
            key = id(self)
        stack = self._command_frames.get(key, [])
        if not stack:
            return None
        frame = stack[-1]
        if gcmd is not None and frame.get("gcmd") is not gcmd:
            return None
        return frame

    def _check_command_collisions(self):
        mux = getattr(self.gcode, "mux_commands", {})
        for cmd in RESERVED_COMMANDS:
            if (cmd in self.gcode.ready_gcode_handlers
                    or cmd in self.gcode.base_gcode_handlers or cmd in mux):
                raise self.config.error(
                    f"Reserved gco-routine command '{cmd}' collides with an existing registration"
                )

    @staticmethod
    def _is_live_persistent(run: Optional[Run]) -> bool:
        return (run is not None and run.persistent
                and run.routines[run.default_id].state in ("running", "waiting"))

    def start_run(self, run_id: str, persistent: bool = False) -> Run:
        active = self.runs.get(self.active_run_id)
        if active is not None and any(
                routine.id != active.default_id
                and routine.state in ("running", "waiting")
                for routine in active.routines.values()):
            raise ContractError("Cannot replace a run with active routines")
        # Retain outcomes for the active job and a bounded recent history.
        # A cancelled native handler may still be unwinding on a greenlet;
        # never remove its context until that callback has returned.
        for old_id, old_run in list(self.runs.items()):
            if len(self.runs) < 64:
                break
            if (not old_run.greenlet_to_rid and all(
                    r.state in ("completed", "cancelled", "failed")
                    for r in old_run.routines.values())):
                del self.runs[old_id]
        if len(self.runs) >= 64:
            raise self.gcode.error("Run retention limit exceeded")
        self._run_sequence += 1
        if run_id in self.runs:
            run_id = "%s_%d" % (run_id, self._run_sequence)
        run = Run(run_id, self.reactor)
        run.persistent = bool(persistent)
        self.runs[run_id] = run
        # The active run is what get_status() and unbound callers observe.
        # A transient API/macro run never displaces a persistent file run
        # that is still running; it executes through its own bound context.
        # Once the file run has ended it stays reported until the next run.
        if persistent or not self._is_live_persistent(active):
            self.active_run_id = run_id
        return run

    def get_current_run(self) -> Run:
        run, _routine = self.get_bound_context()
        if run is not None:
            return run
        if self.active_run_id and self.active_run_id in self.runs:
            return self.runs[self.active_run_id]
        return self.start_run("run_default")

    def get_bound_context(self):
        """Return the run/routine explicitly bound to this greenlet, if any."""
        current = greenlet.getcurrent()
        for run in self.runs.values():
            routine = run.get_routine_by_greenlet(current)
            if routine is not None:
                return run, routine
        return None, None

    def get_current_routine(self) -> Optional[Routine]:
        _run, routine = self.get_bound_context()
        if routine is not None:
            return routine
        # Fallback to active run's default
        run = self.get_current_run()
        return run.get_routine(run.default_id)

    def _run_for_routine(self, routine: Routine) -> Run:
        for run in self.runs.values():
            if run.routines.get(routine.id) is routine:
                return run
        raise ContractError("Routine is not owned by an active run")

    @contextmanager
    def default_context(self, run: Optional[Run] = None):
        """Bind the current Klipper greenlet to a run's default routine."""
        run = run or self.get_current_run()
        with run.routine_context(run.get_routine(run.default_id)) as routine:
            yield routine

    def is_child_of_lock_owner(self, curr_g: greenlet.greenlet, owner_g: greenlet.greenlet) -> bool:
        """HRAL check: determines if curr_g is an admitted descendant routine of owner_g."""
        for run in self.runs.values():
            owner_r = run.get_routine_by_greenlet(owner_g)
            curr_r = run.get_routine_by_greenlet(curr_g)
            if owner_r and curr_r and curr_r.id != owner_r.id:
                return True
        return False

    def set_command_reply(self, gcmd, mapping: dict = None):
        if mapping is None:
            mapping = gcmd
            gcmd = None
        frame = self._current_frame(gcmd)
        if frame is None:
            raise ContractError("No active command frame for structured reply")
        if type(mapping) is not dict:
            raise ContractError("Structured reply must be a map")
        value = thaw(freeze(mapping))
        frame["reply"] = value
        curr = self.get_current_routine()
        if curr:
            curr.reply = dict(value)

    def set_routine_detail(self, fields: dict, gcmd=None):
        if gcmd is not None and self._current_frame(gcmd) is None:
            raise ContractError("No active command frame for routine detail")
        curr = self.get_current_routine()
        if curr:
            curr.detail = freeze(dict(fields))
            self._run_for_routine(curr).revision += 1

    def dispatch_command_for_routine(self, routine: Routine, cmd_text: str):
        routine.command = cmd_text
        routine.reply = {}
        run = self._run_for_routine(routine)
        if run.is_cancelled or run.fault is not None:
            raise ContractError("Cannot dispatch command: run is cancelled or faulted")
        with run.routine_context(routine):
            return self.adapter.dispatch_managed_line(cmd_text, run, routine)

    def check_execution(self, run, routine):
        """Check at every command boundary, including called legacy macros.

        Pause is not checked here: an admitted child suspends at its own next
        command boundary instead (see ``pause_blocks``), and commands nested
        in a command already executing complete, as in stock Klipper.
        """
        try:
            run.check_active(routine.id)
        except ContractError as exc:
            raise self.gcode.error(str(exc))

    def pause_blocks(self, run, routine) -> bool:
        """True if a child must suspend before its next own command.

        Called with the G-code mutex held.  While a routine of the same run
        holds that mutex in WAIT (an API script, ordered macro, file WAIT line
        or nested helper waiting on children), Klipper cannot accept RESUME
        until the wait ends, so suspending would deadlock: the child then
        continues and suspends at a later boundary.
        """
        if not self.admissions_paused or routine.id == run.default_id:
            return False
        mutex = getattr(self.adapter, "_owner_mutex", None)
        current = greenlet.getcurrent()
        for other_g in (mutex.inside() if mutex is not None else ()):
            if other_g is current:
                continue
            other = run.get_routine_by_greenlet(other_g)
            if other is not None and other.state == "waiting":
                return False
        return True

    def suspend_for_pause(self, run, routine):
        """Cooperatively suspend a child (outside the G-code mutex).

        Woken by RESUME/CLEAR_PAUSE, by a mutex-holding routine of the same
        run starting a WAIT, or by cancel/reset/shutdown/failure, which
        complete the routine's completion and leave it cancelled.
        """
        # The run may have been cancelled while this child queued for the
        # mutex; never suspend on a completion that nothing will complete.
        run.check_active(routine.id)
        routine.suspended = "paused"
        routine.completion = run._new_completion()
        self._suspended[routine.id] = (run, routine)
        run.revision += 1
        try:
            routine.completion.wait()
        finally:
            self._suspended.pop(routine.id, None)
            routine.suspended = None
            run.revision += 1
        run.check_active(routine.id)

    def _wake_suspended(self, run=None):
        for owner, routine in tuple(self._suspended.values()):
            if run is not None and owner is not run:
                continue
            completion = routine.completion
            if completion is not None and not completion.test():
                completion.complete(None)

    def _before_blocking_wait(self, run):
        # A routine about to block while holding the G-code mutex must not
        # wait on children suspended for pause: RESUME cannot be accepted.
        mutex = getattr(self.adapter, "_owner_mutex", None)
        if self._suspended and mutex is not None and mutex.holds():
            self._wake_suspended(run)

    def check_spawn(self):
        if self.admissions_paused:
            raise self.gcode.error("Routine admission is paused")

    def wait_for_routine(self, routine: Routine, targets: Optional[List[str]]):
        run = self._run_for_routine(routine)
        routine.command = f"WAIT ON={','.join(targets)}" if targets else "WAIT"
        satisfied = run.wait(routine.id, targets)
        if not satisfied:
            self._before_blocking_wait(run)
            # Yield greenlet until dependencies complete
            res = routine.completion.wait()
            if run.fault:
                raise self.gcode.error(f"Run faulted: {run.fault}")

    def spawn_child_routine(self, run: Run, child_routine: Routine, child_tpl: Any, child_vars: dict):
        def _run_child(eventtime):
            if run.fault is not None or child_routine.state != "running":
                return self.reactor.NEVER
            child_g = greenlet.getcurrent()
            run.register_greenlet(child_g, child_routine.id)
            try:
                self.check_execution(run, child_routine)
                result = child_tpl.execute(self, child_vars, child_routine)
                run.finish_routine(child_routine.id, result)
            except Exception as e:
                run.fail_routine(child_routine.id, str(e))
                self.gcode.respond_info("Routine %s error: %s" %
                                        (child_routine.id, e))
            finally:
                run.unregister_greenlet(child_g)
            return self.reactor.NEVER

        # Register callback on reactor to run child in greenlet
        self.reactor.register_callback(_run_child)

    def spawn_routine_from_commands(self, name: Optional[str], body_lines: List[str], source=None):
        if self.admissions_paused:
            raise ContractError("Routine admission is paused")
        run = self.get_current_run()
        starter = self.get_current_routine()
        child_routine = run.start_routine(name, caller_id=starter.id)
        child_routine.source = freeze(source) if source is not None else None

        def _run_commands(eventtime):
            if run.fault is not None or child_routine.state != "running":
                return self.reactor.NEVER
            child_g = greenlet.getcurrent()
            run.register_greenlet(child_g, child_routine.id)
            try:
                for line in body_lines:
                    clean = line.strip()
                    if not clean or clean.startswith(";"):
                        continue
                    self.dispatch_command_for_routine(child_routine, clean)
                retained = (thaw(child_routine.result)
                            if child_routine.result is not None else {})
                run.finish_routine(child_routine.id, retained)
            except Exception as e:
                run.fail_routine(child_routine.id, str(e))
                self.gcode.respond_info("Routine %s failed: %s" %
                                        (child_routine.id, e))
            finally:
                run.unregister_greenlet(child_g)
            return self.reactor.NEVER

        self.reactor.register_callback(_run_commands)
        return child_routine

    def join_active_run(self):
        """Implicit final wait for all background routines in active run."""
        run = self.get_current_run()
        default_r = run.get_routine(run.default_id)
        if default_r.state == "completed":
            return
        if default_r.state in ("failed", "cancelled"):
            raise self.gcode.error("Run faulted before completion: %s" %
                                   (run.fault or default_r.error))
        # Bare wait on all outstanding background routines
        satisfied = run.wait(default_r.id, None)
        if not satisfied:
            self._before_blocking_wait(run)
            default_r.completion.wait()
        if run.fault:
            raise self.gcode.error("Run faulted before completion: %s" %
                                   run.fault)

    def finish_active_run(self):
        run = self.get_current_run()
        default = run.get_routine(run.default_id)
        if default.state == "completed":
            return
        self.join_active_run()
        if default.state == "running":
            run.finish_routine(default.id, {})

    def cancel_active_runs(self, message: str = "Run cancelled"):
        for run in self.runs.values():
            if any(r.state in ("running", "waiting") for r in run.routines.values()):
                run.cancel_run(message)

    def set_paused(self, paused: bool):
        self.admissions_paused = bool(paused)
        if not self.admissions_paused:
            self._wake_suspended()

    # Command handlers
    def _direct_control_error(self, gcmd):
        line = getattr(gcmd, "get_commandline", lambda: "")().strip()
        if re.match(r"^N\d+\s*(?:START|END|WAIT)\b", line,
                    re.IGNORECASE | re.ASCII):
            raise gcmd.error(
                "E_TRANSPORT: decode transport framing before using "
                "gco-routines controls")
        raise gcmd.error(
            "gco-routines requires a complete file/API source; "
            "pseudo-TTY or generated control input is unsupported")

    def cmd_START(self, gcmd):
        self._direct_control_error(gcmd)

    def cmd_END(self, gcmd):
        self._direct_control_error(gcmd)

    def cmd_WAIT(self, gcmd):
        self._direct_control_error(gcmd)

    def get_status(self, eventtime=None):
        run = self.get_current_run()
        return run.snapshot()

def load_config(config):
    return GcoRoutinesManager(config)
