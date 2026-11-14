"""Raw-source parsing and per-source START block collection.

This module only recognizes the three literal controls.  It never renders a
template, invokes a command, or dispatches a collected body.  The integration
layer decides when a completed :class:`Block` is admitted to a :class:`Run`.
"""

from dataclasses import dataclass, field
import os
import sys
from typing import Callable, Dict, Iterable, List, Optional, Tuple

import re

_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_HEAD = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\b", re.ASCII)
_TRANSPORT = re.compile(r"^N\d+\s*(?:START|END|WAIT)\b", re.IGNORECASE | re.ASCII)


class ProgramError(ValueError):
    """Malformed control source or exhausted collector resource limit."""


@dataclass(frozen=True)
class Control:
    kind: str
    line: int
    text: str
    name: Optional[str] = None
    targets: Optional[Tuple[str, ...]] = None


@dataclass
class Block:
    name: Optional[str]
    body: List[str]
    start_line: int
    end_line: int
    source_id: str = "default"

    def __iter__(self):
        # Compatibility with the original adapter's ``name, body = payload``.
        yield self.name
        yield list(self.body)


@dataclass
class Program:
    """A structural source result; ordinary body lines remain opaque strings."""
    source_id: str
    nodes: List[object] = field(default_factory=list)


def _control(line: str, line_num: int):
    # Keep ordinary G-code opaque.  Only a reserved head is interpreted.
    value = line.split(";", 1)[0].strip(" \t")
    if _TRANSPORT.match(value):
        raise ProgramError("E_TRANSPORT: Decode and validate transport framing before parsing control commands. at line %d" % line_num)
    match = _HEAD.match(value)
    head = match.group(1).upper() if match else None
    if head not in ("START", "END", "WAIT"):
        return None
    if head == "START":
        match = re.fullmatch(r"START(?:[ \t]+NAME=(%s))?" % _IDENT,
                             value, re.IGNORECASE | re.ASCII)
        if not match:
            raise ProgramError("E_START_SYNTAX: Expected START or START NAME=identifier; names must be literal. at line %d" % line_num)
        name = match.group(1)
        if name == "default":
            raise ProgramError("E_RESERVED_NAME: 'default' is reserved. at line %d" % line_num)
        return Control("start", line_num, line, name, None)
    if head == "END":
        if value.upper() != "END":
            raise ProgramError("E_END_SYNTAX: END takes no arguments. at line %d" % line_num)
        return Control("end", line_num, line)
    match = re.fullmatch(r"WAIT(?:[ \t]+ON=(%s(?:,%s)*))?" % (_IDENT, _IDENT),
                         value, re.IGNORECASE | re.ASCII)
    if not match:
        raise ProgramError("E_WAIT_SYNTAX: Expected WAIT or WAIT ON=a,b (no brackets, spaces or empty names). at line %d" % line_num)
    targets = tuple(match.group(1).split(",")) if match.group(1) else None
    if targets and "default" in targets:
        raise ProgramError("E_RESERVED_NAME: 'default' is reserved. at line %d" % line_num)
    return Control("wait", line_num, line, targets=targets)


