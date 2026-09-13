"""
Session state is what makes a roll resumable and what identifies which frames
need redoing after a mid-roll failure. It also decides whether stale calibration
gets silently trusted.
"""

import json
import time

import pytest

from trichrom.lib import session_state


@pytest.fixture(autouse=True)
def isolated_pointer(tmp_path, monkeypatch):
    """The 'most recent session' pointer lives in the user's home directory —
    keep tests off it."""
    monkeypatch.setattr(session_state, 'GLOBAL_POINTER', tmp_path / 'pointer' / 'last_session')


def test_new_session_records_roll_provenance(tmp_path):
    session_dir = tmp_path / 'roll01'
    state = session_state.new_session(session_dir, 'Portra 400', 'roll01', '2026-09-10')

    assert (session_dir / 'session.json').exists()
    assert state['film_stock'] == 'Portra 400'
    assert state['calibration'] is None
    assert state['frames'] == []


def test_state_round_trips_through_disk(tmp_path):
    session_dir = tmp_path / 'roll01'
    state = session_state.new_session(session_dir, 'HP5', 'r1')
    session_state.set_calibration(state, {'R': 200, 'G': 190, 'B': 210}, '1/60',
                                  {'R': 0.85, 'G': 0.86, 'B': 0.84})
    session_state.save_session(session_dir, state)

    reloaded = session_state.load_session(session_dir)
    assert reloaded['calibration']['channel_levels'] == {'R': 200, 'G': 190, 'B': 210}
    assert reloaded['calibration']['shutter_speed'] == '1/60'


def test_resume_defaults_to_the_most_recent_session(tmp_path):
    first = tmp_path / 'roll01'
    second = tmp_path / 'roll02'
    session_state.new_session(first, 'HP5', 'r1')
    session_state.new_session(second, 'Portra', 'r2')

    assert session_state.resolve_resume_dir() == second.resolve()
    assert session_state.resolve_resume_dir(first) == first, "explicit path wins"


def test_resume_without_a_previous_session_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        session_state.resolve_resume_dir()


def test_fresh_calibration_is_not_stale(tmp_path, capsys):
    state = session_state.new_session(tmp_path / 'r', 'HP5', 'r1')
    session_state.set_calibration(state, {}, '1/60', {})

    session_state.warn_if_stale(state)
    assert capsys.readouterr().out == ""


def test_old_calibration_warns_rather_than_being_trusted(tmp_path, capsys):
    """LED drift and physical bumps make old numbers suspect."""
    state = session_state.new_session(tmp_path / 'r', 'HP5', 'r1')
    session_state.set_calibration(state, {}, '1/60', {})
    state['calibration']['calibrated_at'] = time.time() - session_state.STALE_SECONDS - 1

    session_state.warn_if_stale(state)
    assert 'WARNING' in capsys.readouterr().out


def test_failed_frames_identifies_exactly_what_to_redo(tmp_path):
    state = session_state.new_session(tmp_path / 'r', 'HP5', 'r1')
    session_state.record_frame(state, 1, {'R': 'a'}, 'out1.tiff', 'ok')
    session_state.record_frame(state, 2, {'R': 'b'}, None, 'incomplete')
    session_state.record_frame(state, 3, {'R': 'c'}, 'out3.tiff', 'merge_failed')

    assert [f['frame'] for f in session_state.failed_frames(state)] == [2, 3]


def test_frame_numbering_continues_across_a_resume(tmp_path):
    """run_capture_loop starts at len(frames) + 1, so a resumed roll must not
    restart numbering and overwrite earlier frames."""
    session_dir = tmp_path / 'r'
    state = session_state.new_session(session_dir, 'HP5', 'r1')
    session_state.record_frame(state, 1, {}, 'out1.tiff', 'ok')
    session_state.record_frame(state, 2, {}, 'out2.tiff', 'ok')
    session_state.save_session(session_dir, state)

    assert len(session_state.load_session(session_dir)['frames']) + 1 == 3


def test_session_file_is_human_readable(tmp_path):
    """It's the provenance record that travels with the roll."""
    session_dir = tmp_path / 'r'
    session_state.new_session(session_dir, 'Portra 400', 'roll01')
    assert json.loads((session_dir / 'session.json').read_text())['film_stock'] == 'Portra 400'


def test_save_leaves_no_temp_file_behind(tmp_path):
    """The atomic write-then-rename must not leave its scratch file in the
    session folder, which is meant to hold only the roll's own artifacts."""
    session_dir = tmp_path / 'r'
    state = session_state.new_session(session_dir, 'HP5', 'r1')
    session_state.save_session(session_dir, state)
    assert list(session_dir.glob('*.tmp')) == []


def test_flat_capture_shutter_is_recorded(tmp_path):
    """Flats are shot before a scanning shutter exists, at whatever the camera happened
    to be on. That is fine — only the flat's shape is used — but the shutter decides how
    well exposed it is, so it is the first number worth having when a flat comes back
    clipped or too dark. Before this it was recorded nowhere."""
    state = session_state.new_session(tmp_path, 'Portra 400', 'roll01', None)
    session_state.set_flats(state, {'R': tmp_path / 'R.npy'}, '1/4')
    session_state.save_session(tmp_path, state)

    written = json.loads((tmp_path / 'session.json').read_text())
    assert written['flat_capture']['shutter_speed'] == '1/4'
    assert written['flat_capture']['captured_at'] > 0


def test_flat_capture_tolerates_an_unknown_shutter(tmp_path):
    """A backend that will not report the shutter costs a line of provenance, never the
    roll — so the field records None rather than the call failing."""
    state = session_state.new_session(tmp_path, '', '', None)
    session_state.set_flats(state, {'R': tmp_path / 'R.npy'}, None)
    assert state['flat_capture']['shutter_speed'] is None
