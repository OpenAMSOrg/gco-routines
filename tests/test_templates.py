import pytest
from gcoroutines.frontend import parse, lint_closed_world

@pytest.mark.parametrize('source', [
    'START\n T0\n {% set result.lane = reply.lane %}\nEND\nWAIT\nM117 {waited[0].lane}',
    '{% if params.enabled %}\nSTART NAME=a\nT0\nEND\nWAIT ON=a\n{% endif %}',
    '{% for tool in [0,1] %}\nSTART NAME=a\nT{tool}\nEND\nWAIT ON=a\n{% endfor %}',
    'START NAME=a\n{% if params.x %}\nT0\n{% else %}\nT1\n{% endif %}\nEND',
    '{# START\nEND\n #}\nG1 X10',
    'M117 {"START\\nEND"}',
    'START\n{% set result.values = {"a": 1, "b": [2,3]} %}\nEND\nWAIT',
    'START\nT0\nEND\nWAIT\n{% if waited[0].optional|default(false) %}\nM117 Yes\n{% endif %}',
])
def test_parse_jinja_without_rendering(source):
    assert parse(source, template=True).ok

@pytest.mark.parametrize('source,code', [
    ('START NAME={params.name}\nEND', 'E_LITERAL_CONTROL'),
    ('WAIT ON={params.names}', 'E_LITERAL_CONTROL'),
    ('{% if true %}START\nEND\n{% endif %}', 'E_LITERAL_CONTROL'),
    ('START\n{% if x %}\nEND\n{% endif %}', 'E_TEMPLATE_REGION'),
    ('{% if x %}\nSTART\n{% else %}\nEND\n{% endif %}', 'E_TEMPLATE_REGION'),
    ('{% for t in tools %}\nSTART\n{% endfor %}\nEND', 'E_TEMPLATE_REGION'),
    ('{% set generated %}\nSTART\nEND\n{% endset %}', 'E_CAPTURED_CONTROL'),
    ('{% macro generated() %}\nSTART\nEND\n{% endmacro %}', 'E_CAPTURED_CONTROL'),
    ('START\nEND\n{% include "other" %}', 'E_TEMPLATE_COMPOSITION'),
    ('START\nEND\n{% if x %}', 'E_JINJA_SYNTAX'),
    ('START\nEND\n{% set result.x = %}', 'E_JINJA_SYNTAX'),
])
def test_invalid_template_structure(source, code):
    r=parse(source, template=True)
    assert not r.ok
    assert code in [d.code for d in r.diagnostics]

def test_no_execution_of_helper_call():
    # No globals or printer are supplied. Parsing a call cannot invoke it.
    source='START\nT0\n{% set result.x = action_emergency_stop() %}\nEND\nWAIT'
    assert parse(source, template=True).ok

def test_comments_are_not_blocks():
    r=parse('{#\nSTART\nT0\nEND\n#}\nM117 OK', template=True)
    assert r.ok and not any(n.kind == 'routine' for n in r.nodes)

def test_case_sensitive_names_survive_template_parser():
    r=parse('START NAME=Case\nEND\nWAIT ON=Case', template=True)
    assert r.ok and r.nodes[0].name == 'Case'

def test_template_analysis_is_not_claimed_closed_world():
    r=lint_closed_world(parse('{% if ok %}\nSTART NAME=a\nEND\n{% endif %}\nWAIT ON=a', template=True))
    assert r.ok
    assert any(d.code=='W_DYNAMIC_ANALYSIS' for d in r.diagnostics)

def test_scopes_are_not_flattened_as_executable_ir():
    r=parse('{% if x %}\nSTART NAME=a\nEND\n{% endif %}', template=True)
    assert r.template_control_flow
    assert any(n.kind == 'template_source' for n in r.nodes)
