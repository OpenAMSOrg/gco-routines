#!/usr/bin/env python3
"""Exercise the extra and actual OAMS config with real Klippy and inert devices.

No printer configuration, MCU, socket, motion, heater, or service is opened.
Run with Klipper's own Python environment; pytest is not required.
"""
import argparse
import os
from pathlib import Path
import sys
from types import SimpleNamespace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--klipper', type=Path, required=True)
    parser.add_argument('--extra-parent', type=Path, required=True)
    parser.add_argument('--macros', type=Path, required=True)
    parser.add_argument('--ordered-config', type=Path,
                        help='Opt-in overlay (default: oams_macros_ordered.cfg beside --macros)')
    args = parser.parse_args()
    sys.path[:0] = [str(args.extra_parent.resolve()),
                    str(args.klipper.resolve() / 'klippy')]
    import reactor
    import klippy
    import configfile
    import gco_routines
    from extras import gcode_macro

    read_fd, write_fd = os.pipe()
    loop = reactor.SelectReactor()
    printer = klippy.Printer(loop, None, {'gcode_fd': write_fd, 'debuginput': True})
    for timer in list(loop._timers):
        callback = getattr(timer, 'underlying_callback', None) or getattr(timer, 'callback', None)
        if callback == printer._connect:
            loop.unregister_timer(timer)
    printer.add_object('gcode_macro', gcode_macro.PrinterGCodeMacro(
        SimpleNamespace(get_printer=lambda: printer)))
    manager = gco_routines.load_config(SimpleNamespace(
        get_printer=lambda: printer, error=RuntimeError))
    gcode = printer.lookup_object('gcode')
    gcode.output_callbacks.clear()
    gcode.is_fileinput = False
    printer.lookup_object('gcode_io').is_fileinput = False
    printer.send_event('klippy:ready')
    reader = configfile.ConfigFileReader()
    data = reader.build_fileconfig(
        args.macros.read_text(), str(args.macros))
    overlay = args.ordered_config or args.macros.with_name('oams_macros_ordered.cfg')
    reader.append_fileconfig(data, overlay.read_text(), str(overlay))
    validation = configfile.ConfigValidate(printer)
    for section in data.sections():
        cfg = configfile.ConfigWrapper(printer, data, validation.access_tracking, section)
        printer.add_object(section, gcode_macro.GCodeMacro(cfg))
    validation.check_unused(data)
    settings = dict(validation.status_settings)
    settings.update({'filament_group t%d' % i: {} for i in range(4)})
    statuses = {
        'oams_manager': {'current_group': 'T0'},
        'toolhead': {'homed_axes': 'xyz'},
        'extruder': {'can_extrude': True, 'temperature': 220.0},
        'pause_resume': {'is_paused': False},
        'exclude_object': {'current_object': '', 'excluded_objects': []},
        'configfile': {'settings': settings},
        'filament_switch_sensor extruder_in': {'filament_detected': True},
        'filament_switch_sensor extruder_out': {'filament_detected': False},
    }
    for name, status in statuses.items():
        printer.add_object(name, SimpleNamespace(get_status=lambda t, status=status: dict(status)))
    events = []
    for command in ('RESPOND', 'G90', 'M83', 'G0', 'G1', 'M400', 'G4',
                    'SAVE_GCODE_STATE', 'RESTORE_GCODE_STATE', 'SET_STEPPER_ENABLE',
                    'OAMSM_FOLLOWER'):
        gcode.register_command(command, lambda g: events.append(g.get_commandline()))
    def load(g):
        events.append('load-start')
        loop.pause(loop.monotonic() + .01)
        statuses['oams_manager']['current_group'] = g.get('GROUP')
        events.append('load-end')
    def unload(g):
        statuses['oams_manager']['current_group'] = None
        events.append('unloaded')
    def clean(g):
        events.append('clean-start')
        loop.pause(loop.monotonic() + .02)
        events.append('clean-end')
    def pause(g):
        statuses['pause_resume']['is_paused'] = True
        manager.set_paused(True)
    gcode.register_command('OAMSM_LOAD_FILAMENT', load)
    gcode.register_command('OAMSM_UNLOAD_FILAMENT', unload)
    gcode.register_command('CLEAN_NOZZLE', clean)
    gcode.register_command('PAUSE', pause)
    errors = []
    def run(t):
        try:
            gcode.run_script('T1\nT2')
            assert statuses['oams_manager']['current_group'] == 'T2'
            assert events.count('unloaded') == 2
            assert events.count('G1 E48.0 F1000') == 2
            assert events.index('clean-start') < events.index('load-end') < events.index('clean-end')
            events.clear()
            variables = printer.lookup_object('gcode_macro _oams_macro_variables').variables
            variables['fs_extruder_out'] = True
            statuses['filament_switch_sensor extruder_out']['filament_detected'] = True
            try:
                gcode.run_script('T0\nT3')
            except gcode.error as exc:
                assert 'sensor check' in str(exc), str(exc)
            else:
                raise AssertionError('Expected failed sensor to abort the script')
            assert statuses['pause_resume']['is_paused']
            assert 'load-start' not in events and 'unloaded' not in events
            assert not any('E-150' in event for event in events)
        except Exception as exc:
            errors.append(exc)
        finally:
            loop.end()
    loop.register_callback(run)
    try:
        loop.run()
    finally:
        loop.finalize()
        os.close(read_fd)
        os.close(write_fd)
    if errors:
        raise errors[0]
    print('PASS: real Klippy, real OAMS config, inert devices; repeated changes, overlap, sensor abort')
    print('Python ' + sys.version.split()[0])


if __name__ == '__main__':
    main()
