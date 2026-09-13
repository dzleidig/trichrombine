"""
Session state persistence for a scanning session: calibration, flat-field paths,
and per-frame merge progress. The state file lives in the session folder itself so
provenance travels with the roll if it's moved or archived. A small separate global
pointer records the most recently used session dir so --resume can default sensibly
without typing paths — losing that pointer costs convenience, not data.
"""

import json
import os
import time
from pathlib import Path

GLOBAL_POINTER = Path.home() / '.trichrom' / 'last_session'
STALE_SECONDS = 4 * 3600


def _session_file(session_dir):
    return Path(session_dir) / 'session.json'


def new_session(session_dir, film_stock, roll_id, date=None):
    session_dir = Path(session_dir)
    session_dir.mkdir(parents=True, exist_ok=True)
    state = {
        'created_at': time.time(),
        'film_stock': film_stock,
        'roll_id': roll_id,
        'date': date or time.strftime('%Y-%m-%d'),
        'calibration': None,
        'flats': None,
        'flat_capture': None,
        'frames': [],
    }
    save_session(session_dir, state)
    _set_last_session(session_dir)
    return state


def load_session(session_dir):
    with open(_session_file(session_dir)) as f:
        return json.load(f)


def save_session(session_dir, state):
    # Write-then-rename so an interrupted write can't truncate the existing
    # session file: this is rewritten after every frame and is the roll's only
    # record of which frames succeeded, so a half-written file would lose exactly
    # the provenance it exists to preserve.
    target = _session_file(session_dir)
    tmp = target.with_suffix('.json.tmp')
    with open(tmp, 'w') as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, target)
    _set_last_session(session_dir)


def _set_last_session(session_dir):
    GLOBAL_POINTER.parent.mkdir(parents=True, exist_ok=True)
    GLOBAL_POINTER.write_text(str(Path(session_dir).resolve()))


def resolve_resume_dir(explicit_dir=None):
    """Return the session directory to resume: the explicit path, or the last-used one."""
    if explicit_dir:
        return Path(explicit_dir)
    if not GLOBAL_POINTER.exists():
        raise FileNotFoundError("No previous session recorded. Pass --session-dir explicitly.")
    return Path(GLOBAL_POINTER.read_text().strip())


def calibration_age_seconds(state):
    calibration = state.get('calibration')
    if not calibration:
        return None
    return time.time() - calibration['calibrated_at']


def warn_if_stale(state):
    age = calibration_age_seconds(state)
    if age is not None and age > STALE_SECONDS:
        print(
            f"WARNING: calibration is {age / 3600:.1f} hours old. LED drift or a physical "
            f"bump may make it stale — consider recalibrating (see --recalibrate)."
        )


def set_calibration(state, channel_levels, shutter_speed, peaks):
    state['calibration'] = {
        'calibrated_at': time.time(),
        'channel_levels': channel_levels,
        'shutter_speed': shutter_speed,
        'peaks': peaks,
    }


def set_flats(state, flat_paths, shutter_speed=None):
    """
    Record the flat-field maps and the exposure they were shot at.

    The shutter speed is provenance, not a setting anything reads back: flats are
    captured before the exposure pass has chosen a shutter, so they are shot at
    whatever the camera happened to be on (or at --flat-shutter). That is legitimate —
    a flat is peak-normalized, so only its *shape* is used, and shape is illumination
    falloff times lens vignetting, neither of which depends on shutter speed. But the
    shutter does decide how well exposed the flat is, so when a flat turns out clipped
    or too dark it is the first number worth knowing, and without this it was nowhere.
    """
    state['flats'] = {ch: str(p) for ch, p in flat_paths.items()}
    state['flat_capture'] = {
        'captured_at': time.time(),
        'shutter_speed': shutter_speed,
    }


def record_frame(state, frame_num, filenames, output_path, status):
    state['frames'].append({
        'frame': frame_num,
        'filenames': filenames,
        'output': str(output_path) if output_path else None,
        'status': status,
        'at': time.time(),
    })


def failed_frames(state):
    """Frames whose merge did not succeed — what a mid-roll retry needs to redo."""
    return [f for f in state['frames'] if f['status'] != 'ok']