class BlockCollector:
    """Collect one source's flat START/END blocks across line submissions.

    ``feed_line`` returns the historical adapter tuples: ``COLLECTING``,
    ``SPAWN``, ``WAIT`` or ``PASSTHROUGH``.  The ``SPAWN`` payload is a
    historical ``(name, body_lines)`` tuple.  The completed :class:`Block` is
    available as ``last_block`` for callers that need source positions.  No
    body command is run here.
    """
    def __init__(self, source_id: str = "default", max_lines: int = 100000,
                 max_bytes: int = 2000000):
        self.source_id = source_id
        self.max_lines = max_lines
        self.max_bytes = max_bytes
        self.collecting = False
        self.start_line = 0
        self.end_line = 0
        self.routine_name = None
        self.buffer: List[str] = []
        self._bytes = 0
        self.last_block: Optional[Block] = None

    def feed_line(self, line: str, line_num: int = 1):
        if not isinstance(line, str):
            raise ProgramError("Source line must be text")
        if line_num < 1:
            raise ProgramError("Source line numbers start at one")
        node = _control(line, line_num)
        if not self.collecting:
            if node is not None and node.kind == "start":
                self.collecting = True
                self.start_line = line_num
                self.routine_name = node.name
                self.buffer = []
                self._bytes = 0
                return ("COLLECTING", None)
            if node is not None and node.kind == "end":
                raise ProgramError("E_UNMATCHED_END: END has no matching START in this source.")
            if node is not None and node.kind == "wait":
                return ("WAIT", list(node.targets) if node.targets is not None else None)
            return ("PASSTHROUGH", line)

        if node is not None and node.kind == "start":
            raise ProgramError("E_NESTED_START: Nested START blocks are not supported.")
        if node is not None and node.kind == "end":
            body = list(self.buffer)
            block = Block(self.routine_name, body, self.start_line, line_num,
                          self.source_id)
            self.last_block = block
            self.collecting = False
            self.start_line = 0
            self.end_line = line_num
            self.routine_name = None
            self.buffer = []
            self._bytes = 0
            return ("SPAWN", (block.name, list(block.body)))
        if len(self.buffer) >= self.max_lines:
            raise ProgramError("E_SOURCE_LIMIT: block exceeds line limit")
        self._bytes += len(line.encode("utf-8")) + 1
        if self._bytes > self.max_bytes:
            raise ProgramError("E_SOURCE_LIMIT: block exceeds byte limit")
        self.buffer.append(line)
        return ("COLLECTING", None)

    def assert_closed(self):
        if self.collecting:
            raise ProgramError("E_UNCLOSED_START: START at line %d reaches end of source without END." % self.start_line)


class SourceCollectors:
    """Keep independent collectors so unrelated ingress sources cannot join."""
    def __init__(self, max_sources: int = 64, **collector_limits):
        self.max_sources = max_sources
        self.collector_limits = collector_limits
        self._collectors: Dict[str, BlockCollector] = {}

    def collector(self, source_id: str) -> BlockCollector:
        if not isinstance(source_id, str) or not source_id:
            raise ProgramError("source_id must be non-empty")
        if source_id not in self._collectors:
            if len(self._collectors) >= self.max_sources:
                raise ProgramError("E_SOURCE_LIMIT: too many active sources")
            self._collectors[source_id] = BlockCollector(source_id, **self.collector_limits)
        return self._collectors[source_id]

    def feed_line(self, source_id: str, line: str, line_num: int = 1):
        return self.collector(source_id).feed_line(line, line_num)

    def assert_closed(self, source_id: str):
        self.collector(source_id).assert_closed()

    def close(self, source_id: str):
        self.assert_closed(source_id)
        self._collectors.pop(source_id, None)


def parse_program(source: str, source_id: str = "default") -> Program:
    """Parse complete raw G-code structure without executing its body."""
    if not isinstance(source, str):
        raise ProgramError("Source must be text")
    collector = BlockCollector(source_id)
    program = Program(source_id)
    lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if source.endswith(("\n", "\r")):
        lines.pop()
    for line_num, line in enumerate(lines, 1):
        kind, value = collector.feed_line(line, line_num)
        if kind == "SPAWN":
            program.nodes.append(collector.last_block or value)
        elif kind == "WAIT":
            program.nodes.append(Control("wait", line_num, line,
                                         targets=tuple(value) if value is not None else None))
        elif kind == "PASSTHROUGH":
            program.nodes.append(line)
    collector.assert_closed()
    return program


# Friendly aliases used by integrations/tests.
parse_source = parse_program


# A physical line this long cannot be a G-code command (Klipper itself rejects
# commands over 1 MiB).  The limit only bounds preflight memory when a file has
# no newlines; it is not a whole-file size limit.
MAX_PREFLIGHT_LINE_CHARS = 16 * 1024 * 1024
_CONTROL_INITIALS = frozenset("SsEeWwNn")


def _may_be_control(line: str) -> bool:
    """Cheap necessary condition for ``_control`` to recognize a line.

    START/END/WAIT (and transport-framed ``N<digits>`` controls) begin with one
    of these letters after spaces/tabs; any other line is ordinary G-code.
    """
    stripped = line.lstrip(" \t")
    return bool(stripped) and stripped[0] in _CONTROL_INITIALS


# How much of the file one preflight step reads before it lets the reactor
# run (Klipper's timers, the host keepalives of other MCUs): small enough that
# no step blocks for long on a slow host, large enough to stay cheap.
PREFLIGHT_CHUNK_BYTES = 1 << 20
# The exact (line by line) pass yields this often.
PREFLIGHT_YIELD_LINES = 20000

