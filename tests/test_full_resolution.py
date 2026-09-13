"""
Full-resolution merge: interpolating each channel from its own measured sites.

Two properties carry the weight here, and both fail silently if broken.

Measured sites must survive interpolation untouched — if reconstruction perturbs
the values that were really read, every pixel in the file is suspect, not just the
inferred ones.

The channels must stay registered. R, G, G2 and B sit at four different corners of
the 2x2 cell. Interpolating each as though it began at (0, 0) shifts the finished
channels half an output pixel against each other, which produces colour fringing on
every edge of every frame and raises no error at all.
"""

from types import SimpleNamespace

import numpy as np

from trichrom.lib import merge_tri

BLACK = [512, 512, 512, 512]
WHITE = 16383
PATTERN = np.array([[0, 1], [3, 2]], dtype=np.uint8)  # RGGB
# Nonzero margins, as on a real body — zero margins would hide crop/phase errors.
TOP, LEFT, ACTIVE_H, ACTIVE_W = 4, 6, 40, 32
FULL_H, FULL_W = 56, 56
SIZES = SimpleNamespace(top_margin=TOP, left_margin=LEFT, height=ACTIVE_H, width=ACTIVE_W)
USABLE = WHITE - BLACK[0]

# (bayer index, row offset, col offset) for RGGB.
SITES = {'R': [(0, 0, 0)], 'G': [(1, 0, 1), (3, 1, 0)], 'B': [(2, 1, 1)]}


def _raw_from_field(field):
    """A synthetic frame where every photosite samples the same underlying field,
    so the correct reconstruction of each channel is that field itself."""
    image = np.zeros((FULL_H, FULL_W), dtype=np.float32)
    for idx, r, c in [s for sites in SITES.values() for s in sites]:
        image[r::2, c::2] = BLACK[idx] + field[r::2, c::2]
    return {
        'pattern': PATTERN,
        'image': image,
        'sizes': SIZES,
        'black_level_per_channel': BLACK,
        'white_level': WHITE,
        'camera_whitebalance': [1.0, 1.0, 1.0, 0.0],
    }


def _ramp():
    """A separable linear ramp. Linear data is reproduced exactly by the
    interpolator, so any channel-to-channel disagreement is a registration bug
    rather than interpolation error."""
    y, x = np.mgrid[0:FULL_H, 0:FULL_W]
    return (100.0 * y + 50.0 * x).astype(np.float32)


def _curved():
    """A field with curvature in both axes.

    The ramp above cannot catch everything: a linear signal is invariant under the
    *approximating* form of the B-spline filter, `[1, 4, 1] / 6`, so a reconstruction
    that skipped the prefilter entirely would reproduce a ramp perfectly and look
    correct. Only curvature separates interpolation from approximation.
    """
    y, x = np.mgrid[0:FULL_H, 0:FULL_W]
    return (2000.0 + 1500.0 * np.sin(y / 7.0) * np.cos(x / 5.0)).astype(np.float32)


def _triplet_raws(lit=6000.0, dark=60.0):
    """Three exposures as a real narrowband triplet looks: in each, only that channel's
    photosites carry signal.

    The uniform-field fixtures above deliberately light every site equally, which is
    what makes them useful for checking reconstruction — but it is not a frame the
    channel check would ever accept, so anything going through `merge_triplet` needs
    this instead.
    """
    def one(ch):
        image = np.zeros((FULL_H, FULL_W), dtype=np.float32)
        for name, sites in SITES.items():
            for idx, r, c in sites:
                image[r::2, c::2] = BLACK[idx] + (lit if name == ch else dark)
        return {
            'pattern': PATTERN,
            'image': image,
            'sizes': SIZES,
            'black_level_per_channel': BLACK,
            'white_level': WHITE,
            'camera_whitebalance': [1.0, 1.0, 1.0, 0.0],
        }
    return {ch: one(ch) for ch in 'RGB'}


def _fields(raw, full_resolution=True):
    """_channel_field returns (plane, measured peak); these tests are about the plane."""
    return {ch: merge_tri._channel_field(raw, ch, None, full_resolution)[0] for ch in 'RGB'}


def test_full_resolution_output_matches_the_active_area():
    out = _fields(_raw_from_field(_ramp()))
    for ch, plane in out.items():
        assert plane.shape == (ACTIVE_H, ACTIVE_W), ch


