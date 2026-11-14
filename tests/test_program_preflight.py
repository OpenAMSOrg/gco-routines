"""Streaming virtual-SD preflight: no whole-file cap, same structural errors."""
import pytest

import random

from klippy_extra.gco_routines import program
from klippy_extra.gco_routines.program import (
    ProgramError, parse_program, preflight_file,
)

_LINE = "G1 X123.456 Y78.901 E0.12345 F3000 ; ordinary move\n"
_BIG = 50_000_000 + 4096   # above the removed 50 MB whole-file limit


def _write_big(path, tail=""):
    chunk = _LINE * 2000
    written = 0
    with open(path, "w") as f:
        while written < _BIG:
            f.write(chunk)
            written += len(chunk)
        f.write(tail)
    return written // len(_LINE)


def test_control_free_file_over_50_mb_passes_preflight(tmp_path):
    path = tmp_path / "large.gcode"
    try:
        _write_big(path, tail="START NAME=late\nG1 X1\nEND\nWAIT ON=late\n")
        assert path.stat().st_size > 50_000_000
        preflight_file(str(path))
    finally:
        path.unlink()


def test_unclosed_block_near_end_of_large_file_is_rejected(tmp_path):
    path = tmp_path / "large_unclosed.gcode"
    try:
        lines = _write_big(path, tail="START NAME=tail\nG1 X1\n")
        with pytest.raises(ProgramError,
                           match="E_UNCLOSED_START: START at line %d " % (lines + 1)):
            preflight_file(str(path))
    finally:
        path.unlink()


def test_line_length_sanity_check(tmp_path):
    path = tmp_path / "long.gcode"
    path.write_text("G1 X1\n" + "M117 " + "x" * 59 + "\nG1 X2")
    preflight_file(str(path), max_line_chars=64)        # exactly at the limit
    path.write_text("G1 X1\n" + "M117 " + "x" * 60 + "\nG1 X2")
    with pytest.raises(ProgramError, match="E_SOURCE_LIMIT: line 2 exceeds 64"):
        preflight_file(str(path), max_line_chars=64)


def test_missing_file_is_rejected(tmp_path):
    with pytest.raises(ProgramError, match="does not exist"):
        preflight_file(str(tmp_path / "absent.gcode"))


SOURCES = [
    "",
    "G28\nM109 S200\n",
    "START\nT0\nEND\nWAIT\n",
    "  start name=Job ; comment\n\tT0\n end \nwait on=Job\n",
    "START NAME=a\r\nT0\r\nEND\r\nWAIT ON=a\r\n",       # CRLF
    "START NAME=a\rT0\rEND\rWAIT ON=a",                  # bare CR, no final newline
    "G1 X1\nSTART NAME=oops\nG1 X2\n",                   # unclosed
    "START\nG1\nSTART\nEND\nEND\n",                       # nested
    "G1\nEND\n",                                          # unmatched END
    "START NAME=default\nEND\n",                          # reserved name
    "START NAME={params.X}\nEND\n",                       # non-literal name
    "START\nWAIT ON=a,,b\nEND\n",                         # malformed control in block
    "START\nEND now\n",                                   # END with arguments
    "N10 START\nEND\n",                                   # transport framing
    "START\n  N20 WAIT\nEND\n",                           # transport framing in block
    "M117 STARTED\nENDSTOP_CHECK\nWAITER\nN5 G1 X1\n",    # control-like heads
    ";START\n  ; END\nG1 ; WAIT\n",                       # commented controls
    "G1 X1\n\tWAIT ON=a\n",                                # tab before a control
    "G1 X1\r\n n7 end\r\n",                                # framing after a space
    "EXCLUDE_OBJECT_START NAME=a\nSET_PRINT_STATS_INFO\n",  # slicer lines
]


