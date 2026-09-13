"""
LED warm-up. The bug this guards against is the one the original code had: a
warm-up that printed and slept without ever turning the LEDs on. Nothing about
that fails visibly — you just calibrate a cold light and watch the numbers slide
across the roll — so it has to be asserted here.
"""

from types import SimpleNamespace

from trichrom.scan import CHANNEL_SLOT, warm_up


class FakeScanlight:
    """Records the (r, g, b) of every set_color, ignoring w/ir/brightness."""

    def __init__(self):
        self.colors = []

    def set_color(self, r, g, b, w, ir, brightness):
        self.colors.append((r, g, b))


def _warm(seconds=0.06, power=200, dwell=0.005):
    light = FakeScanlight()
    args = SimpleNamespace(warmup_seconds=seconds, start_power=power)
    warm_up(light, args, dwell=dwell)
    return light.colors


def test_warmup_actually_turns_the_leds_on():
    """The regression guard: a warm-up that only sleeps records nothing."""
    assert _warm(), "warm-up drove no LEDs at all"


def test_warmup_lights_every_channel():
    """Each LED's own junction has to come up to temperature, not just the board."""
    lit = {slot for color in _warm() for slot, v in enumerate(color) if v}
    assert lit == set(CHANNEL_SLOT.values())


def test_warmup_runs_one_channel_at_a_time():
    """Scanning never has two LEDs on, so warming with two on would settle the
    board hotter than it ever gets in use — calibration would then drift as it
    cools back down over the roll."""
    for color in _warm():
        assert sum(1 for v in color if v) == 1, f"{color} has more than one channel lit"


def test_warmup_uses_the_scanning_power_not_full_power():
    """Same reason: full power overshoots the thermal operating point."""
    for color in _warm(power=200):
        assert max(color) == 200


def test_warmup_stops_at_the_deadline():
    """A warm-up that overruns its budget wastes the operator's time on every roll."""
    import time
    start = time.monotonic()
    _warm(seconds=0.05, dwell=0.005)
    assert time.monotonic() - start < 0.5
