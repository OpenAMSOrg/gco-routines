"""Handled regions (docs/decisions.md, 2026-11-08): a host component runs
commands whose errors it handles itself, without faulting the run."""

import importlib.util
import json
from pathlib import Path

import jsonschema
import pytest

from test_native_runtime import Reactor


_ROOT = Path(__file__).parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "gco_native_runtime_handled", _ROOT / "klippy_extra/gco_routines/runtime.py")
runtime = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(runtime)


def make_run():
    return runtime.Run("r", Reactor())


class Boom(Exception):
    pass


def test_the_owners_own_error_does_not_fault_the_run_inside_the_region():
    run = make_run()
    owner = run.routines[run.default_id]
    with pytest.raises(Boom):
        with run.handled_errors(owner.id):
            # What the dispatcher does when one of the owner's commands raises.
            assert run.fail_routine(owner.id, "cutter jammed") is False
            raise Boom()
    assert run.fault is None and owner.state == "running"
    # Outside the region the same error faults the run, as before.
    assert run.fail_routine(owner.id, "cutter jammed") is True
    assert run.fault == "cutter jammed"


def test_a_routine_started_inside_the_region_fails_alone():
    run = make_run()
    owner = run.routines[run.default_id]
    outside = run.start_routine("outside")
    with run.handled_errors(owner.id):
        feed = run.start_routine("feed")
        assert feed.handled_by == owner.id and outside.handled_by is None
        assert run.wait(owner.id, ["feed"]) is False
        assert run.fail_routine(feed.id, "the AMS refused the load") is True
        assert run.fault is None
        # The other routine is not cancelled; the waiter is woken with the reason.
        assert outside.state == "running"
        assert feed.state == "failed" and feed.error == "the AMS refused the load"
        assert owner.state == "running" and owner.targets == ()
        assert owner.dependency_error == "Routine feed failed: the AMS refused the load"
        assert owner.completion.done and owner.completion.value is False
    assert feed.retired and feed.handled_by is None
    run.finish_routine(outside.id, {})


def test_a_routine_started_outside_the_region_still_faults_the_run():
    run = make_run()
    owner = run.routines[run.default_id]
    other = run.start_routine("other")
    with run.handled_errors(owner.id):
        assert run.fail_routine(other.id, "unrelated failure") is True
    assert run.fault == "unrelated failure"


def test_an_error_leaving_the_region_cancels_its_unfinished_routines():
    run = make_run()
    owner = run.routines[run.default_id]
    keep = run.start_routine("keep")
    with pytest.raises(Boom):
        with run.handled_errors(owner.id):
            feed = run.start_routine("feed")
            raise Boom()
    assert run.fault is None
    assert feed.state == "cancelled" and feed.retired
    assert keep.state == "running" and not keep.retired
    # A later failure of the cancelled routine's in-flight command is ignored.
    assert run.fail_routine(feed.id, "late") is False and run.fault is None
    run.finish_routine(keep.id, {})


def test_retired_routines_block_neither_a_bare_wait_nor_the_final_join():
    run = make_run()
    owner = run.routines[run.default_id]
    with run.handled_errors(owner.id):
        feed = run.start_routine("feed")
        run.fail_routine(feed.id, "refused")
        owner.dependency_error = None
    done = run.start_routine("done")
    run.finish_routine(done.id, {"ok": True})
    assert run.wait(owner.id) is True
    assert owner.waited == ({"ok": True},)
    # Run completion (the implicit final wait) is not held by the retired routine.
    assert run.finish_routine(owner.id, {}) is True
    # An explicit WAIT on it still reports the failure.
    run2 = make_run()
    owner2 = run2.routines[run2.default_id]
    with run2.handled_errors(owner2.id):
        failed = run2.start_routine("feed")
        run2.fail_routine(failed.id, "refused")
    with pytest.raises(runtime.ContractError, match="Dependency did not succeed"):
        run2.wait(owner2.id, ["feed"])


def test_after_a_normal_exit_a_running_routine_is_ordinary_again():
    run = make_run()
    owner = run.routines[run.default_id]
    with run.handled_errors(owner.id):
        later = run.start_routine("later")
    assert later.handled_by is None and later.state == "running" and not later.retired
    # Its failure now faults the run like any other routine's.
    assert run.fail_routine(later.id, "late failure") is True
    assert run.fault == "late failure"


def test_nested_regions_close_with_the_outermost():
    run = make_run()
    owner = run.routines[run.default_id]
    with run.handled_errors(owner.id):
        with run.handled_errors(owner.id):
            inner = run.start_routine("inner")
        assert run.is_handled(owner.id) and inner.handled_by == owner.id
        run.fail_routine(inner.id, "refused")
        assert run.fault is None
    assert not run.is_handled(owner.id) and inner.retired


def test_a_region_cannot_open_on_a_faulted_run():
    run = make_run()
    owner = run.routines[run.default_id]
    run.fail_routine(owner.id, "already faulted")
    with pytest.raises(runtime.RunFaultError):
        with run.handled_errors(owner.id):
            pass


def test_the_status_keeps_its_schema():
    run = make_run()
    owner = run.routines[run.default_id]
    with run.handled_errors(owner.id):
        feed = run.start_routine("feed")
        run.fail_routine(feed.id, "refused")
    status = run.snapshot()
    with open(_ROOT / "schemas" / "status.schema.json") as f:
        jsonschema.validate(instance=status, schema=json.load(f))
    assert status["fault"] is None
    routine = next(r for r in status["routines"] if r["name"] == "feed")
    assert (routine["state"], routine["error"]) == ("failed", "refused")