@pytest.mark.parametrize("source", SOURCES)
def test_streaming_preflight_matches_whole_source_parse(tmp_path, source):
    path = tmp_path / "case.gcode"
    path.write_bytes(source.encode("utf-8"))

    def outcome(check):
        try:
            check()
        except ProgramError as exc:
            return str(exc)
        return None
    expected = outcome(lambda: parse_program(source, source_id=str(path)))
    assert outcome(lambda: preflight_file(str(path))) == expected


def _outcome(check):
    try:
        check()
    except ProgramError as exc:
        return str(exc)
    return None


@pytest.mark.parametrize("chunk", [1, 2, 7, 64])
def test_tiny_reads_split_lines_anywhere_and_still_agree(tmp_path, monkeypatch,
                                                         chunk):
    """The fast pass reads in steps; a line, a CRLF pair or a keyword cut by
    a step boundary must not change the outcome."""
    monkeypatch.setattr(program, "PREFLIGHT_CHUNK_BYTES", chunk)
    for number, source in enumerate(SOURCES):
        path = tmp_path / ("case%d.gcode" % number)
        path.write_bytes(source.encode("utf-8"))
        expected = _outcome(lambda: parse_program(source, source_id=str(path)))
        assert _outcome(lambda: preflight_file(str(path))) == expected, source


def test_random_sources_agree_with_the_whole_source_parse(tmp_path, monkeypatch):
    rng = random.Random(20261113)
    pieces = ["G1 X1", "START", "START NAME=a", "END", "WAIT", "WAIT ON=a",
              "  end", "\tstart name=b", "N3 START", "n4 wait", "; START",
              "M117 STARTED", "ENDSTOP", "EXCLUDE_OBJECT_END NAME=a", ""]
    ends = ["\n", "\r\n", "\r"]
    monkeypatch.setattr(program, "PREFLIGHT_CHUNK_BYTES", 5)
    for number in range(300):
        source = "".join(rng.choice(pieces) + rng.choice(ends)
                         for _ in range(rng.randint(0, 8)))
        path = tmp_path / ("random%d.gcode" % number)
        path.write_bytes(source.encode("utf-8"))
        expected = _outcome(lambda: parse_program(source, source_id=str(path)))
        assert _outcome(lambda: preflight_file(str(path))) == expected, source


def test_a_sliced_file_never_reaches_the_line_by_line_pass(tmp_path,
                                                           monkeypatch):
    """The 2026-11-13 incident: a 2.5-million-line OrcaSlicer file held
    Klipper's reactor for 4.4 s on a Pi while every line went through
    Python. A file with no control line is settled by the fast pass."""
    path = tmp_path / "sliced.gcode"
    layer = ("EXCLUDE_OBJECT_START NAME=part\n"
             "G1 X10.5 Y20.25 E0.04512\n" * 50
             + "EXCLUDE_OBJECT_END NAME=part\n"
             "SET_PRINT_STATS_INFO CURRENT_LAYER=1\n;LAYER_CHANGE\n")
    path.write_text(layer * 400)

    def no_exact_pass(*args, **kwargs):
        raise AssertionError("the exact pass ran")
    monkeypatch.setattr(program, "BlockCollector", no_exact_pass)
    preflight_file(str(path))


def test_preflight_lets_the_reactor_run_between_steps(tmp_path, monkeypatch):
    monkeypatch.setattr(program, "PREFLIGHT_CHUNK_BYTES", 4096)
    path = tmp_path / "steps.gcode"
    path.write_text("G1 X1 Y2 E0.1\n" * 20000)        # about 280 kB
    calls = []
    preflight_file(str(path), pause=lambda: calls.append(1))
    assert len(calls) >= path.stat().st_size // 4096
    # The exact pass yields too, for a file that needs it.
    monkeypatch.setattr(program, "PREFLIGHT_YIELD_LINES", 1000)
    path.write_text("START NAME=a\nG1\nEND\n" + "G1 X1\n" * 5000)
    calls.clear()
    preflight_file(str(path), pause=lambda: calls.append(1))
    assert len(calls) >= 5
