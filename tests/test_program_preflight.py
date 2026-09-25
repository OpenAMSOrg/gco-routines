"""Streaming virtual-SD preflight: no whole-file cap, same structural errors."""
import pytest

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


@pytest.mark.parametrize("source", [
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
])
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
