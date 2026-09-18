"""Run the installed macro source on real Klipper with inert hardware handlers."""
from pathlib import Path
from types import SimpleNamespace

import pytest
import configfile
from extras import gcode_macro
from test_review_regressions import execute


def _build_oams(env, has_gco, ordered=True):
    path = Path(__file__).parents[2] / 'config/oams_macros.cfg'
    # Exercise Klipper's real include/section merging used in printer.cfg.
    includes = '[include oams_macros.cfg]\n'
    if has_gco and ordered:
        includes += '[include oams_macros_ordered.cfg]\n'
    parsed = configfile.ConfigFileReader().build_fileconfig_with_includes(
        includes, str(path.parent / 'printer.cfg'))
    printer_config = configfile.PrinterConfig(env.printer)
    tracking = printer_config.validate.access_tracking
    for section in parsed.sections():
        cfg = configfile.ConfigWrapper(env.printer, parsed, tracking, section)
        env.printer.add_object(section, gcode_macro.GCodeMacro(cfg))
    printer_config.validate.check_unused(parsed)
    # check_unused clears tracking; restore consumed settings for the later
    # fixture configfile build, including _TX's configured render mode.
    for section, options in printer_config.validate.status_settings.items():
        tracking.update(((section, key), value) for key, value in options.items())
    if has_gco:
        parsed.add_section('gco_routines')
    for lane in range(4):
        section = 'filament_group T%d' % lane
        parsed.add_section(section)
        parsed.set(section, 'oams', 'oams1')
        configfile.ConfigWrapper(env.printer, parsed, tracking, section).get('oams')
    printer_config._build_status_config(
        configfile.ConfigWrapper(env.printer, parsed, tracking, 'printer'))
    printer_config.validate._build_status_settings()
    env.printer.add_object('configfile', printer_config)
    status = {
        'oams_manager': {'current_group': 'T0'},
        'toolhead': {'homed_axes': 'xyz'},
        'extruder': {'can_extrude': True, 'temperature': 220.0},
        'pause_resume': {'is_paused': False},
        'exclude_object': {'current_object': '', 'excluded_objects': []},
        'filament_switch_sensor extruder_in': {'filament_detected': True},
        'filament_switch_sensor extruder_out': {'filament_detected': False},
    }
    for name, values in status.items():
        env.printer.add_object(name, SimpleNamespace(get_status=lambda t, values=values: dict(values)))
    events = []
    for command in ('RESPOND', 'G90', 'M83', 'G0', 'G1', 'G4',
                    'SAVE_GCODE_STATE', 'RESTORE_GCODE_STATE', 'SET_STEPPER_ENABLE',
                    'OAMSM_FOLLOWER'):
        env.gcode.register_command(command, lambda g: events.append(g.get_commandline()))

    controls = {
        'unload_success': True,
        'load_success': True,
        'clean_yields': True,
        'm400_yields': True,
    }
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
        if controls['clean_yields']:
            env.reactor.pause(env.reactor.monotonic() + .02)
        events.append('clean-end')
    def m400(g):
        events.append('m400-start')
        if controls['m400_yields']:
            env.reactor.pause(env.reactor.monotonic() + .02)
        events.append('m400-end')
    def pause(g):
        events.append('pause')
        status['pause_resume']['is_paused'] = True
        if env.manager is not None:
            env.manager.set_paused(True)
    env.gcode.register_command('OAMSM_UNLOAD_FILAMENT', unload)
    env.gcode.register_command('OAMSM_LOAD_FILAMENT', load)
    env.gcode.register_command('CLEAN_NOZZLE', clean)
    env.gcode.register_command('M400', m400)
    env.gcode.register_command('PAUSE', pause)
    return SimpleNamespace(env=env, events=events, status=status, controls=controls,
        variables=env.printer.lookup_object('gcode_macro _oams_macro_variables').variables)


@pytest.fixture
def oams(klippy_env):
    return _build_oams(klippy_env, has_gco=True)


@pytest.fixture
def stock_oams(stock_klippy_env):
    return _build_oams(stock_klippy_env, has_gco=False)


def test_repeated_single_fps_changes_and_load_clean_overlap(oams):
    assert not execute(oams.env, 'T1\nT2')
    assert oams.events.count('unload-start') == 2
    assert oams.events.count('load-start') == 2
    assert oams.status['oams_manager']['current_group'] == 'T2'
    assert oams.events.index('unload-end') < oams.events.index('load-start')
    assert oams.events.index('clean-start') < oams.events.index('load-end') < oams.events.index('clean-end')
    assert oams.events.count('G1 E48.0 F1000') == 2


def test_empty_plugin_section_still_selects_concurrent_workflow(oams):
    # Klipper records only read options in settings. The optionless plugin is
    # present in config and the object registry, but absent from settings.
    config_status = oams.env.printer.lookup_object('configfile').get_status(0)
    assert config_status['config']['gco_routines'] == {}
    assert 'gco_routines' not in config_status['settings']
    oams.status['oams_manager']['current_group'] = None
    assert not execute(oams.env, 'T0')
    assert len(oams.env.manager.get_status()['routines']) == 2
    assert oams.events.index('clean-start') < oams.events.index('load-end')


def test_load_overlaps_non_yielding_clean_motion_at_barrier(oams):
    # A real gcode_macro queues CLEAN_NOZZLE moves without yielding Klippy's
    # reactor. M400 yields while the queued cleaning motion executes, so the
    # child can run before post-load checks continue.
    oams.controls['clean_yields'] = False
    assert not execute(oams.env, 'T1')
    clean_start = oams.events.index('clean-start')
    clean_end = oams.events.index('clean-end')
    first_m400_start = oams.events.index('m400-start', clean_end)
    first_m400_end = oams.events.index('m400-end', first_m400_start)
    load_start = oams.events.index('load-start')
    load_end = oams.events.index('load-end')
    assert clean_start < clean_end < first_m400_start < load_start
    assert load_start < load_end < first_m400_end


def test_stock_upstream_uses_serial_fallback_without_reserved_commands(stock_oams):
    handlers = stock_oams.env.gcode.ready_gcode_handlers
    assert all(command not in handlers for command in ('START', 'END', 'WAIT'))
    assert not execute(stock_oams.env, 'T1')
    assert stock_oams.status['oams_manager']['current_group'] == 'T1'
    assert stock_oams.events.index('load-start') < stock_oams.events.index('load-end')
    assert stock_oams.events.index('load-end') < stock_oams.events.index('clean-start')
    assert stock_oams.events.index('clean-start') < stock_oams.events.index('clean-end')
    assert 'G1 E48.0 F1000' in stock_oams.events


def test_plugin_without_macro_opt_in_uses_serial_fallback(klippy_env):
    oams = _build_oams(klippy_env, has_gco=True, ordered=False)
    assert oams.env.printer.lookup_object('gcode_macro _TX').template.gco_runner is None
    assert not execute(oams.env, 'T1')
    assert oams.status['oams_manager']['current_group'] == 'T1'
    assert oams.events.index('load-end') < oams.events.index('clean-start')


def test_stock_upstream_rechecks_load_result_before_extruding(stock_oams):
    stock_oams.controls['load_success'] = False
    assert execute(stock_oams.env, 'T1')
    assert stock_oams.status['pause_resume']['is_paused']
    assert 'G1 E48.0 F1000' not in stock_oams.events


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
