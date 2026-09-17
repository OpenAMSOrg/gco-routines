"""Executable reference for identities, waits, results and snapshots.

This is NOT a scheduler or Klipper extra. Tests call transitions explicitly;
no G-code, Jinja or device operation executes here. Production should reuse
these invariants with reactor completions and bounded, frame-owned state.
"""
from dataclasses import dataclass, field
from types import MappingProxyType
import math
from typing import Optional

class ContractError(ValueError):
    pass

def freeze(value, depth=0, budget=None):
    """Detach and freeze plain JSON-like results; reject Undefined/NaN/objects."""
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
        return tuple(freeze(x, depth + 1, budget) for x in value)
    if type(value) is dict:
        if any(type(k) is not str for k in value):
            raise ContractError("Result map keys must be strings")
        return MappingProxyType({k: freeze(v, depth + 1, budget) for k, v in value.items()})
    raise ContractError(f"Unsupported result type: {type(value).__name__}")

def thaw(value):
    if isinstance(value, MappingProxyType):
        return {k: thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [thaw(v) for v in value]
    return value

@dataclass
class Routine:
    id: str
    name: Optional[str]
    starter: Optional[str]
    order: int
    state: str = "running"
    targets: tuple = ()
    collected: set = field(default_factory=set)
    waited: tuple = ()
    result: object = None
    error: Optional[str] = None
    command: Optional[str] = None
    detail: object = None
    source: object = None

class Registry:
    """Run-local retained state; caller IDs are never resolved by display name."""
    def __init__(self, run_id="run1", max_routines=1024):
        self.run_id = run_id
        self.default = f"{run_id}:0"
        self.routines = {self.default: Routine(self.default, "default", None, 0)}
        self.names = {}
        self.revision = 0
        self.max_routines = max_routines
        self.fault = None

    def _get(self, rid):
        if rid not in self.routines:
            raise ContractError("Unknown routine identity")
        return self.routines[rid]

    def _active(self, rid):
        r = self._get(rid)
        if self.fault is not None:
            raise ContractError("Run is faulted")
        if r.state != "running":
            raise ContractError("Routine is not runnable")
        return r

    def start(self, name=None, caller=None):
        from .frontend import IDENT
        import re
        caller = self.default if caller is None else caller
        self._active(caller)
        if caller != self.default:
            raise ContractError("Nested spawning is not permitted in v0.2")
        if name is not None:
            if not re.fullmatch(IDENT, name) or name == "default":
                raise ContractError("Invalid or reserved routine name")
            if name in self.names:
                old = self.routines[self.names[name]]
                if old.state != "completed" or old.id not in self.routines[old.starter].collected:
                    raise ContractError("Name is in use; starter must successfully collect its completed instance")
        if len(self.routines) >= self.max_routines:
            raise ContractError("Routine retention limit exceeded")
        order = len(self.routines)
        rid = f"{self.run_id}:{order}"
        self.routines[rid] = Routine(rid, name, caller, order)
        if name is not None:
            self.names[name] = rid
        self.revision += 1
        return rid

    def _resolve(self, caller, names):
        r = self._get(caller)
        if names is None:
            return tuple(x.id for x in self.routines.values()
                         if x.id not in (caller, self.default)
                         and (x.state != "completed" or x.id not in r.collected))
        if not names or len(set(names)) != len(names):
            raise ContractError("An explicit ON list must be nonempty and distinct")
        if any(n not in self.names for n in names):
            raise ContractError("Unknown routine name")
        return tuple(self.names[n] for n in names)

    def _cycle(self, caller, targets):
        for target in targets:
            todo, seen = [target], set()
            while todo:
                rid = todo.pop()
                if rid == caller:
                    return True
                if rid in seen:
                    continue
                seen.add(rid)
                r = self._get(rid)
                if r.state == "waiting":
                    todo.extend(r.targets)
        return False

    def _collect(self, r):
        r.waited = tuple(self.routines[t].result for t in r.targets)
        r.collected.update(r.targets)
        r.targets = ()
        r.state = "running"

    def wait(self, caller, names=None):
        """True=already satisfied; False=caller now suspended. Atomic validation."""
        r = self._active(caller)
        targets = self._resolve(caller, names)
        if caller in targets or self._cycle(caller, targets):
            raise ContractError("Self-wait or circular dependency")
        if any(self.routines[t].state in ("failed", "cancelled") for t in targets):
            raise ContractError("Dependency did not succeed")
        r.targets = targets
        r.state = "waiting"
        done = all(self.routines[t].state == "completed" for t in targets)
        if done:
            self._collect(r)
        self.revision += 1
        return done

    def finish(self, rid, result=None):
        r = self._active(rid)
        if rid == self.default and any(x.id != rid and x.state != "completed" for x in self.routines.values()):
            raise ContractError("Implicit final WAIT required before run completion")
        if result is None:
            result = {}
        if type(result) is not dict:
            raise ContractError("A routine result must be a map")
        # Validate the whole result before committing completion.
        data = freeze(result)
        r.result, r.state = data, "completed"
        for waiter in self.routines.values():
            if waiter.state == "waiting" and all(self.routines[t].state == "completed" for t in waiter.targets):
                self._collect(waiter)
        self.revision += 1

    def fail(self, rid, message):
        r = self._get(rid)
        if r.state not in ("running", "waiting"):
            raise ContractError("Late failure for terminal routine")
        self.fault = str(message)
        r.state, r.error, r.targets = "failed", self.fault, ()
        for other in self.routines.values():
            if other.id != rid and other.state in ("running", "waiting"):
                other.state, other.error, other.targets = "cancelled", "Run faulted", ()
        self.revision += 1
        # 'cancelled' here ONLY means no further software commands admitted.
        # It is not acknowledgment of a physical stop; production needs device policy.

    def cancel(self, message="Run cancelled"):
        self.fault = str(message)
        for r in self.routines.values():
            if r.state in ("running", "waiting"):
                r.state, r.error, r.targets = "cancelled", self.fault, ()
        self.revision += 1

    def observe_command(self, rid, command, detail=None, source=None):
        r = self._active(rid)
        r.command = str(command)
        r.detail = freeze(detail or {})
        r.source = freeze(source or {})
        self.revision += 1

    def snapshot(self):
        return {"schema_version": 1, "run_id": self.run_id,
                "revision": self.revision, "fault": self.fault,
                "routines": [{"id": r.id, "name": r.name, "state": r.state,
                              "command": r.command, "source": thaw(r.source),
                              "waiting_on": list(r.targets), "detail": thaw(r.detail),
                              "result": thaw(r.result), "error": r.error}
                             for r in self.routines.values()]}
