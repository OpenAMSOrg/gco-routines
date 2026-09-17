"""Run the installed macro source on real Klipper with inert hardware handlers."""
from pathlib import Path
from types import SimpleNamespace

import pytest
import configfile
from extras import gcode_macro
from test_review_regressions import execute


@pytest.fixture
def oams(klippy_env):
    env = klippy_env
    path = Path(__file__).parents[2] / 'config/oams_macros.cfg'
    parsed = configfile.ConfigFileReader().build_fileconfig(path.read_text(), str(path))
    for section in parsed.sections():
        cfg = configfile.ConfigWrapper(env.printer, parsed, {}, section)
        env.printer.add_object(section, gcode_macro.GCodeMacro(cfg))
    status = {
        'oams_manager': {'current_group': 'T0'},
        'toolhead': {'homed_axes': 'xyz'},
        'extruder': {'can_extrude': True, 'temperature': 220.0},
        'pause_resume': {'is_paused': False},
        'exclude_object': {'current_object': '', 'excluded_objects': []},
        # Real ConfigFile.get_status() normalizes section keys to lowercase.
        'configfile': {'settings': {'filament_group t%d' % i: {} for i in range(4)}},
        'filament_switch_sensor extruder_in': {'filament_detected': True},
        'filament_switch_sensor extruder_out': {'filament_detected': False},
    }
    for name, values in status.items():
        env.printer.add_object(name, SimpleNamespace(get_status=lambda t, values=values: dict(values)))
    events = []
    for command in ('RESPOND', 'G90', 'M83', 'G0', 'G1', 'M400', 'G4',
                    'SAVE_GCODE_STATE', 'RESTORE_GCODE_STATE', 'SET_STEPPER_ENABLE',
                    'OAMSM_FOLLOWER'):
        env.gcode.register_command(command, lambda g: events.append(g.get_commandline()))

    controls = {'unload_success': True, 'load_success': True}
    def unload(g):
        events.append('unload-start')
        env.reactor.pause(env.reactor.monotonic() + .005)
        if controls['unload_success']:
            status['oams_manager']['current_group'] = None
        events.append('unload-end')
    def load(g):
        events.append('load-start')
        env.reactor.pause(env.reactor.monotonic() + .01)
        if controls['load_success']:
            status['oams_manager']['current_group'] = g.get('GROUP')
        events.append('load-end')
    def clean(g):
        events.append('clean-start')
        env.reactor.pause(env.reactor.monotonic() + .02)
        events.append('clean-end')
    def pause(g):
        events.append('pause')
        status['pause_resume']['is_paused'] = True
        env.manager.set_paused(True)
    env.gcode.register_command('OAMSM_UNLOAD_FILAMENT', unload)
    env.gcode.register_command('OAMSM_LOAD_FILAMENT', load)
    env.gcode.register_command('CLEAN_NOZZLE', clean)
    env.gcode.register_command('PAUSE', pause)
    return SimpleNamespace(env=env, events=events, status=status, controls=controls,
        variables=env.printer.lookup_object('gcode_macro _oams_macro_variables').variables)


def test_repeated_single_fps_changes_and_load_clean_overlap(oams):
    assert not execute(oams.env, 'T1\nT2')
    assert oams.events.count('unload-start') == 2
    assert oams.events.count('load-start') == 2
    assert oams.status['oams_manager']['current_group'] == 'T2'
    assert oams.events.index('unload-end') < oams.events.index('load-start')
    assert oams.events.index('clean-start') < oams.events.index('load-end') < oams.events.index('clean-end')
    assert oams.events.count('G1 E48.0 F1000') == 2


def test_standalone_safe_unload_finishes_transport(oams):
    assert not execute(oams.env, 'SAFE_UNLOAD_FILAMENT')
    assert oams.status['oams_manager']['current_group'] is None
    assert oams.events[-1] == 'RESTORE_GCODE_STATE NAME=oams_unload'


@pytest.mark.parametrize('failure', ['unload_sensor', 'unload_driver', 'load_driver', 'inlet_sensor', 'outlet_sensor'])
def test_failure_stops_toolchange_and_following_tool(failure, oams):
    if failure == 'unload_sensor':
        oams.variables['fs_extruder_out'] = True
        oams.status['filament_switch_sensor extruder_out']['filament_detected'] = True
    elif failure == 'unload_driver':
        oams.controls['unload_success'] = False
    elif failure == 'load_driver':
        oams.controls['load_success'] = False
    elif failure == 'inlet_sensor':
        oams.variables['fs_extruder_in'] = True
        oams.status['filament_switch_sensor extruder_in']['filament_detected'] = False
    else:
        oams.variables['fs_extruder_out'] = True
    errors = execute(oams.env, 'T1\nT2')
    assert errors
    assert oams.status['pause_resume']['is_paused']
    assert oams.events.count('load-start') <= 1
    if failure.startswith('unload'):
        assert 'load-start' not in oams.events
    if failure == 'unload_sensor':
        assert 'unload-start' not in oams.events
        assert not any('E-150' in event for event in oams.events)
    if failure != 'outlet_sensor':
        assert 'G1 E48.0 F1000' not in oams.events


@pytest.mark.parametrize('source', ['_TX GROUP=INVALID', 'START\nT1\nEND\nWAIT'])
def test_invalid_or_nested_toolchange_fails_before_hardware(oams, source):
    assert execute(oams.env, source)
    assert oams.events == []


def test_cold_toolchange_has_no_motion(oams):
    # This printer config sets min_extrude_temp to 10 C, so Klipper reports
    # can_extrude at room temperature.  The macro must retain its own guard.
    oams.status['extruder']['temperature'] = 23.0
    assert execute(oams.env, 'T1')
    assert oams.events == []


def test_cold_standalone_unload_has_no_motion(oams):
    oams.status['extruder']['temperature'] = 23.0
    assert execute(oams.env, 'SAFE_UNLOAD_FILAMENT')
    assert oams.events == []
