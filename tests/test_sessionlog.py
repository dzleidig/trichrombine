"""
The session diagnostic log.

Judged by one question: when a run fails at the rig, does the log answer why without
re-reading the ARWs? Every failure so far needed one of these — the library versions
(a bad numpy made scipy.ndimage return silently wrong numbers), the sensor geometry
(the camera's real crop hides in crop_top_margin, not top_margin), or the per-channel
means a frame was refused on (narrowband blue scores worse than white light on the
wrong statistic).
"""

from types import SimpleNamespace

import numpy as np
import pytest

from trichrom.lib import sessionlog
from trichrom.lib.rawio import verify_channel
from trichrom.lib.scanner import CHANNEL_BAYER_INDICES

BLACK = [512, 512, 512, 512]
WHITE = 16383
SIZE = 64
PATTERN = np.array([[0, 1], [3, 2]], dtype=np.uint8)
SITES = {'R': [(0, 0, 0)], 'G': [(1, 0, 1), (3, 1, 0)], 'B': [(2, 1, 1)]}
SIZES = SimpleNamespace(raw_height=SIZE, raw_width=SIZE, height=56, width=52,
                        top_margin=4, left_margin=6)


@pytest.fixture
def logfile(tmp_path):
    """Start a log, and always detach it — a leaked handler writes into later tests."""
    path = sessionlog.start(tmp_path, argv=['trichrom-scan', '--roll-id', 'roll01'])
    yield lambda: path.read_text()
    sessionlog.stop()


def _raw(levels):
    image = np.zeros((SIZE, SIZE), dtype=np.float32)
    for ch, sites in SITES.items():
        for idx, r, c in sites:
            image[r::2, c::2] = BLACK[idx] + levels[ch]
    return {'pattern': PATTERN, 'image': image, 'sizes': SIZES,
            'black_level_per_channel': BLACK, 'white_level': WHITE}


def test_records_the_command_that_ran(logfile):
    assert 'trichrom-scan --roll-id roll01' in logfile()


def test_records_library_versions(logfile):
    """A numpy built from source against a Python it predated made scipy.ndimage return
    silently wrong numbers, and that looked like a bug in the leader measurement until
    somebody thought to check versions. One line spares the next person that."""
    text = logfile()
    assert 'numpy' in text and 'scipy' in text
    assert 'python' in text.lower()


def test_records_the_channel_means_a_frame_was_judged_on(logfile):
    """The numbers that diagnosed both the mis-attribution guard and the saturated blue
    probe. Previously recoverable only by re-reading the ARWs afterwards."""
    verify_channel(_raw({'R': 6000.0, 'G': 400.0, 'B': 90.0}), 'R', CHANNEL_BAYER_INDICES)
    text = logfile()
    assert 'verify R' in text
    assert 'dominance' in text
    assert 'R=6000' in text and 'B=90' in text


def test_records_a_refused_frame_too(logfile):
    """A frame that fails is the one worth having numbers for."""
    with pytest.raises(ValueError):
        verify_channel(_raw({'R': 6000.0, 'G': 400.0, 'B': 90.0}), 'B', CHANNEL_BAYER_INDICES)
    assert 'reads as R' in logfile()


def test_records_the_sensor_geometry(logfile):
    """The A7R V reports top_margin=0 and keeps its real border in crop_top_margin, so
    which rectangle was used is not inferable later."""
    sizes = SimpleNamespace(raw_height=6656, raw_width=9728, height=6374, width=9566,
                            top_margin=0, left_margin=0, crop_top_margin=20,
                            crop_left_margin=32, crop_height=6336, crop_width=9504)
    sessionlog.record_geometry(sizes, (20, 32, 6336, 9504))
    text = logfile()
    assert '6374x9566' in text and '6336x9504' in text


def test_appends_rather_than_replacing(tmp_path):
    """A session dir accumulates a failed calibration and the retry after it. Both are
    evidence about the same roll, so the second must not erase the first."""
    sessionlog.start(tmp_path, argv=['trichrom-scan', 'first'])
    sessionlog.stop()
    sessionlog.start(tmp_path, argv=['trichrom-scan', 'second'])
    sessionlog.stop()
    text = (tmp_path / sessionlog.LOG_NAME).read_text()
    assert 'first' in text and 'second' in text


def test_does_not_write_to_the_terminal(logfile, capsys):
    """This is a record, not a second copy of the operator's output — print() already
    handles that, and a logger that reached stdout would double every line."""
    verify_channel(_raw({'R': 6000.0, 'G': 400.0, 'B': 90.0}), 'R', CHANNEL_BAYER_INDICES)
    captured = capsys.readouterr()
    assert captured.out == '' and captured.err == ''


def test_survives_a_missing_package(logfile, monkeypatch):
    """Version lookup must never be what ends a roll."""
    import importlib.metadata as meta
    monkeypatch.setattr(meta, 'version', lambda name: (_ for _ in ()).throw(Exception('nope')))
    sessionlog._record_environment()
    assert 'absent' in logfile()
