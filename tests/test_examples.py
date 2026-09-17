from pathlib import Path
import json
import subprocess
import sys
import pytest
from gcoroutines.frontend import parse, lint_closed_world

ROOT=Path(__file__).parents[1]

@pytest.mark.parametrize('name', ['toolchange.gcode','anonymous.gcode','dependencies.gcode',
                                    'results.jinja','anonymous-results.jinja','conditional.jinja','reuse.jinja'])
def test_shipped_examples(name):
    p=ROOT/'examples'/name
    r=parse(p.read_text(),str(p),template=p.suffix=='.jinja')
    assert r.ok, r.to_dict()
    if p.suffix=='.gcode':assert lint_closed_world(r).ok

@pytest.mark.parametrize('name', ['malformed-wait.gcode','cross-branch.jinja',
                                    'dynamic-name.jinja','nested.gcode','self-wait.gcode'])
def test_shipped_rejection_examples(name):
    p=ROOT/'examples/invalid'/name
    assert not parse(p.read_text(),str(p),template=p.suffix=='.jinja').ok

def test_unknown_example_requires_semantic_phase():
    p=ROOT/'examples/invalid/unknown-name.gcode'
    r=parse(p.read_text());assert r.ok
    assert not lint_closed_world(r).ok

def test_demo_produces_revisioned_current_snapshots():
    output=subprocess.check_output([sys.executable,str(ROOT/'tools/demo_state.py')],text=True)
    events=[json.loads(line) for line in output.splitlines()]
    assert len(events)==3
    snapshots=[e['snapshot'] for e in events]
    assert [s['revision'] for s in snapshots]==sorted({s['revision'] for s in snapshots})
    assert snapshots[0]['routines'][0]['state']=='waiting'
    assert snapshots[-1]['routines'][0]['state']=='running'
    assert snapshots[-1]['routines'][1]['result']['lane']==0
    schema=json.loads((ROOT/'schemas/status.schema.json').read_text())
    for snap in snapshots:
        assert set(schema['required']) <= set(snap)
        for routine in snap['routines']:
            rule=schema['properties']['routines']['items']
            assert set(rule['required']) <= set(routine)
            assert routine['state'] in rule['properties']['state']['enum']
