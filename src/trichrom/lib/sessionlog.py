"""Mirror everything the run prints into session.log, beside session.json."""

import sys
import time
from pathlib import Path

LOG_NAME = 'session.log'


class _Tee:
    """
    Writes to the terminal and to the log at once.

    Flushed on every write because the failures worth reading back are the ones that
    end the process: an abort mid-calibration must not cost the line explaining it.

    Do not give this class `fileno`, `encoding` and `errors`. With all of those present
    `input()` looks at a real terminal, takes CPython's readline path, and writes its
    prompt straight to the tty — past this object, so operator prompts vanish from the
    log. Lacking any one of them is enough to keep the ordinary path, which writes
    through `write()` like everything else.
    """

    def __init__(self, stream, log):
        self._stream = stream
        self._log = log

    def write(self, text):
        self._stream.write(text)
        self._stream.flush()
        self._log.write(text)
        self._log.flush()
        return len(text)

    def flush(self):
        self._stream.flush()
        self._log.flush()

    def isatty(self):
        return False


def start(session_dir, argv=None):
    """
    Begin mirroring stdout and stderr into `session_dir/session.log`.

    stderr as well as stdout, because the lines most worth having are the ones that
    end the roll: `sys.exit("...")` and tracebacks both go to stderr, so a log of
    stdout alone would record everything except why the run stopped.

    Appends. A session dir accumulates runs — a failed calibration, a retry, the
    roll itself — and each one's log is evidence about the same roll.

    Returns a callable that restores the original streams — for tests. A real run must
    *not* call it. `sys.exit("...")` and uncaught tracebacks are printed by the
    interpreter after `main()` has unwound, so anything that restores the streams on the
    way out drops the one line explaining why the roll stopped. Measured: restoring in a
    `finally` loses the abort message entirely; leaving the tee in place keeps it. The
    process is ending anyway, and every write is already flushed.
    """
    path = Path(session_dir) / LOG_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    log = open(path, 'a', encoding='utf-8')

    command = ' '.join(argv if argv is not None else sys.argv)
    log.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')}  {command}\n")
    log.flush()

    saved_out, saved_err = sys.stdout, sys.stderr
    sys.stdout = _Tee(saved_out, log)
    sys.stderr = _Tee(saved_err, log)

    def stop():
        sys.stdout, sys.stderr = saved_out, saved_err
        log.close()

    return stop

