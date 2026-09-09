"""Per-channel flat-field construction and application for narrowband scanning."""

import numpy as np
from scipy.ndimage import uniform_filter

from ..legacy.combine_rgb_scans import extract_bayer_channel


def build_channel_flat(raw_images, pattern, channel_indices, black_levels, smooth_size=201):
    """
    Average several black-subtracted raw exposures of one LED channel, extract the
    matching Bayer plane(s), smooth out grain, and normalize so the peak is 1.0.

    channel_indices is (ch,) for R/B or (g0, g2) for green — planes are averaged
    together (at matching half-resolution) before the light-falloff smoothing.
    """
    planes = []
    for image in raw_images:
        chans = []
        for idx in channel_indices:
            data, _, _ = extract_bayer_channel(image, pattern, idx)
            chans.append(data.astype(np.float64) - black_levels[idx])
        planes.append(np.mean(chans, axis=0))
    averaged = np.maximum(np.mean(planes, axis=0), 0)

    flat = uniform_filter(averaged, size=smooth_size)
    peak = flat.max()
    if peak <= 0:
        raise ValueError("Flat-field frame is black — check LED brightness and exposure.")
    return (flat / peak).astype(np.float32)


def apply_flat(signal, flat):
    """Divide a normalized signal plane by its channel's flat-field map."""
    return signal / flat
