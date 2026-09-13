"""
Merge math, exercised on synthetic Bayer data so it needs no ARWs or hardware.

A mistake here is the worst kind this pipeline can make: it produces a valid file
with wrong colour and no error anywhere, which you'd only notice part-way through
inverting a roll. So these check exact expected values, not just plausibility.
"""

import json
from types import SimpleNamespace

import numpy as np
import pytest

from trichrom.lib import merge_tri

BLACK = [512, 512, 512, 512]
WHITE = 16383
SIZE = 64
PATTERN = np.array([[0, 1], [3, 2]], dtype=np.uint8)  # RGGB


def _raw(r_fill, g_fill, b_fill):
    """A synthetic RGGB frame with a flat value at each photosite position."""
    image = np.zeros((SIZE, SIZE), dtype=np.float32)
    image[0::2, 0::2] = BLACK[0] + r_fill   # R
    image[0::2, 1::2] = BLACK[1] + g_fill   # G
    image[1::2, 0::2] = BLACK[3] + g_fill   # G2
    image[1::2, 1::2] = BLACK[2] + b_fill   # B
    return {
        'pattern': PATTERN,
        'image': image,
        'sizes': SimpleNamespace(top_margin=0, left_margin=0, height=SIZE, width=SIZE),
        'black_level_per_channel': BLACK,
        'white_level': WHITE,
        'camera_whitebalance': [2.0, 1.0, 1.5, 0.0],
    }


@pytest.fixture
def patched(monkeypatch):
    """Feed merge_triplet synthetic raws and stub out the EXIF copy, which needs
    a real ARW to read from."""
    raws = {'R': _raw(8000, 100, 100), 'G': _raw(100, 9000, 100), 'B': _raw(100, 100, 7000)}
    monkeypatch.setattr(merge_tri, 'read_raw', lambda path: raws[str(path)])
    monkeypatch.setattr(merge_tri, '_preserve_exif', lambda src, dst: None)
    return raws


def _merge(tmp_path, meta=None):
    out = tmp_path / 'frame.tiff'
    peaks = merge_tri.merge_triplet('R', 'G', 'B', None, out, meta or {'roll_id': 'r1'})
    return out, peaks


