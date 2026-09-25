# Integration tests for virtual-SD file lifecycle, EOF join, cancel/pause, and status (I02, I05, I06, I08, I10, I11)

import io
import json
import jsonschema
import os
from types import SimpleNamespace
import pytest
from gco_routines.integration import ManagedFileWrapper
from gco_routines.program import BlockCollector
from extras import virtual_sdcard


class _SyncExecutor:
    def submit(self, func, *args, **kwargs):
        return func(*args, **kwargs)

    def wrap_obj(self, value):
        return value


class _PrintStats:
    def __init__(self):
        self.filename = None

    def set_current_file(self, filename):
        self.filename = filename

    def reset(self):
        self.filename = None


class _LoadCommand:
    def __init__(self, filename):
        self.filename = filename
        self.responses = []

    def get(self, name, default=None):
        return self.filename if name == "FILENAME" else default

    def get_raw_command_parameters(self):
        return self.filename

    def respond_raw(self, message):
        self.responses.append(message)

    def error(self, message):
        return RuntimeError(message)


def _virtual_sd(env, directory):
    vsd = virtual_sdcard.VirtualSD.__new__(virtual_sdcard.VirtualSD)
    vsd.printer = env.printer
    vsd.gcode = env.gcode
    vsd.reactor = env.reactor
    vsd.sdcard_dirname = str(directory)
    vsd.current_file = None
    vsd.file_position = vsd.file_size = 0
    vsd.work_timer = None
    vsd.must_pause_work = vsd.cmd_from_sd = False
    vsd.executor = _SyncExecutor()
    vsd.print_stats = _PrintStats()
    vsd.do_resume = lambda: None
    env.printer.add_object("virtual_sdcard", vsd)
    env.manager.adapter._hook_virtual_sdcard(vsd)
    return vsd


def test_real_virtual_sd_load_hook_covers_m23_and_print_file(klippy_env, tmp_path):
    first = tmp_path / "first.gcode"
    second = tmp_path / "second.gcode"
    first.write_text("G1 X1\n")
    second.write_text("G1 X2\n")
    vsd = _virtual_sd(klippy_env, tmp_path)

    vsd.cmd_M23(_LoadCommand("first.gcode"))
    assert isinstance(vsd.current_file, ManagedFileWrapper)
    assert klippy_env.manager.get_current_run().run_id.startswith("sd_print")
    assert vsd.current_file.read().endswith("G1 X1\n")
    assert vsd.current_file.read() == ""
    assert klippy_env.manager.get_current_routine().state == "completed"

    vsd.cmd_SDCARD_PRINT_FILE(_LoadCommand("second.gcode"))
    assert isinstance(vsd.current_file, ManagedFileWrapper)
    assert vsd.current_file.read().endswith("G1 X2\n")


def test_virtual_sd_preflight_rejects_unclosed_block_before_open(klippy_env, tmp_path):
    bad = tmp_path / "bad.gcode"
    bad.write_text("START NAME=oops\nG1 X1\n")
    vsd = _virtual_sd(klippy_env, tmp_path)
    # A command error, not a bare ValueError: M23 runs inside Klipper's
    # dispatcher, which shuts the printer down on any other exception.
    with pytest.raises(klippy_env.gcode.error, match="E_UNCLOSED_START"):
        vsd.cmd_M23(_LoadCommand("bad.gcode"))
    assert vsd.current_file is None


