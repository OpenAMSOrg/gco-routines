"""Hardware-free acceptance cases shared by the live runner and real-Klippy CI."""
from dataclasses import dataclass, field
import re


def probe(label, ms=0, value=0, fail=False):
    return "_GCO_TEST_PROBE LABEL=%s MS=%d VALUE=%d FAIL=%d" % (
        label, ms, value, fail)


def child(name, body):
    return "START%s\n%s\nEND" % (" NAME=" + name if name else "", body)


@dataclass(frozen=True)
class Case:
    name: str
    description: str
    script: str
    error: str = ""
    empty: bool = False
    required: tuple = ()
    before: tuple = ()
    overlap: tuple = ()
    values: dict = field(default_factory=dict)
    external: bool = False


CASES = (
    Case("sequential", "Ordinary commands remain sequential",
         probe("first", 30) + "\n" + probe("second", 30) + "\nWAIT",
         before=(("first:end", "second:begin"),)),
    Case("overlap", "Named child overlaps caller; explicit WAIT is a barrier",
         child("slow", probe("slow", 600)) + "\n" + probe("foreground", 250)
         + "\nWAIT ON=slow\n" + probe("after"),
         before=(("slow:end", "after:begin"),), overlap=(("slow", "foreground"),)),
    Case("child_wait", "One child waits without blocking its sibling/caller",
         child("source", probe("source", 500)) + "\n"
         + child("reader", "WAIT ON=source\n" + probe("reader")) + "\n"
         + child("independent", probe("independent", 80)) + "\n"
         + probe("foreground", 180) + "\nWAIT ON=reader,independent",
         before=(("source:end", "reader:begin"),
                 ("independent:end", "source:end"), ("foreground:end", "source:end"))),
    Case("case_sensitive", "Command keys insensitive; routine names sensitive",
         "start name=Alpha\n" + probe("upper", 20) + "\nend\n"
         + child("alpha", probe("lower", 20)) + "\nwait on=Alpha,alpha",
         required=("upper:end", "lower:end")),
    Case("api_eof", "Complete API return implicitly joins an uncollected child",
         child("implicit", probe("implicit", 200)) + "\n" + probe("foreground"),
         required=("implicit:end",)),
    Case("results", "Structured reply/result, explicit order, repeated WAIT",
         "GCO_TEST_RESULTS", required=("PASS_results:end",),
         before=(("beta:end", "alpha:end"),)),
    Case("anonymous", "Anonymous results, START order, completion before WAIT",
         "GCO_TEST_BARE", required=("PASS_bare:end",),
         before=(("second:end", "first:end"), ("first:end", "late_wait:end"))),
    Case("waiters", "Multiple independent waiters retain the same result",
         "GCO_TEST_WAITERS", required=("PASS_waiters:end",),
         values={"reader_a": 73, "reader_b": 73}),
    Case("reuse", "Collected names can be reused in a Jinja loop",
         "GCO_TEST_REUSE", required=("PASS_reuse:end",),
         values={"reuse_0": 0, "reuse_1": 1, "reuse_2": 2}),
    Case("bare_snapshot", "Child bare WAIT excludes caller/default and later starts",
         "GCO_TEST_BARE_SNAPSHOT", required=("PASS_snapshot:end",),
         before=(("collector:end", "later:begin"),)),
    Case("locals", "START snapshots, private locals, params, live printer state",
         "GCO_TEST_LOCALS VALUE=7 ENABLED=1", required=("PASS_locals:end", "live:end")),
    Case("branch", "Whole blocks in a false Jinja branch do not spawn",
         "GCO_TEST_LOCALS VALUE=7 ENABLED=0", required=("PASS_locals:end",)),
    Case("helpers", "Concurrent legacy helper calls and reply-frame isolation",
         "GCO_TEST_HELPERS", required=("PASS_helpers:end",),
         overlap=(("helper_a", "helper_b"),)),
    Case("external", "Unrelated HTTP G-code remains serialized by the real mutex",
         child("slow", probe("slow", 900)) + "\nWAIT ON=slow\n" + probe("after"),
         external=True, before=(("after:end", "external:begin"),)),
    Case("unclosed", "Complete-source preflight rejects before any prefix executes",
         probe("FORBIDDEN") + "\nSTART NAME=bad\n" + probe("FORBIDDEN"),
         error="E_UNCLOSED_START", empty=True),
    Case("direct_nested", "Direct nested blocks are rejected before execution",
         probe("FORBIDDEN") + "\nSTART\nSTART\nEND\nEND",
         error="E_NESTED_START", empty=True),
    Case("unmatched_end", "Unmatched END is rejected before prefix execution",
         probe("FORBIDDEN") + "\nEND", error="E_UNMATCHED_END", empty=True),
    Case("malformed_wait", "Malformed WAIT is never treated as ordinary G-code",
         probe("FORBIDDEN") + "\nWAIT ON=a,", error="E_WAIT_SYNTAX", empty=True),
    Case("reserved_name", "The default routine cannot be explicitly named",
         child("default", probe("FORBIDDEN")), error="E_RESERVED_NAME", empty=True),
    Case("transport", "Framed controls are rejected on unsupported ingress",
         "N1 START*0\n" + probe("FORBIDDEN") + "\nEND", error="E_TRANSPORT", empty=True),
    Case("unknown_target", "Unknown dependency fails closed",
         "WAIT ON=missing\n" + probe("FORBIDDEN"), error="Unknown routine name"),
    Case("wrong_case", "ON values are case-sensitive",
         child("Alpha", probe("alpha", 10)) + "\nWAIT ON=alpha\n" + probe("FORBIDDEN"),
         error="Unknown routine name"),
    Case("self_wait", "A child cannot wait on itself",
         child("self", "WAIT ON=self\n" + probe("FORBIDDEN")) + "\nWAIT",
         error="Self-wait|self.wait"),
    Case("cycle", "A child-to-child cycle faults rather than deadlocking",
         child("a", "WAIT ON=b") + "\n" + child("b", "WAIT ON=a")
         + "\nWAIT\n" + probe("FORBIDDEN"), error="circular dependency"),
    Case("uncollected_name", "A name cannot be reused before collection",
         child("busy", probe("busy", 100)) + "\n" + child("busy", probe("FORBIDDEN")),
         error="Name is in use"),
    Case("duplicate_target", "Duplicate all-of targets are invalid",
         child("a", probe("a", 20)) + "\nWAIT ON=a,a\n" + probe("FORBIDDEN"),
         error="distinct|duplicate|E_DUPLICATE"),
    Case("child_failure", "Child failure blocks subsequent caller commands",
         child("failure", probe("failure", 100, fail=True)) + "\n"
         + probe("admitted", 250) + "\n" + probe("FORBIDDEN") + "\nWAIT",
         error="GCO_TEST_EXPECTED_FAILURE", required=("failure:fail",)),
    Case("parent_failure", "Caller failure prevents a queued child from starting",
         child("queued", probe("FORBIDDEN")) + "\n" + probe("parent_failure", fail=True),
         error="GCO_TEST_EXPECTED_FAILURE", required=("parent_failure:fail",)),
    Case("failed_dependency", "Failure blocks both a dependent child and caller",
         child("failure", probe("failure", 100, fail=True)) + "\n"
         + child("dependent", "WAIT ON=failure\n" + probe("FORBIDDEN"))
         + "\nWAIT ON=dependent\n" + probe("FORBIDDEN"),
         error="GCO_TEST_EXPECTED_FAILURE", required=("failure:fail",)),
    Case("indirect_nested", "A child cannot invoke a spawning macro",
         child("outer", "GCO_TEST_RESULTS") + "\nWAIT\n" + probe("FORBIDDEN"),
         error="Nested spawning"),
    Case("missing_reply", "Missing structured fields are strict errors",
         "GCO_TEST_MISSING_REPLY", error="Managed result contains an undefined field"),
    Case("generated_control", "Rendered text cannot manufacture control syntax",
         "GCO_TEST_GENERATED_CONTROL TEXT=START", error="generated|Generated|E_GENERATED"),
)


