"""Shutter-speed parsing and selection, shared by every camera backend."""

import math

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


def nearest_shutter_choice(choices, target_seconds):
    """Pick the choice whose duration is closest to target_seconds, on a log scale
    (shutter speeds are geometric, so ratio error is what matters, not absolute)."""
    best, best_dist = None, None
    for c in choices:
        try:
            secs = shutter_str_to_seconds(c)
        except ValueError:
            continue
        if secs is None or secs <= 0:
            continue
        dist = abs(math.log(secs) - math.log(target_seconds))
        if best_dist is None or dist < best_dist:
            best_dist, best = dist, c
    return best
