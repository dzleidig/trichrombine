"""Per-channel flat-field construction and application for narrowband scanning."""

import numpy as np
from scipy.ndimage import uniform_filter

from .rawio import crop_half_res, extract_led_channel_plane

# A flat is peak-normalized, so only its *shape* is used — but the exposure it was
# shot at still decides whether that shape is any good. Clipping flat-tops the
# falloff and under-corrects vignetting across the whole roll; a dark flat divides
# its own read noise into every frame. Aim here, refuse outside the bounds.
FLAT_TARGET = 0.70
FLAT_MIN = 0.15
FLAT_MAX = 0.95


def flat_level(image, pattern, channel_indices, black_levels, sizes, white_level):
    """How bright a bare-light frame is, as a fraction of usable range."""
    plane = crop_half_res(
        extract_led_channel_plane(image, pattern, channel_indices, black_levels), sizes)
    return _usable_fraction(plane, black_levels[channel_indices[0]], white_level)


def _usable_fraction(plane, black, white_level):
    return float(np.percentile(plane, 99.9)) / (white_level - black)


def build_channel_flat(raw_images, pattern, channel_indices, black_levels, sizes,
                       white_level, smooth_size=201, led_at_max=False):
    """
    Average several black-subtracted raw exposures of one LED channel, smooth out
    grain, and normalize so the peak is 1.0.

    channel_indices is (ch,) for R/B or (g0, g2) for green — the green photosite
    planes are averaged together. The result is cropped to the active sensor area
    (via the same extract + crop the signal path uses) so the flat and the frames
    it divides are the same shape; the sensor margins are nonzero on real bodies.

    Refuses a flat that is clipped or too dark rather than returning one that
    would quietly degrade every frame of the roll.

    led_at_max says the caller's probe already clamped LED power at 255, which decides
    what the too-dark message can honestly tell the operator to do: with the light
    maxed out, "turn the light up" is advice they cannot follow, and the only levers
    left are exposure ones — a slower shutter or a wider aperture. Those are free to
    use here: the flat is peak-normalized, so only its *shape* (illumination falloff
    times lens vignetting) is applied, and neither of those depends on shutter speed.
    """
    planes = [
        crop_half_res(extract_led_channel_plane(image, pattern, channel_indices, black_levels), sizes)
        for image in raw_images
    ]
    averaged = np.maximum(np.mean(planes, axis=0), 0)

    level = _usable_fraction(averaged, black_levels[channel_indices[0]], white_level)
    if level >= FLAT_MAX:
        raise ValueError(
            f"Flat is clipping ({level:.0%} of usable range) — lower --flat-brightness. "
            f"A clipped flat has a flat-topped falloff and under-corrects the whole roll.")
    if level <= FLAT_MIN:
        if led_at_max:
            remedy = ("LED power is already at its 255 maximum, so the remaining levers "
                      "are exposure: slow the shutter (--flat-shutter) or open the aperture")
        else:
            remedy = ("raise --flat-brightness; if LED power is already at its 255 maximum, "
                      "slow the shutter (--flat-shutter) or open the aperture instead")
        raise ValueError(
            f"Flat is too dark ({level:.0%} of usable range) — {remedy}. "
            f"Its read noise would be divided into every frame. The flats' shutter is "
            f"free to differ from the one scanning runs at: only the flat's shape is used.")

    flat = uniform_filter(averaged, size=smooth_size)
    peak = flat.max()
    if peak <= 0:
        raise ValueError("Flat-field frame is black — check LED brightness and exposure.")
    return (flat / peak).astype(np.float32)


def apply_flat(signal, flat):
    """Divide a normalized signal plane by its channel's flat-field map."""
    return signal / flat
