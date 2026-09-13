"""
Choosing the active sensor area.

LibRaw parses the camera's inset crop from maker metadata but never applies it —
`postprocess()` hands back the larger visible area — so a pipeline that crops by
`top_margin` + `height`/`width` keeps a border strip every other converter trims. On the
A7R V that is a 20-row, 32-column edge: 6374x9566 emitted where the camera intends
6336x9504.

The inset crop is specified relative to the *raw* image, the same basis as `top_margin`,
and LibRaw guarantees `ctop + cheight <= raw_height`. Values failing that bound, or
carrying the 0xffff "not set" sentinel, are not trustworthy.

Real A7R V geometry is used below rather than invented numbers.
"""

from types import SimpleNamespace

import numpy as np

from trichrom.lib.rawio import CROP_UNSET, active_area, active_site_offsets, crop_half_res


def _sizes(**over):
    """A7R V geometry by default."""
    base = dict(raw_height=6656, raw_width=9728, height=6374, width=9566,
                top_margin=0, left_margin=0,
                crop_top_margin=20, crop_left_margin=32,
                crop_height=6336, crop_width=9504)
    base.update(over)
    return SimpleNamespace(**base)


def test_prefers_the_cameras_inset_crop():
    assert active_area(_sizes()) == (20, 32, 6336, 9504)


def test_crops_to_the_dimensions_the_camera_intends():
    plane = np.zeros((6656 // 2, 9728 // 2), dtype=np.float32)
    assert crop_half_res(plane, _sizes()).shape == (3168, 4752)


def test_falls_back_when_no_inset_crop_is_recorded():
    """0xffff is LibRaw's 'not present'. Plenty of files carry no crop at all."""
    s = _sizes(crop_top_margin=CROP_UNSET, crop_left_margin=CROP_UNSET,
               crop_height=CROP_UNSET, crop_width=CROP_UNSET)
    assert active_area(s) == (0, 0, 6374, 9566)


def test_falls_back_when_the_crop_does_not_fit_the_raw():
    """Out of bounds means the values are not what we think they are — better the
    visible area than a crop running off the end of the array."""
    s = _sizes(crop_top_margin=400, crop_height=6600)      # 7000 > raw_height 6656
    assert active_area(s) == (0, 0, 6374, 9566)


def test_falls_back_when_rawpy_predates_the_crop_fields():
    """Older rawpy has no crop_* attributes at all."""
    s = SimpleNamespace(raw_height=6656, raw_width=9728, height=6374, width=9566,
                        top_margin=0, left_margin=0)
    assert active_area(s) == (0, 0, 6374, 9566)


def test_site_offsets_follow_the_crop_origin():
    """The Bayer phase must be computed against whichever origin the crop used. An odd
    inset margin shifts it; taking the phase from top_margin instead would misregister
    the channels against each other with nothing raised."""
    even = _sizes(crop_top_margin=20, crop_left_margin=32)
    odd = _sizes(crop_top_margin=21, crop_left_margin=33)
    assert active_site_offsets(0, 0, even) == (0, 0)
    assert active_site_offsets(0, 0, odd) == (-1, -1)


def test_a_crop_at_the_origin_is_still_honoured():
    """crop_* of 0 is a legitimate 'crop is the whole visible area', not 'unset'."""
    s = _sizes(crop_top_margin=0, crop_left_margin=0, crop_height=6374, crop_width=9566)
    assert active_area(s) == (0, 0, 6374, 9566)
