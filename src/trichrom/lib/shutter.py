"""Shutter-speed parsing and selection, shared by every camera backend."""

import math
import re

# Separators a backend might hand back a shutter ladder with. Capture One returns
# its list pipe-separated ("Bulb|30|25|...|1/8000"); osascript renders an actual
# AppleScript list comma-separated. Accept either, plus newlines, rather than
# betting on one.
_CHOICE_SEPARATORS = re.compile(r'[|,\n\r]+')

# Standard 1/3-stop ladder, used when a backend can't report the camera's own list.
STANDARD_CHOICES = [
    '1/8000', '1/6400', '1/5000', '1/4000', '1/3200', '1/2500', '1/2000', '1/1600',
    '1/1250', '1/1000', '1/800', '1/640', '1/500', '1/400', '1/320', '1/250', '1/200',
    '1/160', '1/125', '1/100', '1/80', '1/60', '1/50', '1/40', '1/30', '1/25', '1/20',
    '1/15', '1/13', '1/10', '1/8', '1/6', '1/5', '1/4', '0.3', '0.4', '0.5', '0.6',
    '0.8', '1', '1.3', '1.6', '2', '2.5', '3.2', '4', '5', '6', '8', '10', '13', '15',
    '20', '25', '30',
]


def shutter_str_to_seconds(s):
    """Parse a shutter-speed string ('1/125', '0.5', '2', 'bulb') to seconds."""
    s = s.strip()
    if s.lower() == 'bulb':
        return None
    if '/' in s:
        num, den = s.split('/', 1)
        return float(num) / float(den)
    return float(s)


def parse_choice_list(raw):
    """Split a backend's shutter-ladder string into individual choices.

    Returns None if nothing in the result parses as a duration — a separator we
    don't know about yields one long unsplittable token, which looks like a valid
    single-entry list and silently poisons every selection made from it. The
    caller is expected to fall back to `STANDARD_CHOICES` on None rather than
    scan a roll against a ladder nobody can read.
    """
    choices = [c.strip() for c in _CHOICE_SEPARATORS.split(raw) if c.strip()]
    if not any(_duration_or_none(c) for c in choices):
        return None
    return choices


def _duration_or_none(choice):
    """Seconds for a choice we can both parse and select on, else None."""
    try:
        secs = shutter_str_to_seconds(choice)
    except ValueError:
        return None
    if secs is None or secs <= 0:
        return None
    return secs


def nearest_shutter_choice(choices, target_seconds):
    """Pick the choice whose duration is closest to target_seconds, on a log scale
    (shutter speeds are geometric, so ratio error is what matters, not absolute)."""
    best, best_dist = None, None
    for c in choices:
        secs = _duration_or_none(c)
        if secs is None:
            continue
        dist = abs(math.log(secs) - math.log(target_seconds))
        if best_dist is None or dist < best_dist:
            best_dist, best = dist, c
    return best
