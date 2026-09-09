"""Shared rawpy read helpers for the trichromatic (3-shot) capture pipeline."""

import numpy as np
import rawpy


def read_raw(path):
    with rawpy.imread(str(path)) as raw:
        return {
            'pattern': raw.raw_pattern.copy(),
            'image': raw.raw_image.copy().astype(np.float32),
            'sizes': raw.sizes,
            'black_level_per_channel': list(raw.black_level_per_channel),
            'white_level': raw.white_level,
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
