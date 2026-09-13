"""Shutter-speed parsing and selection — shared by both camera backends."""

import pytest

from trichrom.lib.shutter import (STANDARD_CHOICES, nearest_shutter_choice, parse_choice_list,
                                  shutter_str_to_seconds)


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


# The exact string Capture One returned for the A7R V at the rig — pipe-separated,
# leading "Bulb", slowest first. Splitting this on commas yields one token that
# parses as nothing, so every selection made from it comes back None; that is what
# aborted the first calibration run partway through the exposure pass.
C1_A7RV_LADDER = (
    'Bulb|30|25|20|15|13|10|8|6|5|4|3.2|2.5|2|1.6|1.3|1|0.8|0.6|0.5|0.4|1/3|1/4|1/5|'
    '1/6|1/8|1/10|1/13|1/15|1/20|1/25|1/30|1/40|1/50|1/60|1/80|1/100|1/125|1/160|'
    '1/200|1/250|1/320|1/400|1/500|1/640|1/800|1/1000|1/1250|1/1600|1/2000|1/2500|'
    '1/3200|1/4000|1/5000|1/6400|1/8000'
)


def test_splits_the_pipe_separated_ladder_capture_one_actually_returns():
    choices = parse_choice_list(C1_A7RV_LADDER)
    assert choices[:3] == ['Bulb', '30', '25']
    assert choices[-1] == '1/8000'
    assert len(choices) == 56


def test_the_real_ladder_can_be_selected_from():
    """The failure was one step downstream of the split: an unsplit ladder still
    looks like a list, and only selection notices there is nothing in it."""
    choices = parse_choice_list(C1_A7RV_LADDER)
    assert nearest_shutter_choice(choices, 1 / 25 * 8.919) == '1/3'
    assert nearest_shutter_choice(choices, 1 / 125) == '1/125'


def test_splits_a_comma_separated_ladder_too():
    assert parse_choice_list('1/125, 1/60, 1/30') == ['1/125', '1/60', '1/30']


def test_unreadable_ladder_is_rejected_rather_than_returned_as_one_token():
    assert parse_choice_list('30;15;1/60') is None
    assert parse_choice_list('') is None
    assert parse_choice_list('Bulb') is None
