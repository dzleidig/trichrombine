"""Film-base ('leader') density measurement, robust to loose positioning and grain."""

import numpy as np
from scipy.ndimage import gaussian_filter, uniform_filter


def measure_leader_level(patch, band_frac=0.6, variance_size=15, variance_percentile=50,
                          blur_sigma=2.5, percentile=99.0):
    """
    Estimate the film-base signal level in a black-level-subtracted channel patch.

    Samples a central band, masks out high-variance regions (image content, frame
    edges, dust — base is smooth), blurs lightly to average out grain, then takes a
    high percentile so isolated hot pixels or dust specks can't set the target.
    """
    h, w = patch.shape
    bh, bw = max(1, int(h * band_frac)), max(1, int(w * band_frac))
    top, left = (h - bh) // 2, (w - bw) // 2
    band = patch[top:top + bh, left:left + bw].astype(np.float64)

    mean = uniform_filter(band, size=variance_size)
    mean_sq = uniform_filter(band * band, size=variance_size)
    variance = np.maximum(mean_sq - mean * mean, 0)

    threshold = np.percentile(variance, variance_percentile)
    mask = variance <= threshold

    blurred = gaussian_filter(band, sigma=blur_sigma)
    values = blurred[mask]
    if values.size == 0:
        values = blurred.ravel()
    return float(np.percentile(values, percentile))
