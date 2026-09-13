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
from .rawio import (INTERPOLATION_ORDER, active_site_offsets, crop_half_res,
                    extract_bayer_channel, read_raw, upsample_bayer_plane)
from .scanner import CHANNEL_BAYER_INDICES

_ICC_PROFILE = None


def _icc_profile():
    global _ICC_PROFILE
    if _ICC_PROFILE is None:
        _ICC_PROFILE = build_linear_prophoto_icc()
    return _ICC_PROFILE


def _channel_field(raw, ch, flat, full_resolution):
    """
    Normalized, flat-corrected signal for one LED channel, plus its drift peak, as a
    fraction of usable range (white_level - black_level).

    Half resolution keeps one output pixel per measured photosite: nothing is
    interpolated, and every value in the result was really read off the sensor.

    Full resolution interpolates each contributing sub-plane from the sites that
    were really read, at their true positions in the 2x2 cell. Three-shot capture
    removes the *spectral* mixing between channels but not the *spatial* sparsity
    of the CFA — red is still only sampled at a quarter of the photosites — so the
    missing three-quarters are reconstructed here. Because each channel comes from
    its own exposure there is no cross-channel contamination to fight, which makes
    this a cleaner reconstruction than demosaicing a single Bayer frame, but it is
    still interpolation: the extra pixels are inferred, not measured.

    The flat is applied before upsampling, on the half-resolution grid it was built
    on. Flats are heavily smoothed by construction, so correcting then interpolating
    and interpolating then correcting differ negligibly — and doing it this way
    keeps flats from existing sessions usable unchanged.
    """
    indices = CHANNEL_BAYER_INDICES[ch]
    black = raw['black_level_per_channel']
    sizes = raw['sizes']
    scale = raw['white_level'] - black[indices[0]]

    # float32 throughout: a full-resolution plane off a 61MP sensor is ~240MB at
    # single precision and twice that at double, and three channels plus
    # interpolation scratch have to be live at once.
    subs = []
    for idx in indices:
        sub, row_off, col_off = extract_bayer_channel(raw['image'], raw['pattern'], idx)
        sub = crop_half_res(sub.astype(np.float32) - black[idx], sizes) / scale
        if flat is not None:
            sub = apply_flat(sub, flat)
        subs.append((sub, row_off, col_off))

    measured = subs[0][0] if len(subs) == 1 else np.mean([s for s, _, _ in subs], axis=0)

    # The drift peak comes from the measured samples rather than the finished plane.
    # That keeps it meaning the same thing at either --resolution, so peaks stay
    # comparable across a roll shot both ways — and it reads a quarter as much data.
    peak = float(np.percentile(measured, 99))

    if not full_resolution:
        return measured, peak

    out_shape = (sizes.height, sizes.width)
    fields = [
        upsample_bayer_plane(sub, *active_site_offsets(row_off, col_off, sizes),
                             out_shape=out_shape)
        for sub, row_off, col_off in subs
    ]
    return (fields[0] if len(fields) == 1 else np.mean(fields, axis=0)), peak


def merge_triplet(red_path, green_path, blue_path, flats, output_path, meta,
                  full_resolution=False):
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

    fields = {ch: _channel_field(raws[ch], ch, flats[ch] if flats else None, full_resolution)
              for ch in 'RGB'}
    planes = {ch: field for ch, (field, _) in fields.items()}
    peaks = {ch: peak for ch, (_, peak) in fields.items()}

    # Filled a channel at a time rather than stacked: at full resolution a stacked
    # float array of three 61MP channels is most of a gigabyte before it is scaled.
    h, w = planes['R'].shape
    image = np.empty((h, w, 3), dtype=np.uint16)
    for i, ch in enumerate('RGB'):
        image[..., i] = (np.clip(planes[ch], 0, 1) * 65535 + 0.5).astype(np.uint16)

    tiff_meta = dict(meta)
    tiff_meta['peaks'] = peaks
    tiff_meta['output_resolution'] = 'full' if full_resolution else 'half'
    # Whether these pixels were measured or reconstructed is provenance, not trivia:
    # it decides what the file can honestly be compared against later.
    tiff_meta['interpolation'] = (
        f'per-channel cubic spline (order {INTERPOLATION_ORDER}) from measured sites'
        if full_resolution else 'none — one output pixel per measured photosite')
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
