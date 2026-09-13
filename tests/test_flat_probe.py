"""
Choosing the LED power for flat-field capture.

The probe scales power by a direct ratio, which is only valid on an *unclipped*
reading. A saturated frame reports 100% of usable range however far past full scale it
truly is, so scaling from it always under-corrects — and quietly, since the number
looks reasonable.

The constants here are measured from a real A7R V + Big ScanLight run rather than
invented: red read 21.0% at power 180 and 29.2% at 255, green read 96.1% at power 126.
Green's first probe at power 180 came back 99.93% clipped, reported 100%, and the
single-shot version chose power 126 — which produced a 96% flat and aborted the roll.
"""

from types import SimpleNamespace

import pytest

from trichrom import scan
from trichrom.lib.flatfield import FLAT_MAX, FLAT_MIN

# Level per unit of LED power, from the unclipped real frames.
RESPONSE = {'R': 0.210 / 180, 'G': 0.961 / 126}


@pytest.fixture
def rig(monkeypatch):
    """A fake rig with the real sensor response, recording the powers it is asked for."""
    state = SimpleNamespace(powers=[], channel='G', response=RESPONSE)

    def fake_shot(scanlight, camera, args, ch, power, what):
        state.powers.append(power)
        return f'probe-{power}'

    def fake_level(*args):
        true = state.response[state.channel] * state.powers[-1]
        return min(true, 1.0)          # the sensor pins at full scale; so does flat_level

    monkeypatch.setattr(scan, '_require_shot', fake_shot)
    monkeypatch.setattr(scan, '_read_verified', lambda path, ch, what: {
        'image': None, 'pattern': None, 'black_level_per_channel': None,
        'sizes': None, 'white_level': None})
    monkeypatch.setattr(scan, 'flat_level', fake_level)
    return state


def _probe(rig, ch, start=180):
    """Returns (power chosen, level the flat will land at, whether the LED clamped)."""
    rig.channel = ch
    args = SimpleNamespace(dry_run=False, flat_brightness=start, flat_shutter=None)
    chosen, led_at_max = scan._probe_flat_power(None, None, args, ch)
    return chosen, rig.response[ch] * chosen, led_at_max


def test_backs_off_from_a_saturated_probe(rig):
    """The regression: green saturates at the starting power, so one reading is not
    enough to compute from."""
    chosen, landed, _ = _probe(rig, 'G')
    assert len(rig.powers) > 1, "accepted a saturated reading without re-probing"
    assert FLAT_MIN < landed < FLAT_MAX, f"flat would land at {landed:.0%} and be rejected"


def test_the_saturated_case_lands_near_target(rig):
    """Not merely legal — actually near FLAT_TARGET, which is the point of probing."""
    _, landed, _ = _probe(rig, 'G')
    assert landed == pytest.approx(scan.FLAT_TARGET, abs=0.05)


def test_an_unclipped_probe_costs_only_one_frame(rig):
    """Red is far from saturation, so there is nothing to back off from."""
    _probe(rig, 'R')
    assert rig.powers == [180]


def test_clamps_at_maximum_power_without_failing(rig):
    """Red cannot reach target at any available power — it needs ~600. That must clamp
    to 255 and carry on, not abort: a dim flat is still usable, and the operator's
    remaining levers are aperture and shutter."""
    chosen, landed, at_max = _probe(rig, 'R')
    assert chosen == 255
    assert landed > FLAT_MIN, "clamped flat must still clear the too-dark guard"
    assert at_max, "the caller needs to know the LED clamped, or its advice is unfollowable"


def test_gives_up_rather_than_probing_forever(rig, monkeypatch):
    """A rig saturated at every power must stop with a clear message."""
    rig.response = {'B': 10.0}          # saturated even at power 1
    with pytest.raises(SystemExit, match="saturated"):
        _probe(rig, 'B')
    assert len(rig.powers) <= scan.MAX_FLAT_PROBES


def test_black_probe_exits(rig):
    rig.response = {'B': 0.0}
    with pytest.raises(SystemExit, match="black"):
        _probe(rig, 'B')


def test_dry_run_returns_without_reading_anything(rig):
    args = SimpleNamespace(dry_run=True, flat_brightness=180, flat_shutter=None)
    assert scan._probe_flat_power(None, None, args, 'G') == (180, False)
