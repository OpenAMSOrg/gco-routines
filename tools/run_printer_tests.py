#!/usr/bin/env python3
"""Opt-in no-motion printer API acceptance. Default operation only lists cases.

Client HTTP threads allow observation while a request owns Klipper's real mutex;
they do not execute printer handlers or replace Klipper's cooperative reactor.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from printer_tests.suite import CASES, probe, validate_status, verify


class RemoteCommandError(RuntimeError):
    pass


class Client:
    def __init__(self, url, timeout=15.):
        parsed = urlsplit(url)
        if (parsed.scheme not in ("http", "https") or not parsed.netloc
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("Use an HTTP(S) Moonraker base URL without credentials, query, or fragment")
        self.url = url.rstrip("/")
        self.timeout = timeout

    def request(self, path, payload=None):
        headers = {"Content-Type": "application/json"}
        key = os.environ.get("MOONRAKER_API_KEY")
        if key:
            headers["X-Api-Key"] = key
        req = Request(self.url + path, headers=headers,
                      data=None if payload is None else json.dumps(payload).encode())
        try:
            with urlopen(req, timeout=self.timeout) as response:
                result = json.load(response)
        except HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            # Authentication, missing endpoints, and server failures are not
            # expected negative G-code results. Never count a timeout as one.
            if path == "/printer/gcode/script" and exc.code == 400:
                raise RemoteCommandError(body) from exc
            raise RuntimeError("HTTP %d on %s: %s" % (exc.code, path, body)) from exc
        if "error" in result:
            raise RuntimeError("API error on %s: %s" % (path, result["error"]))
        return result["result"]

    def script(self, source):
        return self.request("/printer/gcode/script", {"script": source})

    def query(self, names):
        return self.request("/printer/objects/query?" + "&".join(quote(n, safe="") for n in names))


def preflight(client):
    info = client.request("/printer/info")
    if info["state"] != "ready":
        raise RuntimeError("Klipper is not ready")
    names = client.request("/printer/objects/list")["objects"]
    required = {"gco_routines", "gco_routines_test", "pause_resume", "print_stats",
                "toolhead", "motion_report", "heaters", "idle_timeout"}
    if not required.issubset(names):
        raise RuntimeError("Missing required objects: " + ", ".join(sorted(required - set(names))))
    status = client.query(sorted(required))["status"]
    validate_status(status["gco_routines"])
    if status["gco_routines_test"].get("protocol_version") != 1:
        raise RuntimeError("Unsupported diagnostic extra version")
    if status["gco_routines_test"]["active"]:
        raise RuntimeError("A test probe is still active")
    if status["pause_resume"]["is_paused"] or status["print_stats"]["state"] in ("printing", "paused"):
        raise RuntimeError("Printer is printing or paused; leave the current job alone")
    if status["idle_timeout"]["state"] == "Printing":
        raise RuntimeError("Printer reports active command/motion work; wait for idle")
    motion = status["motion_report"]
    if abs(motion["live_velocity"]) > .001 or abs(motion["live_extruder_velocity"]) > .001:
        raise RuntimeError("Printer is moving")
    for r in status["gco_routines"]["routines"]:
        if r["state"] == "waiting" or (r["name"] != "default" and r["state"] == "running"):
            raise RuntimeError("A concurrent routine is active")
    heaters = status["heaters"]["available_heaters"]
    temperatures = client.query(heaters)["status"] if heaters else {}
    if any(s["target"] != 0 or s["temperature"] >= 50 for s in temperatures.values()):
        raise RuntimeError("No-motion suite requires all heaters off and below 50C")
    help_commands = client.request("/printer/gcode/help")
    commands = {"START", "END", "WAIT", "_GCO_TEST_RESET", "_GCO_TEST_PROBE"}
    commands.update(case.script.split()[0] for case in CASES if case.script.startswith("GCO_TEST_"))
    # Internal probe commands may intentionally have no HELP description.
    macros = {"gcode_macro " + c for c in commands if c.startswith("GCO_TEST_")}
    if not macros.issubset(names):
        raise RuntimeError("Diagnostic macros are missing; include printer_tests/macros.cfg")
    if not {"START", "END", "WAIT"}.issubset(help_commands):
        raise RuntimeError("Concurrent controls are not registered")
    return {"printer": info, "status": status, "heaters": temperatures}


def run_case(client, case, record=None):
    record = {} if record is None else record
    record.update(case=case.name, passed=False, samples=[])
    client.script("_GCO_TEST_RESET")
    samples = record["samples"]
    error = ""
    external = None
    response_snapshot = None
    deadline = time.monotonic() + 20
    # These are HTTP clients only. All actual G-code still passes through the
    # unchanged server dispatcher/mutex and the installed routine adapter.
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(client.script, case.script)
        while True:
            # A snapshot requested before HTTP completion can legitimately show
            # an active routine even if the request finishes while it travels.
            # Only use a fresh, post-response snapshot for the join assertion.
            completed_before_sample = future.done()
            external_done_before_sample = (not case.external or
                                           (external is not None and external.done()))
            sample = client.query(["gco_routines", "gco_routines_test", "motion_report",
                                   "pause_resume"])
            samples.append(sample)
            diag = sample["status"]["gco_routines_test"]
            motion = sample["status"]["motion_report"]
            if abs(motion["live_velocity"]) > .001 or abs(motion["live_extruder_velocity"]) > .001:
                raise RuntimeError("Unexpected motion during diagnostic; inspect before continuing")
            if diag["epoch"] != samples[0]["status"]["gco_routines_test"]["epoch"]:
                raise RuntimeError("Another operator reset the diagnostic journal")
            if case.external and external is None and any(
                    e["label"] == "slow" and e["phase"] == "begin" for e in diag["events"]):
                external = pool.submit(client.script, probe("external"))
            if completed_before_sample and response_snapshot is None:
                # Snapshot as soon as return is seen, before waiting for already
                # admitted native probes to unwind after a negative test.
                response_snapshot = sample["status"]["gco_routines"]
                try:
                    future.result()
                except RemoteCommandError as exc:
                    error = str(exc)
                    record["expected_error"] = error
            if completed_before_sample and not diag["active"] and external_done_before_sample:
                break
            if time.monotonic() > deadline:
                raise TimeoutError("Test did not settle; do not retry automatically")
            time.sleep(.05)
        if external is not None:
            external.result()
        elif case.external:
            raise AssertionError("Did not observe the serialization test start")
    last = samples[-1]["status"]
    record["response_status"] = response_snapshot
    if not case.error and response_snapshot is not None:
        if any(r["state"] != "completed" for r in response_snapshot["routines"]):
            raise AssertionError("API returned before its run completed")
    verify(case, last["gco_routines_test"]["events"], last["gco_routines"], error)
    if case.name == "overlap" and not any(
            r.get("detail", {}) and r["detail"].get("phase") == "waiting"
            and r["detail"].get("label") == "slow"
            for s in samples for r in s["status"]["gco_routines"]["routines"]):
        raise AssertionError("Did not observe driver detail during the active child")
    previous = {}
    for sample in samples:
        status = sample["status"]["gco_routines"]
        validate_status(status)
        old = previous.get(status["run_id"], -1)
        if status["revision"] < old:
            raise AssertionError("Status revision regressed within a run")
        previous[status["run_id"]] = status["revision"]
        if sample["status"]["pause_resume"]["is_paused"]:
            raise AssertionError("No-motion diagnostic unexpectedly paused the printer")
    record.update(passed=True, expected_error=error)
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("list", "preflight", "run"), nargs="?", default="list")
    parser.add_argument("--url", help="Moonraker base URL; no automatic default target")
    parser.add_argument("--case", action="append", dest="cases", choices=[c.name for c in CASES])
    parser.add_argument("--report", type=Path, help="New JSON report path (never overwritten)")
    args = parser.parse_args(argv)
    cases = [c for c in CASES if not args.cases or c.name in args.cases]
    if args.action == "list":
        for c in cases:
            print("%-19s %s%s" % (c.name, c.description, " [expected error]" if c.error else ""))
        return 0
    if not args.url:
        parser.error("--url is required; there is no implicit printer target")
    if args.action == "run" and args.report is None:
        parser.error("run requires --report to preserve evidence")
    client = Client(args.url)
    if args.action == "preflight":
        preflight(client)
        print("Preflight passed. No G-code sent; no settings changed.")
        return 0
    # Claim the report path before sending even diagnostic commands.
    with args.report.open("x") as output:
        report = {"started_utc": datetime.now(timezone.utc).isoformat(),
                  "url": client.url, "scope": "hardware-free API", "results": []}
        try:
            report["preflight"] = preflight(client)
            baseline = report["preflight"]["status"]["motion_report"]["live_position"]
            for case in cases:
                result = {}
                report["results"].append(result)
                run_case(client, case, result)
                position = result["samples"][-1]["status"]["motion_report"]["live_position"]
                if any(abs(a - b) > .001 for a, b in zip(baseline, position)):
                    result["passed"] = False
                    raise RuntimeError("Printer position changed; stop and inspect concurrent activity")
                print("PASS " + case.name, flush=True)
            report["passed"] = True
        except Exception as exc:
            report["passed"] = False
            report["error"] = str(exc)
            print("STOP: %s. No automatic retry, reset, restart, or hardware cleanup." % exc,
                  file=sys.stderr)
        finally:
            json.dump(report, output, indent=2)
            output.write("\n")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
