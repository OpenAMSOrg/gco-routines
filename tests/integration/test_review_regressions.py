"""Behavioral regressions found during the deployment review."""
from types import SimpleNamespace

import pytest

from extras import gcode_macro
from test_ordered_jinja import MacroConfig
from gco_routines.program import BlockCollector


def execute(env, source):
    # Model live API ingress, not Klipper's batch-debug exit-on-error mode.
    env.gcode.is_fileinput = False
    env.printer.lookup_object('gcode_io').is_fileinput = False
    errors = []
    def invoke(eventtime):
        try:
            env.gcode.run_script(source)
        except Exception as exc:
            errors.append(exc)
        finally:
            # Let already queued child callbacks drain before ending.
            env.reactor.pause(env.reactor.monotonic() + .03)
            env.reactor.end()
    env.reactor.register_callback(invoke)
    env.reactor.run()
    return errors


def macro(env, name, source):
    obj = gcode_macro.GCodeMacro(MacroConfig(env.printer, name, source))
    env.printer.add_object('gcode_macro ' + name, obj)
    return obj


def test_actual_macro_status_refreshes_after_command(klippy_env):
    env = klippy_env
    state = {'value': 0}
    env.printer.add_object('sensor', SimpleNamespace(get_status=lambda t: dict(state)))
    env.gcode.register_command('CHANGE', lambda g: state.update(value=1))
    seen = []
    env.gcode.register_command('SHOW', lambda g: seen.append(g.get_int('VALUE')))
    macro(env, 'LIVE', 'WAIT\nSHOW VALUE={printer.sensor.value}\nCHANGE\n'
          'SHOW VALUE={printer.sensor.value}')
    assert not execute(env, 'LIVE')
    assert seen == [0, 1]


def test_cancel_stops_remaining_legacy_macro_commands(klippy_env):
    env = klippy_env
    seen = []
    env.gcode.register_command('STOPRUN', lambda g: env.manager.cancel_active_runs('test cancel'))
    env.gcode.register_command('AFTER', lambda g: seen.append('unsafe'))
    macro(env, 'LEGACY', 'STOPRUN\nAFTER')
    assert execute(env, 'START\nLEGACY\nEND\nWAIT')
    assert seen == []


def test_child_failure_stops_parent_before_next_ordinary_line(klippy_env):
    env = klippy_env
    seen = []
    def fail(g):
        raise g.error('device failure')
    env.gcode.register_command('FAIL', fail)
    env.gcode.register_command('YIELD', lambda g: env.reactor.pause(env.reactor.monotonic() + .01))
    env.gcode.register_command('AFTER', lambda g: seen.append('unsafe'))
    assert execute(env, 'START\nFAIL\nEND\nYIELD\nAFTER\nWAIT')
    assert seen == []


def test_complete_api_is_preflighted_before_any_command(klippy_env):
    env = klippy_env
    seen = []
    env.gcode.register_command('MOVE', lambda g: seen.append('unsafe'))
    assert execute(env, 'MOVE\nSTART\nMOVE')
    assert seen == []


def test_cancelled_queued_template_never_calls_actions(klippy_env):
    env = klippy_env
    seen = []
    env.manager.macro_manager.env.globals['record'] = lambda: seen.append('unsafe') or ''
    macro(env, 'QUEUED', 'START\n{record()}\nEND\nSTOPRUN')
    env.gcode.register_command('STOPRUN', lambda g: env.manager.cancel_active_runs('test cancel'))
    assert execute(env, 'QUEUED')
    assert seen == []


def test_paused_managed_macro_cannot_spawn(klippy_env):
    env = klippy_env
    env.manager.set_paused(True)
    seen = []
    env.gcode.register_command('MOVE', lambda g: seen.append('unsafe'))
    macro(env, 'PAUSED', 'START\nMOVE\nEND\nWAIT')
    assert execute(env, 'PAUSED')
    assert seen == []


def test_collector_limit_is_per_block_not_whole_print():
    collector = BlockCollector(max_bytes=32)
    for index in range(100):
        assert collector.feed_line('G1 X123 Y456', index + 1)[0] == 'PASSTHROUGH'
    collector.feed_line('START', 101)
    with pytest.raises(ValueError, match='byte limit'):
        collector.feed_line('G1 ' + 'X' * 40, 102)


@pytest.mark.parametrize('prefix', ['', 'WAIT\n'])
def test_macro_reply_is_not_last_nested_device_reply(klippy_env, prefix):
    env = klippy_env
    seen = []
    env.gcode.register_command('DEVICE', lambda g: env.manager.driver_api.set_reply(g, {'value': 9}))
    env.gcode.register_command('SHOW', lambda g: seen.append(g.get_int('VALUE')))
    macro(env, 'HELPER', prefix + 'DEVICE')
    macro(env, 'CALLER', 'WAIT\nHELPER\nSHOW VALUE={reply.value|default(-1)}')
    assert not execute(env, 'CALLER')
    assert seen == [-1]


def test_two_complete_api_requests_wait_for_each_other(klippy_env):
    env = klippy_env
    errors, seen = [], []
    env.gcode.is_fileinput = False
    def slow(g):
        seen.append('first-start')
        env.reactor.pause(env.reactor.monotonic() + .025)
        seen.append('first-end')
    env.gcode.register_command('SLOW', slow)
    env.gcode.register_command('SECOND', lambda g: seen.append('second'))
    def first(t):
        try:
            env.gcode.run_script('START\nSLOW\nEND\nWAIT')
        except Exception as exc:
            errors.append(exc)
    def second(t):
        env.reactor.pause(env.reactor.monotonic() + .005)
        try:
            env.gcode.run_script('START\nSECOND\nEND\nWAIT')
        except Exception as exc:
            errors.append(exc)
        finally:
            env.reactor.end()
    env.reactor.register_callback(first)
    env.reactor.register_callback(second)
    env.reactor.run()
    assert not errors
    assert seen == ['first-start', 'first-end', 'second']


def test_terminal_runs_are_bounded(klippy_env):
    env = klippy_env
    for _ in range(150):
        env.gcode.run_script('WAIT')
    assert len(env.manager.runs) <= 64
