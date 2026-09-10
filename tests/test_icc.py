"""
The embedded profile is what tells Capture One our pixels are linear ProPhoto.
If it's malformed or the matrix is wrong, every merged TIFF is misinterpreted —
silently, with no error anywhere. These assertions pin the structure and check the
matrix against the published ProPhoto D50 values, which also match Elle Stone's
widely-shipped LargeRGB-elle-V4-g10.icc to within s15Fixed16 quantization.
"""

import struct

import numpy as np

from trichrom.lib.icc import build_linear_prophoto_icc

# Bruce Lindbloom's published ProPhoto RGB -> XYZ (D50) matrix.
REFERENCE_MATRIX = np.array([
    [0.7976749, 0.1351917, 0.0313534],
    [0.2880402, 0.7118741, 0.0000857],
    [0.0000000, 0.0000000, 0.8252100],
])
D50 = np.array([0.9642, 1.0, 0.8249])


def _tags(profile):
    count = struct.unpack('>I', profile[128:132])[0]
    tags, offset = {}, 132
    for _ in range(count):
        sig = profile[offset:offset + 4].decode('ascii')
        data_offset, size = struct.unpack('>II', profile[offset + 4:offset + 12])
        tags[sig] = (data_offset, size)
        offset += 12
    return tags


def _xyz(profile, tags, sig):
    offset = tags[sig][0]
    return np.array(struct.unpack('>3i', profile[offset + 8:offset + 20])) / 65536.0


def test_header_is_well_formed():
    profile = build_linear_prophoto_icc()
    assert len(profile) == struct.unpack('>I', profile[:4])[0], "size field must match actual length"
    assert profile[36:40] == b'acsp'
    assert profile[12:16] == b'mntr'
    assert profile[16:20] == b'RGB '
    assert profile[20:24] == b'XYZ '
    assert struct.unpack('>I', profile[8:12])[0] == 0x04300000, "ICC v4.3"


def test_required_tags_present_with_correct_types():
    profile = build_linear_prophoto_icc()
    tags = _tags(profile)
    expected = {
        'desc': b'mluc', 'cprt': b'mluc', 'wtpt': b'XYZ ',
        'rXYZ': b'XYZ ', 'gXYZ': b'XYZ ', 'bXYZ': b'XYZ ',
        'rTRC': b'curv', 'gTRC': b'curv', 'bTRC': b'curv',
    }
    assert set(tags) == set(expected)
    for sig, type_sig in expected.items():
        offset = tags[sig][0]
        assert profile[offset:offset + 4] == type_sig, f"{sig} has wrong type signature"


def test_tag_data_lies_inside_the_file_and_is_aligned():
    profile = build_linear_prophoto_icc()
    for sig, (offset, size) in _tags(profile).items():
        assert offset + size <= len(profile), f"{sig} data runs past end of profile"
        assert offset % 4 == 0, f"{sig} data is not 4-byte aligned"


def test_matrix_matches_published_prophoto_values():
    profile = build_linear_prophoto_icc()
    tags = _tags(profile)
    matrix = np.stack([_xyz(profile, tags, s) for s in ('rXYZ', 'gXYZ', 'bXYZ')], axis=1)
    assert np.max(np.abs(matrix - REFERENCE_MATRIX)) < 5e-4


def test_white_point_is_d50():
    profile = build_linear_prophoto_icc()
    tags = _tags(profile)
    assert np.max(np.abs(_xyz(profile, tags, 'wtpt') - D50)) < 5e-4


def test_trc_is_linear():
    """A curveType with zero entries is the ICC encoding for the identity curve —
    it's what makes the file linear rather than gamma-encoded."""
    profile = build_linear_prophoto_icc()
    tags = _tags(profile)
    for sig in ('rTRC', 'gTRC', 'bTRC'):
        offset = tags[sig][0]
        assert struct.unpack('>I', profile[offset + 8:offset + 12])[0] == 0
