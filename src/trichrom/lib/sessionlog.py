"""
Diagnostic log for a scanning session, written to session.log beside session.json.

Not a transcript of what scrolled past. The terminal output is a narrative for the
operator, and re-reading it later rarely answers anything. What answers things are the
numbers behind each decision: the level a probe read, the channel means a frame was
accepted or refused on, the sensor geometry, the library versions in play. Every
failure this rig has produced so far was diagnosed from measurements like those, and in
each case they had to be recovered afterwards by re-reading the ARWs.

Appends, so a session dir accumulates its failed calibrations and the retries after
them as evidence about one roll.
"""

import logging
import platform
import sys
from pathlib import Path

LOG_NAME = 'session.log'
FORMAT = '%(asctime)s %(levelname)-5s %(name)-24s %(message)s'

# Configured on the package logger, so every getLogger(__name__) under trichrom.* feeds
# into it without each module needing to know this exists.
ROOT = 'trichrom'

log = logging.getLogger(__name__)


def start(session_dir, argv=None):
    """Begin writing session.log in `session_dir`. Returns its path."""
    path = Path(session_dir) / LOG_NAME
    path.parent.mkdir(parents=True, exist_ok=True)

    handler = logging.FileHandler(path, encoding='utf-8')
    handler.setFormatter(logging.Formatter(FORMAT))

    root = logging.getLogger(ROOT)
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    # The operator's output is written by print(); this record is separate and must not
    # reach the terminal through an ancestor logger.
    root.propagate = False

    log.info('=== run: %s', ' '.join(argv if argv is not None else sys.argv))
    _record_environment()
    return path


def stop():
    """Close and detach the handler. For tests; a real run just exits."""
    root = logging.getLogger(ROOT)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()


def _record_environment():
    """
    Versions and platform, once per run.

    Worth the line: a numpy built from source against a Python it predated once made
    scipy.ndimage return silently wrong numbers here, and that looked like a bug in the
    leader measurement for as long as it took to think of checking versions.
    """
    import importlib.metadata as meta

    versions = []
    for package in ('numpy', 'scipy', 'rawpy', 'tifffile', 'pyexiv2', 'pyserial'):
        try:
            versions.append(f'{package} {meta.version(package)}')
        except Exception:
            versions.append(f'{package} absent')
    log.info('python %s on %s', sys.version.split()[0], platform.platform())
    log.info('deps: %s', ', '.join(versions))


def record_geometry(sizes, active):
    """
    The crop actually used, once per run.

    The A7R V reports top_margin=0 and keeps its real border in crop_top_margin, so
    which rectangle the pipeline settled on is not something to infer after the fact.
    """
    log.info('raw %sx%s | visible %sx%s at (%s,%s) | camera crop %sx%s at (%s,%s) | using %s',
             sizes.raw_height, sizes.raw_width, sizes.height, sizes.width,
             sizes.top_margin, sizes.left_margin,
             getattr(sizes, 'crop_height', '?'), getattr(sizes, 'crop_width', '?'),
             getattr(sizes, 'crop_top_margin', '?'), getattr(sizes, 'crop_left_margin', '?'),
             active)
