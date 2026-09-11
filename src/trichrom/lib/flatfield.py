"""Per-channel flat-field construction and application for narrowband scanning."""

import numpy as np
from scipy.ndimage import uniform_filter

from .rawio import crop_half_res, extract_led_channel_plane


def build_channel_flat(raw_images, pattern, channel_indices, black_levels, sizes, smooth_size=201):
    """
    Average several black-subtracted raw exposures of one LED channel, smooth out
    grain, and normalize so the peak is 1.0.

    channel_indices is (ch,) for R/B or (g0, g2) for green — the green photosite
    planes are averaged together. The result is cropped to the active sensor area
    (via the same extract + crop the signal path uses) so the flat and the frames
    it divides are the same shape; the sensor margins are nonzero on real bodies.
    """
    planes = [
        crop_half_res(extract_led_channel_plane(image, pattern, channel_indices, black_levels), sizes)
        for image in raw_images
    ]
    averaged = np.maximum(np.mean(planes, axis=0), 0)

    flat = uniform_filter(averaged, size=smooth_size)
    peak = flat.max()
    if peak <= 0:
        raise ValueError("Flat-field frame is black — check LED brightness and exposure.")
    return (flat / peak).astype(np.float32)


def apply_flat(signal, flat):
    """Divide a normalized signal plane by its channel's flat-field map."""
    return signal / flat
