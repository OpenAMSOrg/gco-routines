import pytest
from gcoroutines.semantics import Registry, ContractError, freeze

def test_default_and_anonymous_wait():
    r=Registry();a=r.start();b=r.start()
    assert not r.wait(r.default)
    r.finish(b, {'order':2});assert r.routines[r.default].state == 'waiting'
    r.finish(a, {'order':1})
    assert [x['order'] for x in r.routines[r.default].waited] == [1,2]
    assert r.wait(r.default) and r.routines[r.default].waited == ()

def test_explicit_wait_order():
    r=Registry();a=r.start('a');b=r.start('b')
    r.finish(a, {'which':'a'});r.finish(b, {'which':'b'})
    assert r.wait(r.default,['b','a'])
    assert [x['which'] for x in r.routines[r.default].waited] == ['b','a']

def test_background_waiting_on_background():
    r=Registry();a=r.start('a');b=r.start('b')
    assert not r.wait(b,['a'])
    assert not r.wait(r.default,['b'])
    r.finish(a, {'v':1})
    assert r.routines[b].state=='running'
    assert r.routines[r.default].state=='waiting'
    r.finish(b, {'v':2})
    assert r.routines[r.default].waited[0]['v']==2

def test_multiple_waiters_retain_result():
    r=Registry();a=r.start('a');b=r.start('b');c=r.start('c')
    r.wait(b,['a']);r.wait(c,['a']);r.finish(a,{'x':7})
    assert r.routines[b].waited[0]['x'] == r.routines[c].waited[0]['x'] == 7
    assert r.wait(r.default,['a'])

def test_already_completed_not_lost():
    r=Registry();a=r.start();r.finish(a,{'x':1})
    assert r.wait(r.default)
    assert r.routines[r.default].waited[0]['x']==1

def test_wait_targets_are_snapshot_not_future_starts():
    r=Registry();a=r.start('a');b=r.start('b');r.wait(b)
    c=r.start('c')
    r.finish(a)
    assert r.routines[b].state=='running' and r.routines[c].state=='running'

def test_cycle_rejected_atomically():
    r=Registry();a=r.start('a');b=r.start('b')
    r.wait(a,['b']);before=r.snapshot()
    with pytest.raises(ContractError):r.wait(b,['a'])
    assert r.snapshot()==before

def test_bare_cycle_excluding_self_still_rejected():
    r=Registry();a=r.start('a');b=r.start('b');r.wait(a)
    with pytest.raises(ContractError):r.wait(b)

@pytest.mark.parametrize('targets', [['missing'],[],['a','a'],['default']])
def test_invalid_targets(targets):
    r=Registry();r.start('a')
    with pytest.raises(ContractError):r.wait(r.default,targets)

def test_self_wait():
    r=Registry();a=r.start('a')
    with pytest.raises(ContractError):r.wait(a,['a'])

def test_name_reuse_needs_starter_collection():
    r=Registry();a=r.start('a');b=r.start('b');r.finish(a)
    assert r.wait(b,['a'])
    with pytest.raises(ContractError):r.start('a')
    r.wait(r.default,['a']);new=r.start('a')
    assert new!=a and r.routines[b].waited==(r.routines[a].result,)

def test_pending_wait_keeps_original_identity_through_reuse():
    r=Registry();a=r.start('a');b=r.start('b');c=r.start('c')
    r.wait(c,['a','b']);r.finish(a,{'version':1});r.wait(r.default,['a'])
    new=r.start('a');r.finish(new,{'version':2});r.finish(b)
    assert r.routines[c].waited[0]['version']==1

def test_repeated_explicit_wait_reads_same_instance():
    r=Registry();a=r.start('a');r.finish(a,{'x':1})
    assert r.wait(r.default,['a']);assert r.wait(r.default,['a'])
    assert r.routines[r.default].waited[0]['x']==1

def test_results_are_detached_immutable_and_snapshots_fresh():
    r=Registry();a=r.start();data={'nested':[{'x':1}]};r.finish(a,data)
    data['nested'][0]['x']=4;r.wait(r.default)
    with pytest.raises(TypeError):r.routines[r.default].waited[0]['nested'][0]['x']=8
    s=r.snapshot();s['routines'][1]['result']['nested'][0]['x']=9
    assert r.snapshot()['routines'][1]['result']['nested'][0]['x']==1

@pytest.mark.parametrize('data',[{'x':object()},{'x':float('nan')},{1:'bad'},['not-a-map']])
def test_invalid_results_do_not_commit_completion(data):
    r=Registry();a=r.start()
    with pytest.raises(ContractError):r.finish(a,data)
    assert r.routines[a].state=='running'

def test_jinja_undefined_cannot_be_exported():
    from jinja2 import StrictUndefined
    with pytest.raises(ContractError):freeze({'x':StrictUndefined(name='x')})

def test_failure_holds_run_and_prevents_late_success():
    r=Registry();a=r.start('a');r.wait(r.default,['a']);r.fail(a,'jam')
    assert r.routines[r.default].state=='cancelled'
    with pytest.raises(ContractError):r.finish(a)
    with pytest.raises(ContractError):r.start('b')

def test_cancel_is_software_termination_not_a_hardware_ack():
    r=Registry();a=r.start();r.cancel()
    assert r.routines[a].state=='cancelled'
    assert 'hardware_stopped' not in r.snapshot()

def test_default_end_requires_implicit_final_wait():
    r=Registry();a=r.start()
    with pytest.raises(ContractError):r.finish(r.default)
    r.wait(r.default);r.finish(a);r.finish(r.default)
    assert r.routines[r.default].state=='completed'

def test_limits_reject_instead_of_evicting_results():
    r=Registry(max_routines=2);a=r.start();r.finish(a);r.wait(r.default)
    with pytest.raises(ContractError):r.start()

def test_no_nested_spawn():
    r=Registry();a=r.start()
    with pytest.raises(ContractError):r.start(caller=a)

def test_observability_revision_and_source():
    r=Registry();a=r.start();before=r.revision
    r.observe_command(a,'T0',{'phase':'handoff'}, {'macro':'PREPARE','line':3})
    s=r.snapshot();assert s['revision']>before
    assert s['routines'][1]['source']['line']==3
