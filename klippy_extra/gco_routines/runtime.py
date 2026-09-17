"""Cooperative runtime for the gco-routines Klipper extra.

The :class:`Run` object is independent of Klipper's dispatcher.  The
integration layer supplies a reactor and calls these methods at command/frame
boundaries.  A run owns routine identities, the dependency graph, retained
results, and all execution context; there is no process-global current routine.
"""

from contextlib import contextmanager
from dataclasses import dataclass, field
from types import MappingProxyType
import math
import re
import inspect
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    import greenlet
except ImportError:  # parser-only installations
    greenlet = None

IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_IDENT_RE = re.compile(r"^" + IDENT + r"$", re.ASCII)
_MISSING = object()


class ContractError(ValueError):
    """A source, lifecycle, dependency, or result contract was violated."""


class RunFaultError(ContractError):
    """An operation was attempted after the run entered its terminal fault."""


def freeze(value: Any, depth: int = 0, budget: Optional[List[int]] = None) -> Any:
    """Return immutable, detached plain data suitable for a result/status."""
    if budget is None:
        budget = [10000]
    budget[0] -= 1
    if budget[0] < 0 or depth > 32:
        raise ContractError("Result exceeds reference data limits")
    if value is None or type(value) in (bool, int, str):
        if type(value) is str and len(value) > 100000:
            raise ContractError("Result string exceeds limit")
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ContractError("Result numbers must be finite")
        return value
    if type(value) in (list, tuple):
        return tuple(freeze(v, depth + 1, budget) for v in value)
    if type(value) is dict:
        if len(value) > 10000:
            raise ContractError("Result map exceeds limit")
        if any(type(k) is not str for k in value):
            raise ContractError("Result map keys must be strings")
        return MappingProxyType({k: freeze(v, depth + 1, budget)
                                 for k, v in value.items()})
    raise ContractError("Unsupported result type: %s" % type(value).__name__)


