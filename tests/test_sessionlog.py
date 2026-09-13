"""
Mirroring the run into session.log.

The point of the log is the runs that go wrong, so the properties that matter are the
awkward ones: that stderr is captured as well as stdout (`sys.exit("...")` writes
there, so a stdout-only log records everything except why the roll stopped), that
operator prompts are captured (they are the narrative), and that every line is on disk
before the next one is written (an abort must not cost the line explaining it).
"""

import os
import pty
import select
import subprocess
import sys
import textwrap

from trichrom.lib import sessionlog

SCRIPT = textwrap.dedent("""
    import sys
    sys.path.insert(0, {src!r})
    from trichrom.lib import sessionlog
    sessionlog.start({dir!r}, argv=['trichrom-scan', '--roll-id', 'roll01'])
    print('to stdout')
    print('to stderr', file=sys.stderr)
    {tail}
""")


def _run(tmp_path, tail='', stdin=''):
    """Run in a subprocess: interpreter-level exit output only happens for real."""
    src = str(__import__('trichrom').__file__).rsplit('/trichrom/', 1)[0]
    code = SCRIPT.format(src=src, dir=str(tmp_path), tail=tail)
    proc = subprocess.run([sys.executable, '-c', code], input=stdin,
                          capture_output=True, text=True)
    return proc, (tmp_path / sessionlog.LOG_NAME).read_text()


def test_captures_stdout(tmp_path):
    _, log = _run(tmp_path)
    assert 'to stdout' in log


def test_captures_stderr(tmp_path):
    """sys.exit writes here, so a stdout-only log would miss every abort."""
    _, log = _run(tmp_path)
    assert 'to stderr' in log


def test_captures_the_reason_a_run_aborted(tmp_path):
    """The regression guard. The interpreter prints a SystemExit message after main()
    has unwound, so anything that restores the streams on the way out loses it."""
    _, log = _run(tmp_path, tail="sys.exit('CALIBRATION ABORTED')")
    assert 'CALIBRATION ABORTED' in log


def test_captures_an_uncaught_traceback(tmp_path):
    _, log = _run(tmp_path, tail="raise RuntimeError('unexpected')")
    assert 'Traceback' in log and 'unexpected' in log


def _run_on_a_tty(tmp_path, tail, send=b'\n'):
    """Run with a real terminal on stdin and stdout.

    A pipe is not enough to exercise this: `input()` only reaches for CPython's
    readline path — the one that writes the prompt past a replaced `sys.stdout` — when
    both streams are genuine ttys. Tested over a pipe, a `_Tee` that exposed `fileno()`
    passes happily and loses every prompt in real use.
    """
    src = str(__import__('trichrom').__file__).rsplit('/trichrom/', 1)[0]
    code = SCRIPT.format(src=src, dir=str(tmp_path), tail=tail)
    master, slave = pty.openpty()
    proc = subprocess.Popen([sys.executable, '-c', code],
                            stdin=slave, stdout=slave, stderr=slave, close_fds=True)
    os.close(slave)
    os.write(master, send)
    while proc.poll() is None:                      # drain, or the child blocks on a full pty
        if select.select([master], [], [], 0.2)[0]:
            try:
                if not os.read(master, 4096):
                    break
            except OSError:
                break
    proc.wait(timeout=10)
    os.close(master)
    return (tmp_path / sessionlog.LOG_NAME).read_text()


def test_captures_operator_prompts_on_a_real_terminal(tmp_path):
    """Prompts are the narrative of a calibration, and a replaced stdout loses them most
    easily: CPython's readline path writes the prompt straight to the terminal. That path
    is only taken when sys.stdout looks fully file-like — which is why `_Tee` defines no
    `fileno`/`encoding`/`errors`."""
    log = _run_on_a_tty(tmp_path, tail="input('Remove the film holder')")
    assert 'Remove the film holder' in log




def test_still_prints_to_the_terminal(tmp_path):
    """Mirroring, not redirecting — the operator must still see everything."""
    proc, _ = _run(tmp_path)
    assert 'to stdout' in proc.stdout
    assert 'to stderr' in proc.stderr


def test_each_line_is_on_disk_before_the_next(tmp_path):
    """Unflushed output is lost when a run aborts, which is when the log is read."""
    stop = sessionlog.start(tmp_path, argv=['trichrom-scan'])
    try:
        print('written but not yet closed')
        assert 'written but not yet closed' in (tmp_path / sessionlog.LOG_NAME).read_text()
    finally:
        stop()


def test_appends_across_runs_with_a_header_each(tmp_path):
    """A session dir accumulates runs — a failed calibration, a retry, the roll. Each is
    evidence about the same roll, so a later run must not erase an earlier one."""
    _run(tmp_path)
    _, log = _run(tmp_path)
    assert log.count('to stdout') == 2
    assert log.count('trichrom-scan --roll-id roll01') == 2, "each run needs its own header"
