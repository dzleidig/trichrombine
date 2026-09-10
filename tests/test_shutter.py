"""Shutter-speed parsing and selection — shared by both camera backends."""

import pytest

from trichrom.lib.shutter import STANDARD_CHOICES, nearest_shutter_choice, shutter_str_to_seconds


@pytest.mark.parametrize('text,seconds', [
    ('1/8000', 1 / 8000),
    ('1/125', 1 / 125),
    ('1/60', 1 / 60),
    ('0.3', 0.3),
    ('1', 1.0),
    ('30', 30.0),
    ('  1/250  ', 1 / 250),
])
def test_parses_both_fractional_and_decimal_forms(text, seconds):
    assert shutter_str_to_seconds(text) == pytest.approx(seconds)


def test_bulb_has_no_duration():
    assert shutter_str_to_seconds('bulb') is None
    assert shutter_str_to_seconds('Bulb') is None


def test_picks_the_nearest_available_speed():
    choices = ['1/125', '1/60', '1/30']
    assert nearest_shutter_choice(choices, 1 / 60) == '1/60'
    assert nearest_shutter_choice(choices, 1 / 100) == '1/125'


def test_chooses_on_a_log_scale():
    """Shutter steps are geometric, so ratio error is what matters. Between 1/60
    and 1/30, a 1/40 target is closer to 1/30 in absolute terms but nearly
    equidistant in stops — and closer in log terms to 1/30."""
    assert nearest_shutter_choice(['1/60', '1/30'], 1 / 42) == '1/30'
    assert nearest_shutter_choice(['1/60', '1/30'], 1 / 44) == '1/60'


def test_skips_bulb_and_unparseable_entries():
    """Cameras report 'bulb' among their choices; it has no duration to compare."""
    assert nearest_shutter_choice(['bulb', '1/60'], 1 / 60) == '1/60'
    assert nearest_shutter_choice(['nonsense', '1/60'], 1 / 60) == '1/60'


def test_returns_none_when_nothing_is_selectable():
    assert nearest_shutter_choice(['bulb'], 1 / 60) is None
    assert nearest_shutter_choice([], 1 / 60) is None


def test_standard_ladder_is_usable_as_a_fallback():
    """Used when a backend can't report the camera's own list."""
    assert nearest_shutter_choice(STANDARD_CHOICES, 1 / 60) == '1/60'
    assert all(shutter_str_to_seconds(c) > 0 for c in STANDARD_CHOICES)
    durations = [shutter_str_to_seconds(c) for c in STANDARD_CHOICES]
    assert durations == sorted(durations), "ladder should run fast to slow"
