"""Isolated audit probes, not Klipper integration or printer tests.

Scheduling is a deterministic test double. Dispatcher/mutex/completion and
macro guard methods come from the labeled upstream excerpts. Jinja is the
version installed in this environment, NOT Klipper's pinned Jinja 2.11.3.
"""
from contextlib import nullcontext
from types import SimpleNamespace

import greenlet
import jinja2
import pytest
from jinja2.runtime import Namespace

from upstream_excerpts import (
    CommandError, DispatchMethods, GCodeCommand, GetStatusWrapper,
    MacroCommandMethod, ReactorCompletion, ReactorMutex,
)

class TestReactor:
    __test__ = False
    NOW = 0.0
    NEVER = 9999999999999999.0
    def __init__(self):
        self.time = 0.0
        self.tasks = []
        self.driver = greenlet.getcurrent()
    def monotonic(self):
        return self.time
    def assert_no_pause(self):
        return nullcontext()
    def call(self, callback):
        task = greenlet.greenlet(callback, parent=self.driver)
        task.timer = SimpleNamespace(task=task, deadline=self.time)
        self.tasks.append(task)
        return task
    def pause(self, when):
        task = greenlet.getcurrent()
        task.timer.deadline = when
        self.driver.switch()
        return self.time
    def update_timer(self, timer, when):
        timer.deadline = max(self.time, when)
    def run(self, limit=200):
        for _ in range(limit):
            live = [t for t in self.tasks if not t.dead]
            ready = [t for t in live if t.timer.deadline < self.NEVER]
            if not ready:
                return live
            task = min(ready, key=lambda t: t.timer.deadline)
            self.time = max(self.time, task.timer.deadline)
            task.timer.deadline = self.NEVER
            task.switch()
        raise AssertionError('Probe exceeded deterministic scheduling budget')

class DispatchHarness(DispatchMethods):
    def __init__(self, reactor):
        self.mutex = ReactorMutex(reactor, False)
        self.gcode_handlers = {}
        self.messages = []
        self.events = []
        self.printer = SimpleNamespace(
            send_event=lambda name: self.events.append(name),
            invoke_shutdown=lambda msg: self.events.append(('shutdown', msg)),
        )
    def respond_info(self, msg, **kwargs):
        self.messages.append(msg)
    respond_raw = respond_info
    _respond_error = respond_info
    def cmd_default(self, gcmd):
        # Exact final ready-state fallback from GCodeDispatch.cmd_default.
        gcmd.respond_info('Unknown command:"%s"' % (gcmd.get_command(),))
    def register_extended(self, name, handler):
        self.gcode_handlers[name] = lambda c: handler(self._get_extended_params(c))


def test_extended_parameters_preserve_case_and_comma_list():
    g = DispatchHarness(TestReactor())
    seen = []
    g.register_extended('WAIT', lambda c: seen.append(c.get_command_parameters()))
    g.run_script('WAIT ON=filament_change,NozzleHeating ; comment')
    assert seen == [{'ON': 'filament_change,NozzleHeating'}]


def test_original_dispatcher_discards_handler_return_value():
    g = DispatchHarness(TestReactor())
    g.gcode_handlers['T0'] = lambda c: {'lane': 2}
    assert g.run_script('T0') is None


def test_unrecognized_control_command_does_not_abort_later_work():
    g = DispatchHarness(TestReactor())
    executed = []
    g.gcode_handlers['T0'] = lambda c: executed.append('T0')
    g.run_script('START\nT0\nEND\nWAIT')
    assert executed == ['T0']
    assert len(g.messages) == 3


def test_start_handler_alone_cannot_capture_following_commands():
    g = DispatchHarness(TestReactor())
    log = []
    for name in ['START', 'T0', 'END']:
        g.gcode_handlers[name] = lambda c: log.append(c.get_command())
    g.run_script('START\nT0\nEND')
    assert log == ['START', 'T0', 'END']


def mutex_scenario(child_uses_normal_entry):
    r = TestReactor()
    g = DispatchHarness(r)
    completion = ReactorCompletion(r)
    events = []
    def begin(c):
        events.append('parent-start')
        def child():
            runner = g.run_script if child_uses_normal_entry else g.run_script_from_command
            runner('T0')
        r.call(child)
        r.call(lambda: g.run_script('OUTSIDE'))
    def load(c):
        events.append('child-start')
        r.pause(r.time + 2.0)
        events.append('child-done')
        completion.complete({'lane': 2})
    g.gcode_handlers.update({
        'START': begin,
        'T0': load,
        'WAIT': lambda c: completion.wait(),
        'AFTER': lambda c: events.append('parent-after'),
        'OUTSIDE': lambda c: events.append('external-after'),
    })
    r.call(lambda: g.run_script('START\nWAIT\nAFTER'))
    return r.run(), events, r, completion


