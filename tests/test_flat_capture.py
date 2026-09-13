"""
Flat capture waits for the operator before it shoots.

`capture_flats` printed "Remove the film holder — bare light only — before continuing"
and then continued, without waiting. The flats were therefore shot *through* the holder,
so its vignetting and edges were baked into the correction divided into every frame of
the roll. The result is a flat that looks entirely plausible and is wrong, which is the
worst shape a bug can take here.

The order is the whole property: a wait that happens after the first exposure fixes
nothing.
"""

from types import SimpleNamespace

import pytest

from trichrom import scan


class _StopAfterFirstShot(Exception):
    """Ends the run at the first exposure; everything under test happens before it."""


class _FakeScanlight:
    def set_color(self, *args):
        pass


@pytest.fixture
def recorder(monkeypatch):
    """Records operator waits and exposures in the order they happen."""
    events = []
    monkeypatch.setattr('builtins.input', lambda *a: events.append('waited'))

    def fake_shot(scanlight, camera, args, ch, level, what):
        events.append(f'shot:{ch}')
        raise _StopAfterFirstShot()

    monkeypatch.setattr(scan, '_require_shot', fake_shot)
    return events


def _run(tmp_path, dry_run=False):
    args = SimpleNamespace(dry_run=dry_run, flat_brightness=180, flat_shots=6)
    with pytest.raises(_StopAfterFirstShot):
        scan.capture_flats(_FakeScanlight(), None, args, tmp_path)


def test_waits_before_the_first_exposure(tmp_path, recorder):
    """The regression guard: without the wait the first event is a shot, not a wait."""
    _run(tmp_path)
    assert recorder[0] == 'waited', f"shot before the holder came off: {recorder}"


def test_the_first_exposure_still_happens_after_waiting(tmp_path, recorder):
    """The wait must not swallow the run — the probe frame still has to fire."""
    _run(tmp_path)
    assert recorder == ['waited', 'shot:R']


def test_dry_run_does_not_block(tmp_path, recorder):
    """--dry-run has no operator, so a prompt there would hang an unattended check."""
    _run(tmp_path, dry_run=True)
    assert 'waited' not in recorder
