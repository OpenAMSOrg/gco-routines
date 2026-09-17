"""Focused tests for the reactor-independent native runtime contract."""

import importlib.util
from pathlib import Path

import pytest


_ROOT = Path(__file__).parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "gco_native_runtime", _ROOT / "klippy_extra/gco_routines/runtime.py")
runtime = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(runtime)


class Completion:
    def __init__(self):
        self.value = None
        self.done = False

    def test(self):
        return self.done

    def complete(self, value):
        if not self.done:
            self.value = value
            self.done = True

    def wait(self, *unused):
        return self.value


class Reactor:
    NEVER = object()

    def __init__(self):
        self.callbacks = []

    def completion(self):
        return Completion()

    def register_callback(self, callback, *unused):
        completion = Completion()
        self.callbacks.append((callback, completion))
        return completion

    def run_one(self):
        callback, completion = self.callbacks.pop(0)
        value = callback(0.0)
        completion.complete(value)


def make_run(**kwargs):
    return runtime.Run("r", Reactor(), **kwargs)


def test_multiple_waiters_retain_and_order_results():
    run = make_run()
    a = run.start_routine("a")
    b = run.start_routine("b")
    run.finish_routine(b.id, {"which": "b"})
    waiter_a = run.routines[run.default_id]
    assert run.wait(waiter_a.id, ["b"]) is True
    assert waiter_a.waited == ({"which": "b"},)
    # A second explicit read is retained and does not consume b's result.
    assert run.wait(waiter_a.id, ["b"]) is True
    assert waiter_a.waited == ({"which": "b"},)
    run.finish_routine(a.id, {"which": "a"})


def test_bare_wait_is_start_ordered_and_name_reuse_requires_collection():
    run = make_run()
    a = run.start_routine("a")
    b = run.start_routine("b")
    with pytest.raises(runtime.ContractError):
        run.start_routine("a")
    run.finish_routine(b.id, {"n": 2})
    run.finish_routine(a.id, {"n": 1})
    assert run.wait(run.default_id) is True
    assert run.routines[run.default_id].waited == ({"n": 1}, {"n": 2})
    run.start_routine("a")


def test_wait_graph_validation_is_atomic():
    run = make_run()
    a = run.start_routine("a")
    b = run.start_routine("b")
    # Simulate each background routine being the caller to exercise cycles.
    with pytest.raises(runtime.ContractError):
        run.wait(a.id, ["b", "missing"])
    assert a.state == "running" and a.targets == ()
    assert run.wait(a.id, ["b"]) is False
    with pytest.raises(runtime.ContractError):
        run.wait(b.id, ["a"])
    assert b.state == "running" and b.targets == ()


def test_cancel_invalidates_scheduled_callback_and_freezes_data():
    reactor = Reactor()
    run = runtime.Run("r", reactor)
    child = run.start_routine("child", runner=lambda r: run.finish_routine(
        r.id, {"v": [1, 2]}))
    run.cancel_run("reset")
    reactor.run_one()
    assert child.state == "cancelled"
    assert child.result is None
    assert run.finish_routine(child.id, {"late": True}) is False


def test_frame_owned_reply_and_detached_snapshot():
    run = make_run()
    routine = run.routines[run.default_id]
    with run.command_frame(routine, "T0") as frame:
        run.set_command_reply({"lane": 1})
        run.set_routine_detail({"phase": "load"})
        assert frame.reply == {"lane": 1}
    snapshot = run.snapshot()
    snapshot["routines"][0]["detail"]["phase"] = "mutated"
    assert routine.detail["phase"] == "load"
    with pytest.raises(runtime.ContractError):
        run.finish_routine(routine.id, {"bad": object()})
