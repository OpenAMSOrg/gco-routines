"""Client safety and acceptance-oracle regressions; no network or real devices."""
import copy
import json

import pytest
from printer_tests.suite import CASES, verify
from tools.run_printer_tests import Client, main, preflight


class FakeClient:
    def __init__(self):
        self.url = "http://test"
        self.status = {
            "gco_routines": {"schema_version": 1, "run_id": "idle", "revision": 0,
                             "routines": [{"id": "idle:0", "name": "default",
                                           "state": "running", "waiting_on": []}]},
            "gco_routines_test": {"protocol_version": 1, "active": 0},
            "pause_resume": {"is_paused": False},
            "print_stats": {"state": "standby"}, "toolhead": {},
            "idle_timeout": {"state": "Ready"},
            "motion_report": {"live_velocity": 0., "live_extruder_velocity": 0.,
                              "live_position": [0, 0, 0, 0]},
            "heaters": {"available_heaters": ["extruder"]},
            "extruder": {"temperature": 23., "target": 0.},
        }
        self.objects = list(self.status) + ["gcode_macro " + c.script.split()[0]
                                           for c in CASES if c.script.startswith("GCO_TEST_")]
        self.commands = []

    def request(self, path):
        if path == "/printer/info":
            return {"state": "ready"}
        if path == "/printer/objects/list":
            return {"objects": self.objects}
        if path == "/printer/gcode/help":
            return {"START": "", "END": "", "WAIT": ""}
        raise AssertionError(path)

    def query(self, names):
        return {"status": {n: copy.deepcopy(self.status[n]) for n in names}}

    def script(self, source):
        self.commands.append(source)
        raise AssertionError("Preflight must not send G-code")


def test_preflight_is_read_only_and_accepts_cold_idle_printer():
    client = FakeClient()
    preflight(client)
    assert not client.commands


@pytest.mark.parametrize("object_name,field,value", [
    ("pause_resume", "is_paused", True), ("print_stats", "state", "printing"),
    ("idle_timeout", "state", "Printing"), ("extruder", "target", 50.),
    ("extruder", "temperature", 50.), ("motion_report", "live_velocity", 1.),
    ("gco_routines_test", "active", 1), ("gco_routines_test", "protocol_version", 2),
])
def test_preflight_rejects_busy_hot_or_incompatible_printer(object_name, field, value):
    client = FakeClient()
    client.status[object_name][field] = value
    with pytest.raises(RuntimeError):
        preflight(client)
    assert not client.commands


def test_preflight_missing_extra_fails_closed():
    client = FakeClient()
    client.objects.remove("gco_routines")
    with pytest.raises(RuntimeError, match="Missing"):
        preflight(client)


def test_default_cli_only_lists_and_never_connects(monkeypatch, capsys):
    monkeypatch.setattr(Client, "request", lambda *a: pytest.fail("Unexpected network"))
    assert main([]) == 0
    assert "overlap" in capsys.readouterr().out


def test_run_preserves_failure_report_and_never_retries(monkeypatch, tmp_path):
    from tools import run_printer_tests as runner
    client = FakeClient()
    monkeypatch.setattr(runner, "Client", lambda url: client)
    def fail(client, case, record):
        record.update(case=case.name, passed=False, samples=[{"evidence": "kept"}])
        raise TimeoutError("unknown outcome")
    monkeypatch.setattr(runner, "run_case", fail)
    path = tmp_path / "report.json"
    assert runner.main(["run", "--url", "http://test", "--report", str(path)]) == 1
    data = json.loads(path.read_text())
    assert not data["passed"] and len(data["results"]) == 1
    assert data["results"][0]["samples"] == [{"evidence": "kept"}]
    with pytest.raises(FileExistsError):
        runner.main(["run", "--url", "http://test", "--report", str(path)])


def test_serial_schedule_cannot_pass_overlap_case():
    case = next(c for c in CASES if c.name == "overlap")
    events = [dict(label=label, phase=phase, sequence=i, value=0, time=float(i))
              for i, (label, phase) in enumerate([
                  ("foreground", "begin"), ("foreground", "end"),
                  ("slow", "begin"), ("slow", "end"), ("after", "begin")])]
    status = {"schema_version": 1, "run_id": "x", "revision": 1, "routines": []}
    with pytest.raises(AssertionError, match="overlap"):
        verify(case, events, status)


def test_unrelated_error_cannot_pass_negative_case():
    case = next(c for c in CASES if c.name == "unclosed")
    status = {"schema_version": 1, "run_id": "x", "revision": 1, "routines": []}
    with pytest.raises(AssertionError, match="Expected"):
        verify(case, [], status, "network connection refused")


def test_runner_uses_post_response_not_inflight_snapshot(monkeypatch):
    from tools import run_printer_tests as runner
    class Future:
        finished = False
        def done(self):
            return self.finished
        def result(self):
            return "ok"
    future = Future()
    class Pool:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def submit(self, *args):
            return future
    client = FakeClient()
    client.script = lambda source: None
    calls = []
    def query(names):
        done = future.finished
        # Response completes while the earlier status query is in flight.
        future.finished = True
        calls.append(done)
        status = copy.deepcopy(client.status)
        status["gco_routines"]["routines"][0]["state"] = "completed" if done else "waiting"
        status["gco_routines_test"].update(epoch=1, active=0 if done else 1,
            events=[dict(label="implicit", phase="end", sequence=0, value=0)])
        return {"status": {n: status[n] for n in names}}
    client.query = query
    monkeypatch.setattr(runner, "ThreadPoolExecutor", Pool)
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: None)
    case = next(c for c in CASES if c.name == "api_eof")
    result = runner.run_case(client, case)
    assert result["passed"] and calls == [False, True]
    assert result["response_status"]["routines"][0]["state"] == "completed"
