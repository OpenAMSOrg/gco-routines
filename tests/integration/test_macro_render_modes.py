"""Explicit per-macro rendering, tested through the real Klipper dispatcher."""
from types import SimpleNamespace

import configfile
import pytest
from extras import gcode_macro
from test_review_regressions import execute, macro


BODY = ("SHOW VALUE={printer.sensor.value}\nCHANGE\n"
        "SHOW VALUE={printer.sensor.value}\n{record(printer.sensor.value)}")


def instrumentation(env):
    state, events = {'value': 0}, []
    env.printer.add_object('sensor', SimpleNamespace(get_status=lambda t: dict(state)))
    def change(gcmd):
        state['value'] += 1
        events.append(('change', state['value']))
    def record(value):
        events.append(('action', value))
        return ''
    env.gcode.register_command('CHANGE', change)
    env.gcode.register_command('SHOW', lambda g: events.append(('show', g.get_int('VALUE'))))
    env.printer.lookup_object('gcode_macro').env.globals['record'] = record
    return events


@pytest.mark.parametrize('mode', [None, 'legacy', 'ordered'])
def test_mode_controls_status_and_action_timing_without_concurrency(klippy_env, mode):
    events = instrumentation(klippy_env)
    obj = macro(klippy_env, 'TIMING', BODY, render_mode=mode)
    assert obj.template.gco_render_mode == (mode or 'legacy')
    assert (obj.template.gco_runner is not None) == (mode == 'ordered')
    assert not events, 'Configuration must not evaluate actions'
    assert not execute(klippy_env, 'TIMING')
    if mode == 'ordered':
        assert events == [('show', 0), ('change', 1), ('show', 1), ('action', 1)]
    else:
        assert events == [('action', 0), ('show', 0), ('change', 1), ('show', 0)]


def test_stock_macro_has_identical_legacy_timing(stock_klippy_env):
    events = instrumentation(stock_klippy_env)
    macro(stock_klippy_env, 'TIMING', BODY)
    assert not execute(stock_klippy_env, 'TIMING')
    assert events == [('action', 0), ('show', 0), ('change', 1), ('show', 0)]


@pytest.mark.parametrize('child_mode', [None, 'legacy', 'ordered'])
@pytest.mark.parametrize('background', [False, True])
def test_ordered_caller_does_not_override_helper_mode(klippy_env, child_mode, background):
    events = instrumentation(klippy_env)
    macro(klippy_env, 'HELPER', BODY, render_mode=child_mode)
    source = 'START\nHELPER\nEND\nWAIT' if background else 'HELPER'
    macro(klippy_env, 'PARENT', source, render_mode='ordered')
    assert not execute(klippy_env, 'PARENT')
    expected = ([('show', 0), ('change', 1), ('show', 1), ('action', 1)]
                if child_mode == 'ordered' else
                [('action', 0), ('show', 0), ('change', 1), ('show', 0)])
    assert events == expected


def test_legacy_parent_can_call_ordered_helper_without_changing_its_own_timing(klippy_env):
    events = instrumentation(klippy_env)
    macro(klippy_env, 'HELPER', BODY, render_mode='ordered')
    macro(klippy_env, 'PARENT', 'HELPER\nSHOW VALUE={printer.sensor.value}')
    assert not execute(klippy_env, 'PARENT')
    assert events == [('show', 0), ('change', 1), ('show', 1), ('action', 1), ('show', 0)]


@pytest.mark.parametrize('mode', [None, 'legacy'])
def test_literal_controls_do_not_implicitly_change_macro_mode(klippy_env, mode):
    events = instrumentation(klippy_env)
    # The disabled branch is legal legacy Jinja, even with an unmatched START.
    obj = macro(klippy_env, 'LEGACY',
                '{% if false %}\nSTART\n{% endif %}\n' + BODY, render_mode=mode)
    assert obj.template.gco_runner is None
    assert not execute(klippy_env, 'LEGACY')
    assert events == [('action', 0), ('show', 0), ('change', 1), ('show', 0)]


def test_legacy_rendered_controls_fail_with_opt_in_hint_before_dispatch(klippy_env):
    events = instrumentation(klippy_env)
    macro(klippy_env, 'LEGACY', 'CHANGE\nWAIT\nCHANGE')
    errors = execute(klippy_env, 'LEGACY')
    assert errors and 'render_mode: ordered' in str(errors[0])
    assert not events