def test_normal_child_entry_deadlocks_behind_waiting_parent_mutex():
    blocked, events, reactor, completion = mutex_scenario(True)
    assert len(blocked) == 3
    assert events == ['parent-start']
    assert not completion.test()


def test_managed_child_bypass_allows_progress_and_preserves_external_lock():
    blocked, events, reactor, completion = mutex_scenario(False)
    assert not blocked
    assert events == ['parent-start', 'child-start', 'child-done',
                      'parent-after', 'external-after']
    assert completion.result == {'lane': 2}


def test_completion_can_wake_multiple_waiters_and_retain_result():
    r = TestReactor()
    c = ReactorCompletion(r)
    values = []
    r.call(lambda: values.append(('a', c.wait())))
    r.call(lambda: values.append(('b', c.wait())))
    def producer():
        r.pause(r.time + 1)
        c.complete({'ok': True})
    r.call(producer)
    assert not r.run()
    assert dict(values) == {'a': {'ok': True}, 'b': {'ok': True}}
    assert c.wait() == {'ok': True}


def test_independent_cooperative_commands_overlap_in_test_reactor():
    r = TestReactor()
    g = DispatchHarness(r)
    events = []
    completions = []
    def operation(seconds, label):
        events.append((label, 'start', r.time))
        r.pause(r.time + seconds)
        events.append((label, 'done', r.time))
    g.gcode_handlers['T0'] = lambda c: operation(2, 'feed')
    g.gcode_handlers['M109'] = lambda c: operation(5, 'heat')
    def parent():
        with g.mutex:
            for command in ['T0', 'M109 S220']:
                done = ReactorCompletion(r)
                completions.append(done)
                def child(command=command, done=done):
                    g.run_script_from_command(command)
                    done.complete(None)
                r.call(child)
            for done in completions:
                done.wait()
    r.call(parent)
    assert not r.run()
    assert r.time == 5.0
    assert {t for _, phase, t in events if phase == 'start'} == {0.0}


def test_macro_recursion_flag_rejects_unrelated_concurrent_call():
    r = TestReactor()
    g = DispatchHarness(r)
    macro = MacroCommandMethod()
    macro.alias = 'HELPER'
    macro.variables = {}
    macro.in_script = False
    macro.template = SimpleNamespace(
        create_template_context=lambda: {},
        run_gcode_from_command=lambda context: r.pause(r.time + 1),
    )
    cmd = GCodeCommand(g, 'HELPER', 'HELPER', {}, False)
    errors = []
    r.call(lambda: macro.cmd(cmd))
    def second():
        try:
            macro.cmd(cmd)
        except CommandError as exc:
            errors.append(str(exc))
    r.call(second)
    assert not r.run()
    assert errors == ['Macro HELPER called recursively']


def env():
    return jinja2.Environment('{%', '%}', '{', '}', undefined=jinja2.StrictUndefined)


def test_stock_render_reads_result_before_command_can_supply_it():
    template = env().from_string('T0\n{% set result.lane = reply.lane %}\nM117 {result.lane}')
    with pytest.raises(jinja2.UndefinedError):
        template.render(result=Namespace(), reply={})


def test_generator_compiles_waited_lookup_before_barrier():
    template = env().from_string('WAIT\nM117 {waited[0].lane}\n')
    context = template.new_context({'waited': ({'lane': 0},)})
    generated = template.root_render_func(context)
    assert next(generated) == 'WAIT\nM117 '
    context.vars['waited'] = ({'lane': 2},)
    # Replacing the context binding is not enough: generated code cached it.
    assert ''.join(generated) == '0'


def test_dynamic_result_binding_can_bridge_barrier_in_simple_generator():
    current = {'waited': ({'lane': 0},)}
    class WaitedProxy:
        def __getitem__(self, index):
            return current['waited'][index]
    template = env().from_string('WAIT\nM117 {waited[0].lane}\n')
    generated = template.generate(waited=WaitedProxy())
    assert next(generated) == 'WAIT\nM117 '
    current['waited'] = ({'lane': 2},)
    assert ''.join(generated) == '2'


def test_generating_child_to_find_end_evaluates_child_statements_early():
    result = Namespace()
    template = env().from_string(
        'START\nT0\n{% set result.executed = true %}\nEND\n')
    text = ''.join(template.generate(result=result))
    assert 'T0' in text
    # No command was dispatched, but child Jinja has already run.
    assert result.executed is True


def test_printer_status_wrapper_retains_old_snapshot():
    r = TestReactor()
    state = {'temperature': 20}
    printer = SimpleNamespace(
        lookup_object=lambda name, default=None: SimpleNamespace(
            get_status=lambda eventtime: dict(state)),
        get_reactor=lambda: r,
    )
    wrapper = GetStatusWrapper(printer)
    assert wrapper['extruder']['temperature'] == 20
    state['temperature'] = 220
    assert wrapper['extruder']['temperature'] == 20
    assert GetStatusWrapper(printer)['extruder']['temperature'] == 220