def test_pause_blocks_new_routines_until_resume(klippy_env):
    class Pause:
        def cmd_PAUSE(self, gcmd):
            return None

        def cmd_RESUME(self, gcmd):
            return None

        def cmd_CLEAR_PAUSE(self, gcmd):
            return None

        def cmd_CANCEL_PRINT(self, gcmd):
            return None

    pause = Pause()
    for command in ("PAUSE", "RESUME", "CLEAR_PAUSE", "CANCEL_PRINT"):
        klippy_env.gcode.register_command(command, getattr(pause, "cmd_" + command))
    klippy_env.manager.adapter._hook_pause_resume(pause)

    klippy_env.gcode.run_script("PAUSE")
    assert klippy_env.manager.admissions_paused
    with pytest.raises(Exception, match="admission is paused"):
        klippy_env.gcode.run_script("START NAME=x\nEND\nWAIT ON=x")
    klippy_env.gcode.run_script("RESUME")
    assert not klippy_env.manager.admissions_paused

    def invoke(eventtime):
        try:
            klippy_env.gcode.run_script("START NAME=x\nEND\nWAIT ON=x")
        finally:
            klippy_env.reactor.end()
        return klippy_env.reactor.NEVER

    klippy_env.reactor.register_callback(invoke)
    klippy_env.reactor.run()
    assert all(item["state"] == "completed"
               for item in klippy_env.manager.get_status()["routines"])

def test_status_schema_compliance(klippy_env):
    """Verify get_status output strictly conforms to schemas/status.schema.json (I08)."""
    env = klippy_env
    mgr = env.manager
    status = mgr.get_status()

    schema_path = os.path.join(os.path.dirname(__file__), "..", "..", "schemas", "status.schema.json")
    with open(schema_path) as f:
        schema = json.load(f)

    jsonschema.validate(instance=status, schema=schema)
    assert status["schema_version"] == 1
    assert len(status["routines"]) >= 1


def test_virtual_sd_eof_join_delays_completion(klippy_env):
    """Verify ManagedFileWrapper executes implicit final wait before yielding EOF (I05)."""
    env = klippy_env
    r = env.reactor
    mgr = env.manager

    # Start a routine that takes 0.040s
    run = mgr.get_current_run()
    child = run.start_routine("bg_job", caller_id=run.default_id)

    log = []

    def _bg_task(eventtime):
        r.pause(r.monotonic() + 0.040)
        run.finish_routine(child.id, {"done": True})
        log.append(("child_finished", r.monotonic()))
        return r.NEVER

    r.register_callback(_bg_task)

    raw_stream = io.BytesIO(b"G1 X10\n")
    collector = BlockCollector()
    wrapper = ManagedFileWrapper(raw_stream, mgr, collector)

    done = [False]

    def test_run(eventtime):
        try:
            chunk1 = wrapper.read(8192)
            assert chunk1 == b"G1 X10\n"
            # Second read reaches EOF -> must block until child finishes!
            log.append(("read_eof_start", r.monotonic()))
            chunk2 = wrapper.read(8192)
            assert chunk2 == b""
            log.append(("read_eof_end", r.monotonic()))
            done[0] = True
        finally:
            r.end()
        return r.NEVER

    r.register_callback(test_run)
    r.run()

    assert done[0]
    events = [e[0] for e in log]
    assert events == ["read_eof_start", "child_finished", "read_eof_end"]

    # read_eof_end occurred after child_finished
    t_child_end = [t for e, t in log if e == "child_finished"][0]
    t_read_end = [t for e, t in log if e == "read_eof_end"][0]
    assert t_read_end >= t_child_end


def test_unclosed_start_at_eof_raises(klippy_env):
    """Verify unclosed START block at EOF is detected and raises error before print completion."""
    env = klippy_env
    mgr = env.manager

    collector = BlockCollector()
    collector.feed_line("START NAME=unclosed")
    collector.feed_line("    T0")

    raw_stream = io.BytesIO(b"")
    wrapper = ManagedFileWrapper(raw_stream, mgr, collector)

    with pytest.raises(ValueError) as exc:
        wrapper.read(8192)
    assert "E_UNCLOSED_START" in str(exc.value)


