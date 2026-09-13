"""
Reading back which LED actually lit each frame.

This guards the one assumption the whole pipeline rests on and never checks: that the
file which landed after the red trigger really is the red exposure. Attribution comes
from `wait_for_new_file()` returning the right file per trigger, which `wait_for_settle()`
makes likely and cannot guarantee. Get it wrong and the merge produces a valid, sharp,
correctly-exposed TIFF with two channels exchanged — nothing raises, and you find out
part-way through inverting a roll.
"""

import numpy as np
import pytest

from trichrom.lib.rawio import MIN_DOMINANCE, channel_means, verify_channel
from trichrom.lib.scanner import CHANNEL_BAYER_INDICES

BLACK = [512, 512, 512, 512]
WHITE = 16383
SIZE = 64
PATTERN = np.array([[0, 1], [3, 2]], dtype=np.uint8)  # RGGB
SITES = {'R': [(0, 0, 0)], 'G': [(1, 0, 1), (3, 1, 0)], 'B': [(2, 1, 1)]}


def _raw(levels):
    """A frame with a given black-subtracted level per LED channel."""
    image = np.zeros((SIZE, SIZE), dtype=np.float32)
    for ch, sites in SITES.items():
        for idx, r, c in sites:
            image[r::2, c::2] = BLACK[idx] + levels[ch]
    return {'pattern': PATTERN, 'image': image,
            'black_level_per_channel': BLACK, 'white_level': WHITE}


def _lit(ch, level=6000.0, leak=80.0):
    return _raw({c: (level if c == ch else leak) for c in 'RGB'})


def test_identifies_the_lit_channel():
    for ch in 'RGB':
        assert verify_channel(_lit(ch), ch, CHANNEL_BAYER_INDICES) > MIN_DOMINANCE


def test_green_is_read_from_both_green_photosites():
    """`CHANNEL_BAYER_INDICES` maps green to G *and* G2, and the check must honour that.

    Driven deliberately far apart, because a frame with the two greens equal cannot
    distinguish a reader of both from a reader of one — which is exactly how an earlier
    version of this test let that mutation through.

    Red is parked between the two greens: averaging G and G2 puts green far above it
    (ratio 8, comfortably narrowband), while reading G alone drops green below red and
    the frame is rejected as mis-attributed. Red has to stay low enough not to spoil
    dominance, which the first attempt at this fixture got wrong.
    """
    image = np.zeros((SIZE, SIZE), dtype=np.float32)
    image[0::2, 0::2] = BLACK[0] + 500.0    # R, between the two greens
    image[0::2, 1::2] = BLACK[1] + 100.0    # G  low
    image[1::2, 0::2] = BLACK[3] + 8000.0   # G2 high  -> true green mean 4050
    image[1::2, 1::2] = BLACK[2] + 100.0    # B
    raw = {'pattern': PATTERN, 'image': image,
           'black_level_per_channel': BLACK, 'white_level': WHITE}

    means = channel_means(image, PATTERN, BLACK, CHANNEL_BAYER_INDICES)
    assert means['G'] == pytest.approx(4050.0, rel=1e-3), "green must average G and G2"
    assert verify_channel(raw, 'G', CHANNEL_BAYER_INDICES)


def test_rejects_a_swapped_channel():
    """The regression this exists for: a green frame handed over as the red one."""
    with pytest.raises(ValueError, match="reads as G"):
        verify_channel(_lit('G'), 'R', CHANNEL_BAYER_INDICES)


def test_rejects_every_mis_pairing():
    for shot in 'RGB':
        for claimed in 'RGB':
            if shot == claimed:
                continue
            with pytest.raises(ValueError):
                verify_channel(_lit(shot), claimed, CHANNEL_BAYER_INDICES)


def test_rejects_a_frame_shot_under_preview_light():
    """Between frames the light sits at a white-equivalent level. Such a frame has no
    dominant channel, so its argmax is meaningless — the message must say that rather
    than name a channel at random."""
    white = _raw({'R': 900.0, 'G': 900.0, 'B': 900.0})
    with pytest.raises(ValueError, match="not narrowband-lit"):
        verify_channel(white, 'R', CHANNEL_BAYER_INDICES)


def test_rejects_a_black_frame():
    """An LED that never fired. Reported as black rather than as a channel mismatch,
    since at zero signal the argmax is decided by noise."""
    with pytest.raises(ValueError, match="black"):
        verify_channel(_raw({'R': 0.0, 'G': 0.0, 'B': 0.0}), 'R', CHANNEL_BAYER_INDICES)


def test_accepts_a_dim_but_clearly_lit_frame():
    """Dominance is about which LED fired, not whether exposure was good — an
    underexposed frame is a different complaint and must not trip this one."""
    assert verify_channel(_lit('B', level=300.0, leak=4.0), 'B', CHANNEL_BAYER_INDICES)


def test_subsampling_does_not_change_the_verdict():
    """The check subsamples for speed; that must not alter which channel it names."""
    raw = _lit('G')
    full = channel_means(raw['image'], PATTERN, BLACK, CHANNEL_BAYER_INDICES, stride=1)
    cheap = channel_means(raw['image'], PATTERN, BLACK, CHANNEL_BAYER_INDICES, stride=8)
    assert max(full, key=full.get) == max(cheap, key=cheap.get) == 'G'