def validate_status(status):
    if status.get("schema_version") != 1 or not isinstance(status.get("run_id"), str):
        raise AssertionError("Unsupported/malformed routine status")
    if not isinstance(status.get("revision"), int) or status["revision"] < 0:
        raise AssertionError("Invalid status revision")
    ids = set()
    for routine in status["routines"]:
        if routine["id"] in ids:
            raise AssertionError("Duplicate routine identity")
        ids.add(routine["id"])
        if routine["state"] not in ("running", "waiting", "completed", "failed", "cancelled"):
            raise AssertionError("Invalid routine state")
        if not isinstance(routine["waiting_on"], list):
            raise AssertionError("Malformed wait dependencies")
        if routine.get("result") is not None and not isinstance(routine["result"], dict):
            raise AssertionError("Malformed structured result")


def verify(case, events, status, error=""):
    """No elapsed-time pass thresholds: compare actual begin/end ordering."""
    validate_status(status)
    if case.error:
        if not error or not re.search(case.error, error):
            raise AssertionError("Expected %r, received %r" % (case.error, error))
    elif error:
        raise AssertionError("Unexpected command error: " + error)
    elif status.get("fault") or any(r["state"] != "completed" for r in status["routines"]):
        raise AssertionError("Request returned without a successful implicit/explicit join")
    if any(e["label"] == "FORBIDDEN" for e in events):
        raise AssertionError("A forbidden command executed after invalid source/dependency")
    if case.empty and events:
        raise AssertionError("Preflight did not reject the entire source before execution")
    by_name = {e["label"] + ":" + e["phase"]: e for e in events}
    for key in case.required:
        if key not in by_name:
            raise AssertionError("Missing observation: " + key)
    for first, second in case.before:
        if first not in by_name or second not in by_name:
            raise AssertionError("Missing ordering observations: %s, %s" % (first, second))
        if by_name[first]["sequence"] >= by_name[second]["sequence"]:
            raise AssertionError("Expected %s before %s" % (first, second))
    for a, b in case.overlap:
        if not (by_name[a + ":begin"]["sequence"] < by_name[b + ":end"]["sequence"]
                and by_name[b + ":begin"]["sequence"] < by_name[a + ":end"]["sequence"]):
            raise AssertionError("No observed overlap: %s / %s" % (a, b))
    for label, value in case.values.items():
        if by_name[label + ":end"]["value"] != value:
            raise AssertionError("Incorrect value for " + label)
    if case.name == "branch" and any(e["label"] == "live" for e in events):
        raise AssertionError("False branch executed")