def test_channels_come_from_their_own_exposure(patched, tmp_path):
    """Each plane must be read only from the matching narrowband exposure — the
    whole point of 3-shot. Getting this wrong swaps or contaminates channels."""
    import tifffile

    out, _ = _merge(tmp_path)
    data = tifffile.imread(str(out))
    assert data.shape == (SIZE // 2, SIZE // 2, 3)
    assert data.dtype == np.uint16

    usable = WHITE - BLACK[0]
    expected = np.array([8000, 9000, 7000]) / usable * 65535
    assert np.allclose(data.reshape(-1, 3).mean(axis=0), expected, atol=1.0)


def test_normalizes_by_usable_range_not_white_level(patched, tmp_path):
    """Signal is a fraction of (white - black); dividing by white_level alone
    would leave the black offset folded into every value."""
    _, peaks = _merge(tmp_path)
    assert peaks['R'] == pytest.approx(8000 / (WHITE - BLACK[0]), rel=1e-3)
    assert peaks['G'] == pytest.approx(9000 / (WHITE - BLACK[1]), rel=1e-3)
    assert peaks['B'] == pytest.approx(7000 / (WHITE - BLACK[2]), rel=1e-3)


def test_green_averages_both_green_photosites(monkeypatch, tmp_path):
    """G and G2 sit at different positions; the green plane averages them."""
    raws = {'R': _raw(1000, 0, 0), 'G': _raw(0, 0, 0), 'B': _raw(0, 0, 1000)}
    green = raws['G']
    green['image'][0::2, 1::2] = BLACK[1] + 4000   # G
    green['image'][1::2, 0::2] = BLACK[3] + 2000   # G2
    monkeypatch.setattr(merge_tri, 'read_raw', lambda path: raws[str(path)])
    monkeypatch.setattr(merge_tri, '_preserve_exif', lambda src, dst: None)

    _, peaks = _merge(tmp_path)
    assert peaks['G'] == pytest.approx(3000 / (WHITE - BLACK[1]), rel=1e-3)


def test_flat_field_division_is_applied(patched, tmp_path):
    """Halving the flat should double the recovered signal."""
    half = {ch: np.full((SIZE // 2, SIZE // 2), 0.5, dtype=np.float32) for ch in 'RGB'}
    out = tmp_path / 'flat.tiff'
    peaks = merge_tri.merge_triplet('R', 'G', 'B', half, out, {'roll_id': 'r1'})
    assert peaks['R'] == pytest.approx(2 * 8000 / (WHITE - BLACK[0]), rel=1e-3)


def test_output_carries_icc_profile_and_provenance(patched, tmp_path):
    import tifffile

    out, _ = _merge(tmp_path, meta={'roll_id': 'r1', 'film_stock': 'Portra 400', 'frame': 7})
    with tifffile.TiffFile(str(out)) as tf:
        page = tf.pages[0]
        assert 34675 in {tag.code for tag in page.tags}, "ICC profile must be embedded"
        described = json.loads(page.tags['ImageDescription'].value)

    assert described['film_stock'] == 'Portra 400'
    assert described['frame'] == 7
    # Recorded as documentation; unity white balance means it is never applied.
    assert described['camera_as_shot_wb_recorded_not_applied']['R'] == [2.0, 1.0, 1.5, 0.0]


def test_writes_json_sidecar_next_to_the_tiff(patched, tmp_path):
    out, _ = _merge(tmp_path)
    sidecar = out.with_suffix('.json')
    assert sidecar.exists()

    record = json.loads(sidecar.read_text())
    assert record['source_files'] == {'R': 'R', 'G': 'G', 'B': 'B'}
    assert set(record['peaks']) == {'R', 'G', 'B'}


def test_clips_rather_than_wrapping_on_overflow(monkeypatch, tmp_path):
    """A flat-field division can push values past full scale; uint16 wraparound
    would turn highlights black."""
    # One channel hot per exposure, as a narrowband triplet actually looks; lighting
    # all three at once is a frame the channel check rightly refuses.
    raws = {'R': _raw(16000, 100, 100), 'G': _raw(100, 16000, 100), 'B': _raw(100, 100, 16000)}
    monkeypatch.setattr(merge_tri, 'read_raw', lambda path: raws[str(path)])
    monkeypatch.setattr(merge_tri, '_preserve_exif', lambda src, dst: None)

    import tifffile
    out = tmp_path / 'clip.tiff'
    merge_tri.merge_triplet('R', 'G', 'B', None, out, {'roll_id': 'r1'})
    assert tifffile.imread(str(out)).max() == 65535


def test_merge_refuses_a_mis_attributed_triplet(monkeypatch, tmp_path):
    """End-to-end: hand merge_triplet the green exposure as its red one. Without the
    check this writes a valid TIFF with red and green exchanged and reports success."""
    swapped = {'R': _raw(100, 9000, 100), 'G': _raw(8000, 100, 100), 'B': _raw(100, 100, 7000)}
    monkeypatch.setattr(merge_tri, 'read_raw', lambda path: swapped[str(path)])
    monkeypatch.setattr(merge_tri, '_preserve_exif', lambda src, dst: None)

    out = tmp_path / 'swapped.tiff'
    with pytest.raises(ValueError, match="mis-attributed"):
        merge_tri.merge_triplet('R', 'G', 'B', None, out, {'roll_id': 'r1'})
    assert not out.exists(), "a refused frame must not leave a file behind"


def test_sidecar_records_the_channel_dominance(patched, tmp_path):
    """Headroom on the check is worth keeping: a ratio drifting toward 1 across a roll
    means the light is going wrong."""
    out, _ = _merge(tmp_path)
    record = json.loads(out.with_suffix('.json').read_text())
    assert set(record['channel_dominance']) == {'R', 'G', 'B'}
    assert record['channel_dominance']['R'] > 1.0
