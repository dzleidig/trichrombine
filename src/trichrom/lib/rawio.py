"""Shared rawpy read helpers for the trichromatic (3-shot) capture pipeline."""

import numpy as np
import rawpy
from scipy.interpolate import RectBivariateSpline

# Cubic spline for the full-resolution path. Spline interpolation passes exactly
# through its samples, so every photosite that was really read keeps its measured
# value and only the gaps between them are filled. Order 1 (bilinear) is the
# no-overshoot alternative if cubic ever rings visibly on a high-contrast edge.
INTERPOLATION_ORDER = 3

# Interpolation needs support beyond the samples it has, at both ends, and getting
# the extension wrong shows up as a band of wrong pixels down the edge of every frame.
#
# At the leading edge an unpadded cubic spline rings: the prefilter's transient starts
# at the boundary and decays inward as |z|^n with z = -0.268. At the trailing edge the
# output grid runs half a sample past the last measured site, so clamping holds the
# last value instead of continuing.
#
# Padding with odd reflection fixes both. It continues the edge gradient rather than
# flattening it, so a linear signal extends exactly and the spline sees no kink to ring
# from — unlike edge replication, which pads with a constant and makes the spline bend
# right where the real data starts.
INTERPOLATION_PAD = 12

# Output rows evaluated per pass. The spline is fitted once and evaluated in stripes
# purely to bound memory: a whole 61MP plane comes back from the evaluator as float64
# (~480MB) before it is narrowed to float32, and three channels are live at once.
EVAL_ROW_BLOCK = 512


def read_raw(path):
    with rawpy.imread(str(path)) as raw:
        return {
            'pattern': raw.raw_pattern.copy(),
            'image': raw.raw_image.copy().astype(np.float32),
            'sizes': raw.sizes,
            'black_level_per_channel': list(raw.black_level_per_channel),
            'white_level': raw.white_level,
            # Recorded as provenance only — never applied. See merge_tri.
            'camera_whitebalance': [float(v) for v in raw.camera_whitebalance],
        }


def crop_half_res(plane, sizes):
    """Crop a half-resolution Bayer-channel plane to the active sensor area."""
    top, left = sizes.top_margin // 2, sizes.left_margin // 2
    h, w = sizes.height // 2, sizes.width // 2
    return plane[top:top + h, left:left + w]


def extract_bayer_channel(raw_image, bayer_pattern, channel_index):
    """
    Extract the Bayer photosites for a given channel index.
    channel_index: 0=R, 1=G, 2=B, 3=G2 (second green in RGGB)
    Returns (data, row_offset, col_offset)
    """
    positions = np.argwhere(bayer_pattern == channel_index)
    if len(positions) == 0:
        raise ValueError(f"Channel index {channel_index} not found in Bayer pattern")
    row, col = positions[0]
    return raw_image[row::2, col::2], row, col


def extract_led_channel_plane(image, pattern, channel_indices, black_per_channel):
    """Black-subtracted plane for one LED channel, averaging G+G2 for green."""
    planes = [
        extract_bayer_channel(image, pattern, idx)[0].astype(np.float64) - black_per_channel[idx]
        for idx in channel_indices
    ]
    return np.mean(planes, axis=0)


def active_site_offsets(row_offset, col_offset, sizes):
    """
    Where a sub-plane's samples sit relative to the active area's top-left corner.

    `crop_half_res` cuts at `margin // 2`, which lands half a cell early when a
    margin is odd — the Bayer phase inside the active area then shifts by one
    photosite. Folding `margin % 2` into the offset makes the mapping correct for
    either parity instead of silently misregistering the channels on a body whose
    margins aren't even.
    """
    return row_offset - (sizes.top_margin % 2), col_offset - (sizes.left_margin % 2)


def upsample_bayer_plane(plane, row_offset, col_offset, out_shape, order=INTERPOLATION_ORDER):
    """
    Interpolate one cropped Bayer sub-plane up to full active-area resolution,
    placing its samples at the positions they were actually read from.

    `plane[i, j]` came from active-area pixel `(2i + row_offset, 2j + col_offset)`,
    so output pixel `(y, x)` reads the sub-plane at
    `((y - row_offset) / 2, (x - col_offset) / 2)`.

    Carrying those offsets is the whole point. R, G, G2 and B sit at four different
    corners of the 2x2 cell; interpolating each as though it started at (0, 0) would
    leave the finished channels shifted against each other by half an output pixel
    diagonally — chromatic fringing baked into every frame, with nothing to show for
    it in any error message.

    Evaluated as a tensor-product spline rather than through `map_coordinates`,
    because the mapping is separable — every output row wants the same column
    coordinates. Handing a regular grid to a scattered-point evaluator costs a
    meshgrid of two float64 arrays the size of the output (near a gigabyte at 61MP)
    and runs about seven times slower for identical results.
    """
    # Odd reflection needs at least one real sample to mirror through per padded one.
    pad = max(0, min(INTERPOLATION_PAD, min(plane.shape) - 1))
    padded = np.pad(plane, pad, mode='reflect', reflect_type='odd') if pad else plane

    h, w = out_shape
    rows = (np.arange(h, dtype=np.float64) - row_offset) / 2.0 + pad
    cols = (np.arange(w, dtype=np.float64) - col_offset) / 2.0 + pad

    spline = RectBivariateSpline(
        np.arange(padded.shape[0]), np.arange(padded.shape[1]), padded,
        kx=min(order, padded.shape[0] - 1), ky=min(order, padded.shape[1] - 1), s=0)

    out = np.empty((h, w), dtype=np.float32)
    for start in range(0, h, EVAL_ROW_BLOCK):
        stop = min(start + EVAL_ROW_BLOCK, h)
        out[start:stop] = spline(rows[start:stop], cols, grid=True)
    return out