def test_control_free_ordered_loop_and_if_read_fresh_status(klippy_env):
    events = instrumentation(klippy_env)
    macro(klippy_env, 'LOOP',
          '{% set before = printer.sensor.value %}\n'
          '{% for n in range(params.COUNT|int) %}\nCHANGE\n'
          '{% if printer.sensor.value == n + 1 %}\n'
          'SHOW VALUE={printer.sensor.value}\n{% endif %}\n{% endfor %}\n'
          'SHOW VALUE={before}', render_mode='ordered')
    assert not execute(klippy_env, 'LOOP COUNT=2')
    assert events == [('change', 1), ('show', 1), ('change', 2), ('show', 2), ('show', 0)]


@pytest.mark.parametrize('mode', ['', 'automatic', 'Ordered', '"ordered"'])
def test_invalid_mode_is_a_configuration_error(klippy_env, mode):
    with pytest.raises(RuntimeError, match='render_mode.*gcode_macro BAD'):
        macro(klippy_env, 'BAD', 'CHANGE', render_mode=mode)


@pytest.mark.parametrize('mode', [None, 'legacy', 'ordered'])
def test_real_config_property_is_consumed_not_a_macro_variable(klippy_env, mode):
    env = klippy_env
    source = '[gcode_macro CONFIG_TEST]\n'
    if mode is not None:
        source += 'render_mode: %s\n' % mode
    source += ('variable_value: 7\ngcode:\n    SHOW VALUE={value}\n'
               '    SET_GCODE_VARIABLE MACRO=CONFIG_TEST VARIABLE=value VALUE=9\n'
               '    SHOW VALUE={value}\n'
               "    SHOW VALUE={printer['gcode_macro CONFIG_TEST'].value}\n")
    parsed = configfile.ConfigFileReader().build_fileconfig(source, '<test>')
    validation = configfile.ConfigValidate(env.printer)
    cfg = configfile.ConfigWrapper(env.printer, parsed, validation.access_tracking,
                                  'gcode_macro CONFIG_TEST')
    obj = gcode_macro.GCodeMacro(cfg)
    env.printer.add_object(cfg.get_name(), obj)
    validation.check_unused(parsed)
    assert obj.variables == {'value': 7}
    assert validation.get_status(0)['settings']['gcode_macro config_test']['render_mode'] == (mode or 'legacy')
    seen = []
    env.gcode.register_command('SHOW', lambda g: seen.append(g.get_int('VALUE')))
    assert not execute(env, 'CONFIG_TEST')
    # Bare macro variables are invocation-time bindings; only a fresh printer
    # status lookup sees the mutation at the ordered command boundary.
    assert seen == [7, 7, 9 if mode == 'ordered' else 7]
    errors = execute(env, 'SET_GCODE_VARIABLE MACRO=CONFIG_TEST VARIABLE=render_mode VALUE=1')
    assert errors and 'Unknown gcode_macro variable' in str(errors[0])


def test_stock_klipper_requires_omitting_extension_property(stock_klippy_env):
    env = stock_klippy_env
    source = '[gcode_macro CONFIG_TEST]\nrender_mode: ordered\ngcode:\n    G4 P0\n'
    parsed = configfile.ConfigFileReader().build_fileconfig(source, '<test>')
    validation = configfile.ConfigValidate(env.printer)
    cfg = configfile.ConfigWrapper(env.printer, parsed, validation.access_tracking,
                                  'gcode_macro CONFIG_TEST')
    env.printer.add_object(cfg.get_name(), gcode_macro.GCodeMacro(cfg))
    with pytest.raises(configfile.error, match="Option 'render_mode' is not valid"):
        validation.check_unused(parsed)


def test_ordered_macro_aborts_before_later_lines_on_command_error(klippy_env):
    events = instrumentation(klippy_env)
    def fail(gcmd):
        raise gcmd.error('test failure')
    klippy_env.gcode.register_command('FAIL', fail)
    macro(klippy_env, 'ABORT', 'FAIL\nCHANGE\n{record(1)}', render_mode='ordered')
    assert execute(klippy_env, 'ABORT')
    assert not events


def test_legacy_inline_jinja_and_permissive_undefined_are_preserved(klippy_env):
    events = instrumentation(klippy_env)
    macro(klippy_env, 'INLINE', 'SHOW VALUE={% if true %}7{% endif %}{missing}')
    assert not execute(klippy_env, 'INLINE')
    assert events == [('show', 7)]


@pytest.mark.parametrize('source', [
    'SHOW VALUE={% if true %}7{% endif %}',
    '{% include "external.jinja" %}',
    '{% import "external.jinja" as helpers %}',
    '{% from "external.jinja" import helper %}',
    '{% extends "external.jinja" %}',
])
def test_control_free_ordered_macros_keep_managed_restrictions(klippy_env, source):
    with pytest.raises(RuntimeError, match='separate command lines|E_TEMPLATE_COMPOSITION'):
        macro(klippy_env, 'BAD', source, render_mode='ordered')