def thaw(value: Any) -> Any:
    """Deep-copy frozen data to ordinary mutable values for an API caller."""
    if isinstance(value, MappingProxyType):
        return {k: thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [thaw(v) for v in value]
    if isinstance(value, list):
        return [thaw(v) for v in value]
    return value


@dataclass
class CommandFrame:
    """Identity and frame-owned response data for one ordinary command."""
    id: str
    routine_id: str
    command: Optional[str] = None
    reply: Dict[str, Any] = field(default_factory=dict)
    detail: Any = None
    source: Any = None


@dataclass
class Routine:
    id: str
    name: Optional[str]
    starter: Optional[str]
    order: int
    state: str = "running"
    targets: Tuple[str, ...] = ()
    collected: set = field(default_factory=set)
    waited: Tuple[Any, ...] = ()
    result: Any = None
    error: Optional[str] = None
    command: Optional[str] = None
    detail: Any = None
    source: Any = None
    reply: Dict[str, Any] = field(default_factory=dict)
    greenlet: Any = None
    completion: Any = None
    generation: int = 0
    _frames: Dict[Any, List[CommandFrame]] = field(default_factory=dict,
                                                    repr=False)


class _Never:
    pass


class Run:
    """One bounded, cooperative run and its retained routine outcomes.

    ``start_routine(..., runner=callable)`` admits a child and schedules its
    runner with ``reactor.register_callback``. The runner receives its
    :class:`Routine`; integrations that already own scheduling may omit it.
    """

    def __init__(self, run_id: str, reactor: Any, max_routines: int = 1024,
                 max_frames: int = 4096):
        if not isinstance(run_id, str) or not run_id:
            raise ContractError("run_id must be a non-empty string")
        if max_routines < 1 or max_frames < 1:
            raise ContractError("Runtime resource limits must be positive")
        self.run_id = run_id
        self.reactor = reactor
        self.max_routines = max_routines
        self.max_frames = max_frames
        self.default_id = "%s:0" % run_id
        self.routines: Dict[str, Routine] = {}
        self.names: Dict[str, str] = {}
        self.greenlet_to_rid: Dict[Any, str] = {}
        self._fallback_rid: Optional[str] = None
        self._frame_count = 0
        self._frame_seq = 0
        self.revision = 0
        self.fault: Optional[str] = None
        self.is_cancelled = False
        self.generation = 0
        self._new_routine("default", None)

    def _new_completion(self) -> Any:
        factory = getattr(self.reactor, "completion", None)
        if factory is None:
            raise ContractError("Reactor does not provide completion()")
        return factory()

    def _new_routine(self, name: Optional[str], starter: Optional[str]) -> Routine:
        order = len(self.routines)
        rid = "%s:%d" % (self.run_id, order)
        r = Routine(rid, name, starter, order, completion=self._new_completion())
        self.routines[rid] = r
        if name is not None and name != "default":
            self.names[name] = rid
        return r

    def _current_greenlet(self) -> Any:
        return greenlet.getcurrent() if greenlet is not None else None

    def register_greenlet(self, g: Any, rid: str) -> None:
        if rid not in self.routines:
            raise ContractError("Unknown routine identity: %s" % rid)
        self.greenlet_to_rid[g] = rid
        self.routines[rid].greenlet = g

    def unregister_greenlet(self, g: Any) -> None:
        self.greenlet_to_rid.pop(g, None)

    def get_routine_by_greenlet(self, g: Any) -> Optional[Routine]:
        rid = self.greenlet_to_rid.get(g)
        return self.routines.get(rid) if rid else None

    def current_routine(self) -> Routine:
        g = self._current_greenlet()
        r = self.get_routine_by_greenlet(g) if g is not None else None
        if r is not None:
            return r
        if g is None and self._fallback_rid in self.routines:
            return self.routines[self._fallback_rid]
        return self.routines[self.default_id]

    def get_current_routine(self) -> Routine:
        return self.current_routine()

    @contextmanager
    def routine_context(self, routine: Routine):
        """Temporarily bind a routine to the current greenlet/test context."""
        g = self._current_greenlet()
        old = self.greenlet_to_rid.get(g, _MISSING) if g is not None else _MISSING
        old_fallback = self._fallback_rid
        if g is not None:
            self.register_greenlet(g, routine.id)
        else:
            self._fallback_rid = routine.id
        try:
            yield routine
        finally:
            if g is not None:
                if old is _MISSING:
                    self.unregister_greenlet(g)
                else:
                    self.register_greenlet(g, old)
            else:
                self._fallback_rid = old_fallback

    @contextmanager
    def command_frame(self, routine: Optional[Routine] = None,
                      command: Optional[str] = None, source: Any = None):
        """Push a frame so driver replies/detail bind to this command only."""
        routine = routine or self.current_routine()
        if routine.id not in self.routines:
            raise ContractError("Unknown routine identity")
        if self._frame_count >= self.max_frames:
            raise ContractError("Command frame retention limit exceeded")
        g = self._current_greenlet()
        key = g if g is not None else "__fallback__"
        self._frame_seq += 1
        frame = CommandFrame("%s:f%d" % (self.run_id, self._frame_seq),
                             routine.id, command, {}, None, source)
        stack = routine._frames.setdefault(key, [])
        stack.append(frame)
        self._frame_count += 1
        routine.command = command
        routine.reply = {}
        if source is not None:
            routine.source = freeze(source)
        try:
            yield frame
        finally:
            if stack and stack[-1] is frame:
                stack.pop()
            else:
                try:
                    stack.remove(frame)
                except ValueError:
                    pass
            if not stack:
                routine._frames.pop(key, None)
            self._frame_count -= 1

    def current_frame(self, routine: Optional[Routine] = None) -> Optional[CommandFrame]:
        routine = routine or self.current_routine()
        g = self._current_greenlet()
        key = g if g is not None else "__fallback__"
        stack = routine._frames.get(key)
        return stack[-1] if stack else None

    def get_routine(self, rid: str) -> Routine:
        if rid not in self.routines:
            raise ContractError("Unknown routine identity: %s" % rid)
        return self.routines[rid]

    def check_active(self, rid: str) -> Routine:
        r = self.get_routine(rid)
        if self.fault is not None:
            raise RunFaultError("Run is faulted: %s" % self.fault)
        if r.state != "running":
            raise ContractError("Routine %s is not runnable (state=%s)" %
                                (rid, r.state))
        return r

    def _validate_name(self, name: Optional[str]) -> None:
        if name is not None and (not _IDENT_RE.fullmatch(name) or name == "default"):
            raise ContractError("Invalid or reserved routine name: %s" % name)

    def start_routine(self, name: Optional[str] = None,
                      caller_id: Optional[str] = None, runner: Any = None) -> Routine:
        """Admit a child and optionally schedule ``runner`` cooperatively."""
        caller_id = self.default_id if caller_id is None else caller_id
        self.check_active(caller_id)
        if caller_id != self.default_id:
            raise ContractError("Nested spawning is not permitted in v0.2")
        self._validate_name(name)
        if name is not None and name in self.names:
            old = self.routines[self.names[name]]
            starter = self.routines.get(old.starter)
            if (old.state != "completed" or starter is None or
                    old.id not in starter.collected):
                raise ContractError("Name is in use; starter must successfully collect completed instance")
        if len(self.routines) >= self.max_routines:
            raise ContractError("Routine retention limit exceeded")
        routine = self._new_routine(name, caller_id)
        routine.generation = self.generation
        self.revision += 1
        if runner is not None:
            self._schedule_routine(routine, runner)
        return routine

    def _schedule_routine(self, routine: Routine, runner: Any) -> Any:
        if not hasattr(self.reactor, "register_callback"):
            raise ContractError("Reactor does not provide register_callback()")
        generation = self.generation

        def invoke(eventtime):
            if generation != self.generation or routine.state != "running":
                return getattr(self.reactor, "NEVER", _Never)
            g = self._current_greenlet()
            if g is not None:
                self.register_greenlet(g, routine.id)
            try:
                with self.routine_context(routine):
                    runner(routine)
                if routine.state == "running":
                    self.finish_routine(routine.id, {})
            except Exception as exc:
                if routine.state in ("running", "waiting"):
                    self.fail_routine(routine.id, str(exc))
            finally:
                # Reactor greenlets are commonly reused for later callbacks;
                # never leave a terminated child as the apparent caller.
                if g is not None and self.greenlet_to_rid.get(g) == routine.id:
                    self.unregister_greenlet(g)
            return getattr(self.reactor, "NEVER", _Never)

        return self.reactor.register_callback(invoke)

    spawn_routine = start_routine

    def spawn(self, name=None, payload=None, executor=None, starter=None):
        """Compatibility helper for adapters that pass a body payload.

        ``executor`` may accept ``(routine, payload)`` or just ``routine``.
        The latter is preferred because it keeps the routine identity explicit.
        """
        caller = starter.id if isinstance(starter, Routine) else starter
        if executor is None:
            return self.start_routine(name, caller)
        def runner(routine):
            try:
                positional = [p for p in inspect.signature(executor).parameters.values()
                               if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
            except (TypeError, ValueError):
                positional = [None, None]
            if len(positional) >= 2:
                executor(routine, payload)
            else:
                executor(routine)
        return self.start_routine(name, caller, runner=runner)

    def wait_for_routine(self, routine: Any, names=None) -> bool:
        """Wait helper accepting either a Routine or its identity."""
        rid = routine.id if isinstance(routine, Routine) else routine
        return self.wait(rid, names)

    def join(self, caller_id: Optional[str] = None) -> bool:
        """Bind a bare wait for the caller (the implicit end-of-run join)."""
        caller_id = caller_id or self.default_id
        return self.wait(caller_id, None)

    def observe_command(self, rid: str, command: str, detail=None, source=None):
        """Record detached command/status metadata at a command boundary."""
        r = self.check_active(rid)
        r.command = str(command)
        r.reply = {}
        r.detail = freeze(detail) if detail is not None else None
        r.source = freeze(source) if source is not None else None
        self.revision += 1

    def _resolve(self, caller_id: str, names: Optional[Iterable[str]]) -> Tuple[str, ...]:
        caller = self.get_routine(caller_id)
        if names is None:
            return tuple(x.id for x in sorted(self.routines.values(), key=lambda x: x.order)
                         if x.id not in (caller_id, self.default_id) and
                         (x.state != "completed" or x.id not in caller.collected))
        names = list(names)
        if not names or len(set(names)) != len(names):
            raise ContractError("An explicit ON list must be nonempty and distinct")
        if any(not isinstance(n, str) or not _IDENT_RE.fullmatch(n) for n in names):
            raise ContractError("Invalid routine name in WAIT ON")
        unknown = [n for n in names if n not in self.names]
        if unknown:
            raise ContractError("Unknown routine name in WAIT ON: %s" % unknown[0])
        return tuple(self.names[n] for n in names)

    def _cycle(self, caller_id: str, targets: Tuple[str, ...]) -> bool:
        todo = list(targets)
        seen = set()
        while todo:
            rid = todo.pop()
            if rid == caller_id:
                return True
            if rid in seen:
                continue
            seen.add(rid)
            target = self.get_routine(rid)
            if target.state == "waiting":
                todo.extend(target.targets)
        return False

    def _collect(self, caller: Routine) -> None:
        caller.waited = tuple(thaw(self.routines[t].result) for t in caller.targets)
        caller.collected.update(caller.targets)
        caller.targets = ()
        caller.state = "running"

    def wait(self, caller_id: str, names: Optional[Iterable[str]] = None) -> bool:
        """Atomically validate/bind a wait; true means it was already done."""
        caller = self.check_active(caller_id)
        targets = self._resolve(caller_id, names)
        if caller_id in targets or self._cycle(caller_id, targets):
            raise ContractError("Self-wait or circular dependency")
        if any(self.routines[t].state in ("failed", "cancelled") for t in targets):
            raise ContractError("Dependency did not succeed")
        # Fresh completion per wait makes repeated waits and multiple waiters safe.
        caller.completion = self._new_completion()
        caller.targets = targets
        if all(self.routines[t].state == "completed" for t in targets):
            self._collect(caller)
            self.revision += 1
            return True
        caller.state = "waiting"
        self.revision += 1
        return False

    def _complete(self, routine: Routine, value: Any) -> None:
        completion = routine.completion
        if completion is not None and not getattr(completion, "test", lambda: False)():
            completion.complete(value)

    def finish_routine(self, rid: str, result: Optional[dict] = None) -> bool:
        """Commit a successful result; late callbacks are ignored safely."""
        r = self.get_routine(rid)
        if r.state in ("completed", "failed", "cancelled") or self.fault is not None:
            return False
        if rid == self.default_id and any(x.id != rid and x.state != "completed"
                                          for x in self.routines.values()):
            raise ContractError("Implicit final WAIT required before run completion")
        if result is None:
            result = {}
        if type(result) is not dict:
            raise ContractError("A routine result must be a map")
        data = freeze(result)
        r.result = data
        r.state = "completed"
        r.targets = ()
        self._complete(r, data)
        for waiter in list(self.routines.values()):
            if waiter.state == "waiting" and all(self.routines[t].state == "completed"
                                                  for t in waiter.targets):
                self._collect(waiter)
                self._complete(waiter, True)
        self.revision += 1
        return True

    def fail_routine(self, rid: str, message: str) -> bool:
        r = self.get_routine(rid)
        if r.state in ("completed", "failed", "cancelled"):
            return False
        self.fault = str(message)
        r.state, r.error, r.targets = "failed", self.fault, ()
        self._complete(r, False)
        for other in self.routines.values():
            if other.id != rid and other.state in ("running", "waiting"):
                other.state, other.error, other.targets = "cancelled", "Run faulted", ()
                self._complete(other, False)
        self.generation += 1
        self.revision += 1
        return True

    def cancel_run(self, message: str = "Run cancelled") -> None:
        self.fault = str(message)
        self.is_cancelled = True
        self.generation += 1
        for r in self.routines.values():
            if r.state in ("running", "waiting"):
                r.state, r.error, r.targets = "cancelled", self.fault, ()
                self._complete(r, False)
        self.revision += 1

    cancel = cancel_run
    reset = cancel_run

    def set_reply(self, gcmd: Any, mapping: Dict[str, Any]) -> None:
        if type(mapping) is not dict:
            raise ContractError("set_reply mapping must be a dict")
        routine = self.current_routine()
        frame = self.current_frame(routine)
        if frame is None:
            raise ContractError("set_reply requires an active command frame")
        frozen = thaw(freeze(mapping))
        frame.reply = frozen
        routine.reply = dict(frozen)

    def set_command_reply(self, mapping: Dict[str, Any]) -> None:
        self.set_reply(None, mapping)

    def set_detail(self, gcmd: Any, **fields: Any) -> None:
        routine = self.current_routine()
        frame = self.current_frame(routine)
        if frame is None:
            raise ContractError("set_detail requires an active command frame")
        frozen = freeze(dict(fields))
        frame.detail = frozen
        routine.detail = frozen

    def set_routine_detail(self, fields: Dict[str, Any]) -> None:
        if type(fields) is not dict:
            raise ContractError("set_detail fields must be a dict")
        self.set_detail(None, **fields)

    def snapshot(self) -> dict:
        """Return a detached current-state snapshot safe for subscribers."""
        def detached(value):
            if value is None:
                return None
            # Integration adapters may attach ordinary mutable dictionaries
            # directly to a Routine; normalize those too before publication.
            return thaw(freeze(thaw(value)))

        return {"schema_version": 1, "run_id": self.run_id,
                "revision": self.revision, "fault": self.fault,
                "routines": [{
                    "id": r.id, "name": r.name, "state": r.state,
                    "command": r.command, "source": detached(r.source),
                    "waiting_on": list(r.targets), "detail": detached(r.detail),
                    "result": detached(r.result), "error": r.error,
                } for r in self.routines.values()]}

    get_status = snapshot