# A line the exact pass could treat as a control (_control: START, END or
# WAIT as the head after spaces and tabs, or transport framing N<digits>
# before one), matched on lowercased bytes at a line start. A superset is
# safe: a match only sends the file through the exact pass.
_CANDIDATE = re.compile(
    rb"[ \t]*(?:n[0-9]+[ \t\x0b\x0c]*)?(?:start|end|wait)(?![a-z0-9_])")
# Where such a line can begin: a line end followed by a keyword, a space or
# tab, or transport framing. One regex pass with a literal first character
# finds them at C speed; each hit is then checked against _CANDIDATE.
_LINE_START = re.compile(rb"\n(?:[ \t]|n[0-9]|start|end|wait)")


def _chunk_has_candidate(piece: bytes) -> bool:
    """Whether whole lines ``piece`` (ending at a line end) hold a line the
    exact pass could treat as a control."""
    low = piece.lower()
    if b"\r" in low:            # universal newlines, as the exact pass reads
        low = low.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    low = b"\n" + low
    for hit in _LINE_START.finditer(low):
        if _CANDIDATE.match(low, hit.start() + 1):
            return True
    return False


def _may_hold_control(filepath: str, max_line_chars: int,
                      pause: Optional[Callable[[], None]]) -> bool:
    """A fast pass over the raw bytes: False only when the exact pass could
    find no control line and no overlong line, so it would accept the file
    without collecting anything. Searching in C instead of a Python loop per
    line makes a 2.5-million-line slicer file a fraction of a second instead
    of seconds. Line lengths are measured in bytes, never fewer than the
    exact pass's characters, so a doubtful line goes to the exact pass."""
    # A line wholly inside one read can exceed only a limit below the read.
    long_line = (re.compile(rb"[^\r\n]{%d}" % (max_line_chars + 1))
                 if max_line_chars < PREFLIGHT_CHUNK_BYTES else None)
    carry = b""
    with open(filepath, "rb") as source_file:
        while True:
            data = source_file.read(PREFLIGHT_CHUNK_BYTES)
            if not data:
                break
            ends = [i for i in (data.find(b"\n"), data.find(b"\r")) if i != -1]
            if not ends:
                carry += data
                if len(carry) > max_line_chars:
                    return True
            else:
                if len(carry) + min(ends) > max_line_chars:
                    return True      # the line the last read left open
                cut = max(data.rfind(b"\n"), data.rfind(b"\r"))
                piece, carry = carry + data[:cut + 1], data[cut + 1:]
                if long_line is not None and long_line.search(piece):
                    return True
                if _chunk_has_candidate(piece) or len(carry) > max_line_chars:
                    return True
            if pause is not None:
                pause()
    return bool(carry) and _chunk_has_candidate(carry + b"\n")


def preflight_file(filepath: str,
                   max_line_chars: int = MAX_PREFLIGHT_LINE_CHARS,
                   pause: Optional[Callable[[], None]] = None) -> None:
    """Validate a complete file's control structure before admission.

    A fast pass over the raw bytes settles the common case, a sliced file
    with no START, END or WAIT line, without parsing a line. Otherwise the
    file is streamed line by line through a :class:`BlockCollector`.
    Ordinary lines outside a block are neither retained nor fully parsed, so
    memory is bounded by the collector's per-block limits and one line,
    whatever the file size.  Line splitting matches :func:`parse_program`
    (universal newlines).  Nothing is rendered or dispatched.

    ``pause``, when given, is called between steps so a caller on Klipper's
    reactor can let it run: preflight then never holds the reactor for long,
    whatever the file size.
    """
    if not os.path.exists(filepath):
        raise ProgramError("Source file does not exist: %s" % filepath)
    if not _may_hold_control(filepath, max_line_chars, pause):
        return
    collector = BlockCollector(source_id=filepath)
    with open(filepath, "r", encoding="utf-8", errors="replace") as source_file:
        line_num = 0
        while True:
            line = source_file.readline(max_line_chars + 1)
            if not line:
                break
            line_num += 1
            if line.endswith("\n"):
                line = line[:-1]
            elif len(line) > max_line_chars:
                raise ProgramError(
                    "E_SOURCE_LIMIT: line %d exceeds %d characters"
                    % (line_num, max_line_chars))
            if collector.collecting or _may_be_control(line):
                collector.feed_line(line, line_num)
            if pause is not None and line_num % PREFLIGHT_YIELD_LINES == 0:
                pause()
    collector.assert_closed()
