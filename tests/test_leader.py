"""
Leader measurement decides the exposure target for a whole roll, so a regression
here means every frame is mis-exposed. The properties that matter are the ones
that buy positioning tolerance: ignore image content, ignore dust, survive grain.
"""

import numpy as np

from trichrom.lib.leader import measure_leader_level

BASE = 1000.0


def _leader(rng, grain=15.0):
    return np.full((400, 400), BASE) + rng.normal(0, grain, size=(400, 400))


def test_reads_the_base_level_through_grain():
    patch = _leader(np.random.default_rng(0))
    np.testing.assert_allclose(measure_leader_level(patch), BASE, rtol=0.02)


def test_masks_out_image_content():
    """Frame edges and image content are high-variance; film base is smooth. If
    they weren't masked, loose leader positioning would corrupt the reading."""
    rng = np.random.default_rng(1)
    patch = _leader(rng)
    patch[50:150, 50:150] += rng.normal(0, 400, size=(100, 100))

    np.testing.assert_allclose(measure_leader_level(patch), BASE, rtol=0.02)


def test_dust_and_hot_pixels_cannot_set_the_target():
    """A high percentile rather than the true max — otherwise one hot pixel
    would drive the whole roll's exposure."""
    rng = np.random.default_rng(2)
    patch = _leader(rng)
    patch[10, 10] = 60000
    patch[200, 300] = 50000

    np.testing.assert_allclose(measure_leader_level(patch), BASE, rtol=0.02)


def test_tracks_brightness_changes():
    rng = np.random.default_rng(3)
    dim = measure_leader_level(_leader(rng) * 0.5)
    bright = measure_leader_level(_leader(rng))
    assert dim < bright
    np.testing.assert_allclose(dim, BASE * 0.5, rtol=0.03)


def test_samples_only_the_central_band():
    """Sprocket holes and holder edges live outside the central band, so extreme
    values out there must not move the reading."""
    rng = np.random.default_rng(4)
    patch = _leader(rng)
    patch[:40, :] = 60000
    patch[-40:, :] = 0

    np.testing.assert_allclose(measure_leader_level(patch), BASE, rtol=0.02)
