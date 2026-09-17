from pathlib import Path
import json
import pytest
from gcoroutines.frontend import parse, lint_closed_world, guard_rendered_commands
from gcoroutines.__main__ import main

@pytest.mark.parametrize('source', [
    '', '; comment', 'G1 X10 F1200\nM400', 'START\nT0\nEND\nWAIT',
    'START NAME=filament_change\nT0\nEND\nWAIT ON=filament_change',
    'start name=a\nT0\nend\nwait on=a',
    '\tSTART NAME=A_1 ; note\n T0\n END\nWAIT ON=A_1',
    'START\r\nM109 S220\r\nEND\r\nWAIT',
    'START\rT0\rEND\rWAIT', 'START\nEND\nWAIT',
    'START_PRINT\nEND_PRINT\nWAITING\nM117 START NAME=not_a_block',
    'N42 G1 X20*17', '(legacy comment)\nG1X10Y20',
    'START NAME=a\nEND\nSTART NAME=b\nWAIT ON=a\nEND\nWAIT ON=b,a',
])
def test_valid_programs(source):
    assert parse(source).ok

@pytest.mark.parametrize('source,code', [
    ('START NAME=\nEND', 'E_START_SYNTAX'),
    ('START NAME=1bad\nEND', 'E_START_SYNTAX'),
    ('START NAME=a-b\nEND', 'E_START_SYNTAX'),
    ('START NAME="a"\nEND', 'E_START_SYNTAX'),
    ('START NAME=a MORE=1\nEND', 'E_START_SYNTAX'),
    ('START NAME =a\nEND', 'E_START_SYNTAX'),
    ('START NAME={params.name}\nEND', 'E_START_SYNTAX'),
    ('WAIT ON=', 'E_WAIT_SYNTAX'), ('WAIT ON=[a,b]', 'E_WAIT_SYNTAX'),
    ('WAIT ON=a, b', 'E_WAIT_SYNTAX'), ('WAIT ON=a,', 'E_WAIT_SYNTAX'),
    ('WAIT ON=a,,b', 'E_WAIT_SYNTAX'), ('WAIT a', 'E_WAIT_SYNTAX'),
    ('WAIT ON=a TIMEOUT=5', 'E_WAIT_SYNTAX'),
    ('END NAME=a', 'E_END_SYNTAX'), ('END', 'E_UNMATCHED_END'),
    ('START\nT0', 'E_UNCLOSED_START'),
    ('START\nSTART\nEND\nEND', 'E_NESTED_START'),
    ('START NAME=default\nEND', 'E_RESERVED_NAME'),
    ('WAIT ON=a,a', 'E_DUPLICATE_TARGET'),
    ('WAIT ON=default', 'E_RESERVED_NAME'),
    ('START NAME=a\nWAIT ON=a\nEND', 'E_SELF_WAIT'),
    ('N1 START\nEND', 'E_TRANSPORT'),
    ('G1\x00X10', 'E_NUL'),
])
def test_invalid_programs(source, code):
    r = parse(source)
    assert not r.ok
    assert code in [d.code for d in r.diagnostics]

def test_preserves_source_and_case():
    r = parse('  START NAME=MixedCase\n G1 X12  ; keep this\n  END\nWAIT ON=MixedCase')
    assert r.nodes[0].name == 'MixedCase'
    assert r.nodes[0].line == 1 and r.nodes[0].end_line == 3
    assert r.nodes[0].column == 3
    assert r.nodes[0].body[0].text == ' G1 X12  ; keep this'
    assert r.nodes[1].targets == ['MixedCase']

def test_explicit_closed_world_assumption():
    text='SOME_MACRO\nWAIT ON=created_elsewhere'
    assert parse(text).ok
    assert not lint_closed_world(parse(text)).ok

def test_source_order_not_finish_order():
    assert lint_closed_world(parse('START NAME=a\nEND\nSTART NAME=b\nWAIT ON=a\nEND\nWAIT ON=b')).ok
    r=lint_closed_world(parse('START NAME=b\nWAIT ON=a\nEND\nSTART NAME=a\nEND'))
    assert r.ok
    assert any(d.code == 'W_SCHEDULE_DEPENDENT' for d in r.diagnostics)

def test_name_reuse_after_collection():
    assert lint_closed_world(parse('START NAME=a\nEND\nWAIT ON=a\nSTART NAME=a\nEND')).ok
    assert not lint_closed_world(parse('START NAME=a\nEND\nSTART NAME=a\nEND')).ok

def test_limits():
    assert not parse('abc', max_bytes=2).ok
    assert not parse('a\nb', max_lines=1).ok

@pytest.mark.parametrize('text', ['WAIT','START NAME=a','END','M117 fine\nwait on=a','N4 START*33'])
def test_expansion_guard_rejects_manufactured_controls(text):
    assert guard_rendered_commands(text)

@pytest.mark.parametrize('text', ['M117 WAIT ON=a','START_PRINT','; START','G1 X{params.x}','G1 X10\nM400'])
def test_expansion_guard_preserves_ordinary(text):
    assert not guard_rendered_commands(text)

def test_cli(tmp_path, capsys):
    p=tmp_path/'a.gcode';p.write_text('START\nT0\nEND\nWAIT')
    assert main([str(p),'--json']) == 0
    assert json.loads(capsys.readouterr().out)['ok']
    p.write_text('WAIT ON=[a]')
    assert main([str(p)]) == 1
