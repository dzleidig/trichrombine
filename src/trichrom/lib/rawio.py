"""Shared rawpy read helpers for the trichromatic (3-shot) capture pipeline."""

import numpy as np
import rawpy
from scipy.ndimage import spline_filter

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

# Because the scale factor is exactly 2, every output sample lands either exactly on
# a coefficient or exactly halfway between two — so evaluation is not a general
# resample at all, it is two fixed kernels. These are the B-spline basis sampled at
# those two positions, with the index of their first tap relative to the base:
#
#   cubic, integer  B3(-1, 0, 1)            = [1, 4, 1] / 6
#   cubic, half     B3(-1.5, -.5, .5, 1.5)  = [1, 23, 23, 1] / 48
#   linear          needs no prefilter, since its coefficients are the samples
#
# Evaluating these as separable slice arithmetic over the spline coefficients beats
# handing the job to a general evaluator by a wide margin: `RectBivariateSpline`'s fit
# alone cost more than this whole routine, and `map_coordinates` additionally wanted a
# meshgrid of two float64 arrays the size of the output — near a gigabyte at 61MP.
KERNELS = {
    1: ((np.array([1.0]), 0), (np.array([0.5, 0.5]), 0)),
    3: ((np.array([1.0, 4.0, 1.0]) / 6.0, -1),
        (np.array([1.0, 23.0, 23.0, 1.0]) / 48.0, -1)),
}


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

    Evaluation exploits the fixed 2x scale: each output sample sits either on a
    spline coefficient or exactly between two, so it is separable slice arithmetic
    with the two kernels in `KERNELS` rather than a general resampler. See there for
    why that matters — the general evaluators spend most of their time on machinery
    this mapping does not need.
    """
    # Odd reflection needs at least one real sample to mirror through per padded one.
    pad = max(0, min(INTERPOLATION_PAD, min(plane.shape) - 1))
    padded = np.pad(plane, pad, mode='reflect', reflect_type='odd') if pad else plane

    # Prefilter to B-spline coefficients. Linear needs none: its coefficients are the
    # samples, which is also why it cannot overshoot.
    coeffs = (spline_filter(padded, order=order, mode='mirror', output=np.float32)
              if order > 1 else padded.astype(np.float32, copy=False))

    h, w = out_shape
    # Narrow axis first, so the intermediate is the smaller of the two.
    coeffs = _upsample_axis(coeffs, col_offset, w, pad, order, axis=1)
    return _upsample_axis(coeffs, row_offset, h, pad, order, axis=0)


def _upsample_axis(coeffs, offset, out_len, pad, order, axis):
    """One separable pass: double `axis` from spline coefficients to output samples.

    Output index `y` reads coordinate `(y - offset) / 2 + pad`, so the parity of
    `y - offset` alone decides which of the two kernels applies. Each parity picks out
    a contiguous run of coefficients, which is why this is plain slicing rather than
    gather-indexing.
    """
    shape = list(coeffs.shape)
    shape[axis] = out_len
    out = np.empty(shape, dtype=np.float32)

    for parity, (kernel, first_tap) in enumerate(KERNELS[order]):
        # Integer phase where (y - offset) is even, half phase where it is odd.
        start = (offset + parity) % 2
        count = (out_len - start + 1) // 2
        if count <= 0:
            continue
        # Base coefficient index for the first output sample of this phase.
        base = (start - offset - parity) // 2 + pad

        acc = None
        for k, weight in enumerate(kernel):
            lo = base + first_tap + k
            chunk = coeffs[lo:lo + count] if axis == 0 else coeffs[:, lo:lo + count]
            acc = weight * chunk if acc is None else acc + weight * chunk

        if axis == 0:
            out[start::2] = acc
        else:
            out[:, start::2] = acc
    return out
