#!/usr/bin/env python3
"""Print synthetic state transitions. No G-code, Jinja or hardware is executed."""
from pathlib import Path
import sys
import json
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gcoroutines.semantics import Registry

r = Registry('demo-run')
filament = r.start('filament_change')
heat = r.start('nozzle_heating')
r.observe_command(filament, 'T0', {'device': 'mmu', 'phase': 'seeking_handoff'},
                  {'source': 'examples/toolchange.gcode', 'line': 5})
r.observe_command(heat, 'M109 S220', {'device': 'heater', 'reason': 'temperature'},
                  {'source': 'examples/toolchange.gcode', 'line': 9})
r.observe_command(r.default, 'WAIT ON=filament_change,nozzle_heating',
                  source={'source': 'examples/toolchange.gcode', 'line': 13})
r.wait(r.default, ['filament_change', 'nozzle_heating'])
print(json.dumps({'event': 'synthetic-wait', 'snapshot': r.snapshot()}))
r.finish(heat)
print(json.dumps({'event': 'synthetic-heater-complete', 'snapshot': r.snapshot()}))
r.finish(filament, {'lane': 0, 'loaded_mm': 684.5})
print(json.dumps({'event': 'synthetic-resumed', 'snapshot': r.snapshot()}))
