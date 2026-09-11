"""
Flat-field build. The load-bearing property: a flat must come out the same shape
as the cropped signal plane it will divide, because the sensor's active area is
smaller than the full raw. Synthetic frames here use NONZERO margins on purpose —
zero-margin frames would hide exactly the crop mismatch this guards against.
"""

from types import SimpleNamespace

import numpy as np
import pytest

from trichrom.lib.flatfield import apply_flat, build_channel_flat
from trichrom.lib.rawio import crop_half_res, extract_led_channel_plane

PATTERN = np.array([[0, 1], [3, 2]], dtype=np.uint8)  # RGGB
BLACK = [512, 512, 512, 512]
# Full raw incl. margins is larger than the active area — as on a real body.
FULL_H, FULL_W = 80, 80
TOP, LEFT, ACTIVE_H, ACTIVE_W = 8, 12, 64, 56
SIZES = SimpleNamespace(top_margin=TOP, left_margin=LEFT, height=ACTIVE_H, width=ACTIVE_W)


def _flat_frame(fill):
    img = np.zeros((FULL_H, FULL_W), dtype=np.float32)
    img[0::2, 0::2] = BLACK[0] + fill   # R
    img[0::2, 1::2] = BLACK[1] + fill   # G
    img[1::2, 0::2] = BLACK[3] + fill   # G2
    img[1::2, 1::2] = BLACK[2] + fill   # B
    return img


def test_flat_matches_the_cropped_signal_shape():
    """The regression guard: build a flat and divide a cropped signal plane by it.
    A flat built without the active-area crop would broadcast-fail here."""
    frames = [_flat_frame(4000) for _ in range(3)]
    flat = build_channel_flat(frames, PATTERN, (0,), BLACK, SIZES)

    signal = crop_half_res(extract_led_channel_plane(_flat_frame(4000), PATTERN, (0,), BLACK), SIZES)
    assert flat.shape == signal.shape == (ACTIVE_H // 2, ACTIVE_W // 2)
    apply_flat(signal, flat)  # must not raise


def test_flat_is_normalized_to_unit_peak():
    flat = build_channel_flat([_flat_frame(4000)], PATTERN, (0,), BLACK, SIZES)
    assert flat.max() == pytest.approx(1.0)
    assert (flat > 0).all()


def test_uniform_illumination_divides_out_to_unity():
    """A flat frame and a signal frame lit identically should correct to ~1.0."""
    flat = build_channel_flat([_flat_frame(4000)], PATTERN, (0,), BLACK, SIZES)
    signal = crop_half_res(extract_led_channel_plane(_flat_frame(4000), PATTERN, (0,), BLACK), SIZES)
    corrected = apply_flat(signal / 1.0, flat)  # signal already black-subtracted
    assert np.allclose(corrected / corrected.mean(), 1.0, atol=1e-6)


def test_black_frame_is_rejected():
    black_only = np.full((FULL_H, FULL_W), BLACK[0], dtype=np.float32)
    with pytest.raises(ValueError, match="black"):
        build_channel_flat([black_only], PATTERN, (0,), BLACK, SIZES)