def test_half_resolution_still_halves():
    """The no-interpolation path must be unaffected by any of this."""
    out = _fields(_raw_from_field(_ramp()), full_resolution=False)
    for ch, plane in out.items():
        assert plane.shape == (ACTIVE_H // 2, ACTIVE_W // 2), ch


def test_measured_sites_keep_their_measured_values():
    """The regression guard: reconstruction fills gaps, it does not rewrite data."""
    field = _ramp()
    out = _fields(_raw_from_field(field))
    expected = field[TOP:TOP + ACTIVE_H, LEFT:LEFT + ACTIVE_W] / USABLE

    for ch in 'RGB':
        for _, r, c in SITES[ch]:
            # Active-area coordinates of this channel's real photosites.
            ys = np.arange((r - TOP) % 2, ACTIVE_H, 2)
            xs = np.arange((c - LEFT) % 2, ACTIVE_W, 2)
            got = out[ch][np.ix_(ys, xs)]
            want = expected[np.ix_(ys, xs)]
            assert np.allclose(got, want, atol=1e-5), f"{ch} sites at offset ({r},{c}) altered"


def test_channels_land_registered_on_the_same_grid():
    """All three channels sample one identical field, so after reconstruction they
    must agree. Dropping the per-site offsets shifts them against each other."""
    out = _fields(_raw_from_field(_ramp()))
    assert np.allclose(out['R'], out['G'], atol=1e-4)
    assert np.allclose(out['R'], out['B'], atol=1e-4)


def test_reconstruction_recovers_the_underlying_field():
    """Not just self-consistent between channels — actually right."""
    field = _ramp()
    out = _fields(_raw_from_field(field))
    expected = field[TOP:TOP + ACTIVE_H, LEFT:LEFT + ACTIVE_W] / USABLE
    for ch in 'RGB':
        assert np.allclose(out[ch], expected, atol=1e-4), ch


def test_uniform_field_stays_uniform():
    """Flat input must not acquire ringing or edge artifacts — those would show as
    a bright or dark border on every frame."""
    flat_field = np.full((FULL_H, FULL_W), 0.5 * USABLE, dtype=np.float32)
    out = _fields(_raw_from_field(flat_field))
    for ch, plane in out.items():
        assert np.allclose(plane, 0.5, atol=1e-5), ch


def test_full_resolution_merge_writes_a_full_size_tiff(monkeypatch, tmp_path):
    import tifffile

    raws = _triplet_raws()
    monkeypatch.setattr(merge_tri, 'read_raw', lambda path: raws[str(path)])
    monkeypatch.setattr(merge_tri, '_preserve_exif', lambda src, dst: None)

    out = tmp_path / 'full.tiff'
    merge_tri.merge_triplet('R', 'G', 'B', None, out, {'roll_id': 'r1'}, full_resolution=True)
    assert tifffile.imread(str(out)).shape == (ACTIVE_H, ACTIVE_W, 3)


def test_sidecar_records_whether_pixels_were_measured_or_inferred(monkeypatch, tmp_path):
    """Provenance: a full-resolution file must not be mistakable for a measured one."""
    import json

    raws = _triplet_raws()
    monkeypatch.setattr(merge_tri, 'read_raw', lambda path: raws[str(path)])
    monkeypatch.setattr(merge_tri, '_preserve_exif', lambda src, dst: None)

    for full, expected in ((True, 'full'), (False, 'half')):
        out = tmp_path / f'{expected}.tiff'
        merge_tri.merge_triplet('R', 'G', 'B', None, out, {'roll_id': 'r1'}, full_resolution=full)
        record = json.loads(out.with_suffix('.json').read_text())
        assert record['output_resolution'] == expected
        assert ('cubic' in record['interpolation']) == full


def test_flats_from_an_existing_session_still_apply():
    """Flats are stored at half resolution; the full-resolution path must keep
    using them as-is rather than demanding they be rebuilt."""
    raw = _raw_from_field(np.full((FULL_H, FULL_W), 0.4 * USABLE, dtype=np.float32))
    half_flat = np.full((ACTIVE_H // 2, ACTIVE_W // 2), 0.5, dtype=np.float32)
    plane, _ = merge_tri._channel_field(raw, 'R', half_flat, full_resolution=True)
    assert plane.shape == (ACTIVE_H, ACTIVE_W)
    assert np.allclose(plane, 0.8, atol=1e-5)  # 0.4 / 0.5


def test_measured_sites_survive_a_curved_field():
    """Interpolation must pass through its samples even where the signal bends —
    the property a skipped spline prefilter would quietly lose."""
    field = _curved()
    out = _fields(_raw_from_field(field))
    expected = field[TOP:TOP + ACTIVE_H, LEFT:LEFT + ACTIVE_W] / USABLE

    for ch in 'RB':  # single-sub-plane channels: the site value is the whole value
        (_, r, c), = SITES[ch]
        ys = np.arange((r - TOP) % 2, ACTIVE_H, 2)
        xs = np.arange((c - LEFT) % 2, ACTIVE_W, 2)
        got, want = out[ch][np.ix_(ys, xs)], expected[np.ix_(ys, xs)]
        assert np.allclose(got, want, atol=1e-5), f"{ch}: measured sites not preserved"
