"""
Merge a trichromatic (3-shot) R/G/B ARW triplet into a linear TIFF.

Each exposure carries one clean narrowband channel, and only the matching CFA
plane is ever read from each file — so there is no LED crosstalk to correct.
Unity white balance throughout: with no cross-channel
interpolation to assist it, and no meaningful single-channel as-shot value, the
camera's per-channel WB guess is recorded in metadata as documentation only and
never applied. No lens/vignetting correction (flats handle falloff) and no DCP or
camera color matrix — the file stays in sensor space; color characterization
happens downstream in Capture One on the merged TIFF.
"""

import json
import time

import numpy as np
import pyexiv2
import tifffile

from .flatfield import apply_flat
from .icc import build_linear_prophoto_icc
from .rawio import crop_half_res, extract_led_channel_plane, read_raw
from .scanner import CHANNEL_BAYER_INDICES

_ICC_PROFILE = None


def _icc_profile():
    global _ICC_PROFILE
    if _ICC_PROFILE is None:
        _ICC_PROFILE = build_linear_prophoto_icc()
    return _ICC_PROFILE


def _normalized_plane(raw, ch):
    """Black-subtracted, white-scaled plane for one LED channel — fraction of
    usable range (white_level - black_level), before any flat-field correction."""
    plane = extract_led_channel_plane(raw['image'], raw['pattern'], CHANNEL_BAYER_INDICES[ch],
                                       raw['black_level_per_channel'])
    plane = crop_half_res(plane, raw['sizes'])
    black = raw['black_level_per_channel'][CHANNEL_BAYER_INDICES[ch][0]]
    return plane / (raw['white_level'] - black)


def merge_triplet(red_path, green_path, blue_path, flats, output_path, meta):
    """
    Build the RGB planes from their matching narrowband exposures, divide by
    flat-field, stack into a linear TIFF, and write a JSON sidecar alongside it.

    meta carries roll-level provenance (film stock, roll id, date, ScanLight
    channel levels, shutter speed) plus this frame's source filenames — all of
    it also lands in the JSON sidecar; a copy travels in the TIFF description.
    Returns the per-channel 99th-percentile peak, for the drift-check report.
    """
    raws = {
        'R': read_raw(red_path),
        'G': read_raw(green_path),
        'B': read_raw(blue_path),
    }

    planes = {ch: _normalized_plane(raws[ch], ch) for ch in 'RGB'}
    if flats:
        planes = {ch: apply_flat(planes[ch], flats[ch]) for ch in 'RGB'}

    peaks = {ch: float(np.percentile(planes[ch], 99)) for ch in 'RGB'}

    stacked = np.clip(np.stack([planes['R'], planes['G'], planes['B']], axis=-1), 0, 1)
    image = (stacked * 65535 + 0.5).astype(np.uint16)

    tiff_meta = dict(meta)
    tiff_meta['peaks'] = peaks
    tiff_meta['camera_as_shot_wb_recorded_not_applied'] = {
        ch: raws[ch]['camera_whitebalance'] for ch in 'RGB'
    }

    tifffile.imwrite(
        str(output_path),
        image,
        photometric='rgb',
        iccprofile=_icc_profile(),
        description=json.dumps(tiff_meta),
    )

    _preserve_exif(red_path, output_path)

    sidecar_path = output_path.with_suffix('.json')
    with open(sidecar_path, 'w') as f:
        json.dump({
            **tiff_meta,
            'source_files': {
                'R': str(red_path), 'G': str(green_path), 'B': str(blue_path),
            },
            'output': str(output_path),
            'merged_at': time.time(),
        }, f, indent=2)

    return peaks


# All of this describes the digitization camera copying the negative, not the
# original photograph — it's provenance of the scan. The JSON sidecar is the
# authoritative record; this is a convenience copy for tools that read EXIF.
# Missing keys are skipped, so entries absent on a given body are harmless.
_PRESERVED_EXIF_KEYS = [
    # Camera + lens identity
    'Exif.Image.Make', 'Exif.Image.Model', 'Exif.Image.Software',
    'Exif.Photo.BodySerialNumber', 'Exif.Photo.LensModel',
    'Exif.Sony2.SonyModelID', 'Exif.Sony2.LensID',
    # Copy-shot exposure settings
    'Exif.Photo.FNumber', 'Exif.Photo.ExposureTime', 'Exif.Photo.ISOSpeedRatings',
    'Exif.Photo.FocalLength', 'Exif.Photo.FocalLengthIn35mmFilm',
    # When the scan was made
    'Exif.Image.DateTime', 'Exif.Photo.DateTimeOriginal',
]


def _preserve_exif(source_arw, output_tiff):
    """Copy provenance EXIF from the (red) source ARW into the merged TIFF.
    Source ARWs themselves are never modified — they're intermediate files."""
    with pyexiv2.Image(str(source_arw)) as src:
        exif_data = src.read_exif()
    preserved = {k: exif_data[k] for k in set(_PRESERVED_EXIF_KEYS).intersection(exif_data.keys())}
    if not preserved:
        return
    try:
        with pyexiv2.Image(str(output_tiff)) as dst:
            dst.modify_exif(preserved)
    except Exception as e:
        # EXIF embedding is a convenience — the sidecar holds full provenance.
        # Sony MakerNote tags in particular may not write cleanly into a non-ARW
        # TIFF; never let that cost the merged frame.
        print(f"  NOTE: could not embed EXIF in {output_tiff.name} ({e}); "
              f"sidecar JSON still carries full provenance.")