def test_cancel_while_waiting_unwinds_lock(klippy_env):
    """Verify API cancellation faults the run and unwinds suspended wait (I06)."""
    env = klippy_env
    r = env.reactor
    mgr = env.manager
    gcode = env.gcode

    run = mgr.get_current_run()
    child = run.start_routine("slow_child", caller_id=run.default_id)

    log = []

    def cancel_after_delay(eventtime):
        r.pause(r.monotonic() + 0.020)
        log.append(("cancel_sent", r.monotonic()))
        mgr.cancel_active_runs("Print cancelled by user")
        return r.NEVER

    r.register_callback(cancel_after_delay)

    done = [False]
    error = [None]

    def test_run(eventtime):
        try:
            curr = mgr.get_current_routine()
            log.append(("wait_start", r.monotonic()))
            mgr.wait_for_routine(curr, ["slow_child"])
            log.append(("wait_end", r.monotonic()))
            done[0] = True
        except Exception as e:
            error[0] = e
            log.append(("wait_error", r.monotonic()))
        finally:
            r.end()
        return r.NEVER

    r.register_callback(test_run)
    r.run()

    assert not done[0]
    assert error[0] is not None
    assert "faulted" in str(error[0]).lower() or "cancelled" in str(error[0]).lower()
    assert run.is_cancelled
    events = [e[0] for e in log]
    assert events == ["wait_start", "cancel_sent", "wait_error"]


def test_command_collision_preflight_fails_closed(klippy_env):
    """Verify preflight rejects registration if START/END/WAIT already registered (I10)."""
    env = klippy_env
    # Attempt to load manager again with conflicting START command already present in gcode
    cfg = SimpleNamespace(
        get_printer=lambda: env.printer,
        error=lambda msg: Exception(msg)
    )
    import gco_routines
    with pytest.raises(Exception) as exc:
        gco_routines.load_config(cfg)
    assert "collides with an existing registration" in str(exc.value)


@pytest.mark.parametrize('failure', [False, True])
@pytest.mark.parametrize('explicit_wait', [False, True])
def test_real_sd_worker_joins_children_and_runs_error_cleanup(klippy_env, tmp_path, failure, explicit_wait):
    env = klippy_env
    env.gcode.is_fileinput = False
    env.printer.lookup_object('gcode_io').is_fileinput = False
    events = []
    def background(g):
        env.reactor.pause(env.reactor.monotonic() + .01)
        if failure:
            raise g.error('background device failed')
        events.append('background-done')
    env.gcode.register_command('BACKGROUND', background)
    env.gcode.register_command('CLEANUP', lambda g: events.append('cleanup'))
    env.gcode.register_command('AFTER', lambda g: events.append('after'))
    path = tmp_path / 'job.gcode'
    path.write_text('START\nBACKGROUND\nEND\n' + ('WAIT\nAFTER\n' if explicit_wait else ''))
    vsd = _virtual_sd(env, tmp_path)
    # Attach the recovery template before re-installing the hooks.
    env.manager.adapter._hooked_vsd.remove(id(vsd))
    vsd.on_error_gcode = SimpleNamespace(render=lambda: 'CLEANUP')
    env.manager.adapter._hook_virtual_sdcard(vsd)
    vsd.print_stats.note_start = lambda: events.append('start')
    vsd.print_stats.note_complete = lambda: events.append('complete')
    vsd.print_stats.note_pause = lambda: events.append('paused')
    vsd.print_stats.note_error = lambda error: events.append('error')
    vsd.cmd_M23(_LoadCommand('job.gcode'))
    vsd.work_timer = env.reactor.register_timer(vsd.work_handler, env.reactor.NOW)
    def finished(t):
        if vsd.work_timer is None:
            env.reactor.end()
            return env.reactor.NEVER
        return t + .01
    env.reactor.register_timer(finished, env.reactor.monotonic() + .05)
    env.reactor.run()
    if failure:
        assert 'cleanup' in events
        assert 'after' not in events and 'complete' not in events
        assert events[-1] == 'error'
    else:
        assert events == ['start', 'background-done'] + (['after'] if explicit_wait else []) + ['complete']
