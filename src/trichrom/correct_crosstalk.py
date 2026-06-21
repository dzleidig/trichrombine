#!/usr/bin/env python3
"""
Apply crosstalk correction to single-shot RGB scans.

LEDs in single-shot scanning have non-zero spectral overlap — when the red LED
fires, it also excites the green and blue photosites (crosstalk). This tool
loads a crosstalk matrix and applies its inverse to correct the captured pixel
values.

Usage:
    trichrom-correct --input INPUT.ARW --output OUTPUT.DNG [--matrix crosstalk.csv]
    trichrom-correct --watch WATCH_DIR [--output OUTPUT_DIR] [--matrix crosstalk.csv]

Single-file mode processes one ARW and writes a corrected DNG. Watch mode
monitors a directory for new ARW files, processes them continuously, and moves
originals to a processed/ subfolder.

The crosstalk matrix is loaded from a CSV file (--matrix) produced by
trichrom-measure-crosstalk, or falls back to hardcoded measurements for the
Sony A7III + Scanlight (see test_images/crosstalk.csv).
"""

import argparse
import sys
import time
import traceback
from pathlib import Path

import numpy as np
import rawpy
import tifffile
import pyexiv2

from .legacy.combine_rgb_scans import extract_bayer_channel, cm_to_flatrational, is_file_stable, move_to_processed

# Measured from test_images/crosstalk/channel_calibration/ for the Sony A7III + Scanlight.
# Each column is one channel's crosstalk into all three sensor outputs, normalized
# so the diagonal (correct-channel response) is 1.0.
_FALLBACK_M = np.array([
    [1.0000, 0.1452, 0.0651],
    [0.1455, 1.0000, 0.4514],
    [0.0416, 0.2273, 1.0000],
], dtype=np.float64)


def _resolve_matrix(args):
    """
    Resolve and return the inverse crosstalk matrix M⁻¹.

    Loads M from --matrix CSV if provided, otherwise falls back to the
    hardcoded matrix measured for the Sony A7III + Scanlight.
    """
    if args.matrix:
        print(f"Loading crosstalk matrix from {args.matrix}")
        M = np.loadtxt(args.matrix, delimiter=',')
        print("Crosstalk matrix M:")
        for row in M:
            print("  " + "  ".join(f"{v:.4f}" for v in row))
    else:
        print("Using hardcoded crosstalk matrix (Sony A7III + Scanlight). Pass --matrix to override.")
        M = _FALLBACK_M

    M_inv = np.linalg.inv(M)
    print("Correction matrix M⁻¹:")
    for row in M_inv:
        print("  " + "  ".join(f"{v:+.6f}" for v in row))
    print()
    return M_inv


# ---------------------------------------------------------------------------
# Option A: direct pixel correction
# ---------------------------------------------------------------------------

def correct_crosstalk(input_path, output_path, M_inv):
    """
    Apply crosstalk correction to a single-shot ARW and write a corrected DNG.

    Read the RAW file, extract per-channel data, apply M⁻¹ to correct for
    crosstalk, and write the result as a DNG with metadata preserved. The
    output includes the color matrix and white balance from the original, so
    it can be correctly color-corrected in downstream processing.
    """
    print(f"  Reading: {input_path.name}")
    with rawpy.imread(str(input_path)) as raw:
        bayer_pattern = raw.raw_pattern.copy()
        bayer_data = raw.raw_image.copy().astype(np.uint16)
        sizes = raw.sizes
        WhiteLevel = raw.white_level
        BlackLevel_perChannel = np.array(raw.black_level_per_channel, dtype=np.uint16)
        WB_AsShot = raw.camera_whitebalance
        CM_XYZ2camRGB = raw.rgb_xyz_matrix

    R_data, Rrow, Rcol = extract_bayer_channel(bayer_data, bayer_pattern, 0)
    G_data, G0row, G0col = extract_bayer_channel(bayer_data, bayer_pattern, 1)
    G2_data, G1row, G1col = extract_bayer_channel(bayer_data, bayer_pattern, 3)
    B_data, Brow, Bcol = extract_bayer_channel(bayer_data, bayer_pattern, 2)

    bl_r = float(BlackLevel_perChannel[0])
    bl_g = float(BlackLevel_perChannel[1])
    bl_b = float(BlackLevel_perChannel[2])
    bl_g2 = float(BlackLevel_perChannel[3])

    print(f"  Pre-correction stats:")
    print(f"    R  max={np.amax(R_data)}  min={np.amin(R_data)}  median={np.median(R_data):.1f}")
    print(f"    G  max={np.amax(G_data)}  min={np.amin(G_data)}  median={np.median(G_data):.1f}")
    print(f"    G2 max={np.amax(G2_data)}  min={np.amin(G2_data)}  median={np.median(G2_data):.1f}")
    print(f"    B  max={np.amax(B_data)}  min={np.amin(B_data)}  median={np.median(B_data):.1f}")

    h, w = R_data.shape
    channels_signal = [
        (R_data.astype(np.float32) - bl_r).ravel(),
        (G_data.astype(np.float32) - bl_g).ravel(),
        (B_data.astype(np.float32) - bl_b).ravel(),
    ]

    corrected = M_inv @ np.stack(channels_signal, axis=0)

    R_corrected = np.clip(corrected[0].reshape(h, w) + bl_r, 0, WhiteLevel).astype(np.uint16)
    G_corrected = np.clip(corrected[1].reshape(h, w) + bl_g, 0, WhiteLevel).astype(np.uint16)
    B_corrected = np.clip(corrected[2].reshape(h, w) + bl_b, 0, WhiteLevel).astype(np.uint16)

    # G2 is also a green photosite — apply full row-1 correction using its own value with the same R and B neighbors
    G2_signal = (G2_data.astype(np.float32) - bl_g2).ravel()
    G2_corrected = np.clip(
        (M_inv[1, 0] * channels_signal[0] + M_inv[1, 1] * G2_signal + M_inv[1, 2] * channels_signal[2])
        .reshape(h, w) + bl_g2,
        0, WhiteLevel
    ).astype(np.uint16)

    print(f"  Post-correction stats:")
    print(f"    R  max={np.amax(R_corrected)}  min={np.amin(R_corrected)}  median={np.median(R_corrected):.1f}")
    print(f"    G  max={np.amax(G_corrected)}  min={np.amin(G_corrected)}  median={np.median(G_corrected):.1f}")
    print(f"    G2 max={np.amax(G2_corrected)}  min={np.amin(G2_corrected)}  median={np.median(G2_corrected):.1f}")
    print(f"    B  max={np.amax(B_corrected)}  min={np.amin(B_corrected)}  median={np.median(B_corrected):.1f}")

    merged = bayer_data.copy()
    merged[Rrow::2, Rcol::2] = R_corrected
    merged[G0row::2, G0col::2] = G_corrected
    merged[G1row::2, G1col::2] = G2_corrected
    merged[Brow::2, Bcol::2] = B_corrected

    top = sizes.top_margin
    left = sizes.left_margin
    crop_height = sizes.height
    crop_width = sizes.width
    merged = merged[top:top + crop_height, left:left + crop_width]

    bayer_pattern_copy = bayer_pattern.copy()
    blacklevel_array = np.array(BlackLevel_perChannel)[bayer_pattern_copy].astype(np.uint16)
    bayer_pattern_copy[bayer_pattern_copy == 3] = 1

    cmatrix = CM_XYZ2camRGB[:-1, :]

    preserved_keys = [
        'Exif.Photo.LensModel', 'Exif.Photo.FocalLengthIn35mmFilm', 'Exif.Photo.FocalLength',
        'Exif.Photo.FNumber', 'Exif.Photo.ExposureTime', 'Exif.Image.Make', 'Exif.Image.Model',
        'Exif.Image.Orientation', 'Exif.Image.DateTime', 'Exif.Sony2.SonyModelID',
        'Exif.Sony2.LensID', 'Exif.Photo.ISOSpeedRatings',
    ]
    with pyexiv2.Image(str(input_path)) as exiv_file:
        exif_data = exiv_file.read_exif()
    preserved_data = {k: exif_data[k] for k in set(preserved_keys).intersection(exif_data.keys())}

    unique_cam_model = (
        preserved_data.get('Exif.Image.Make', 'Unknown') + ' ' +
        preserved_data.get('Exif.Image.Model', 'Unknown')
    )

    wb_r = WB_AsShot[1] / WB_AsShot[0]
    wb_b = WB_AsShot[1] / WB_AsShot[2]

    dng_extratags = [
        ('CFARepeatPatternDim', 'H', 2, bayer_pattern_copy.shape),
        ('CFAPattern', 'B', bayer_pattern_copy.size, bayer_pattern_copy.flatten()),
        (50711, 'H', 1, 1),
        (50721, '2i', cmatrix.size, cm_to_flatrational(cmatrix)),
        (50778, 'H', 1, 17),
        (50722, '2i', cmatrix.size, cm_to_flatrational(cmatrix)),
        (50779, 'H', 1, 21),
        ('BlackLevelRepeatDim', 'H', 2, blacklevel_array.shape),
        ('BlackLevel', 'H', blacklevel_array.size, blacklevel_array.flatten()),
        ('WhiteLevel', 'H', 1, WhiteLevel),
        (50829, 'H', 4, np.array([0, 0, crop_height, crop_width], dtype=np.uint16)),
        (50719, 'H', 2, np.array([0, 0], dtype=np.uint16)),
        (50720, 'H', 2, np.array([crop_width, crop_height], dtype=np.uint16)),
        (50733, 'H', 1, 500),
        ('DNGVersion', 'B', 4, [1, 4, 0, 0]),
        ('DNGBackwardVersion', 'B', 4, [1, 4, 0, 0]),
        (50728, '2I', 3, np.array([int(wb_r * 10000), 10000, 10000, 10000, int(wb_b * 10000), 10000], dtype=np.uint32)),
        ('UniqueCameraModel', 's', len(unique_cam_model), unique_cam_model),
    ]

    print(f"  Writing: {output_path.name}")
    with tifffile.TiffWriter(str(output_path)) as dng:
        dng.write(merged.astype(np.uint16), photometric='CFA', compression=None,
                  extratags=dng_extratags, subfiletype=0, rowsperstrip=1)

    with pyexiv2.Image(str(output_path)) as dng_exiv:
        dng_exiv.modify_exif(preserved_data)

    print(f"  Done -> {output_path}")


def run_watch_loop(watch_dir, output_dir, M_inv, interval=5.0):
    """
    Continuously watch a directory for new ARW files and correct them.

    Poll watch_dir every 'interval' seconds for stable ARW files (fully written
    and not currently being modified). For each new file, apply crosstalk
    correction and move the original to watch_dir/processed/. Errors are logged
    and the file is left in place for retry.
    """
    processed = set()
    print(f"[watch] Watching {watch_dir}  →  {output_dir}")

    while True:
        arw_files = sorted(watch_dir.glob('*.ARW')) or sorted(watch_dir.glob('*.arw'))
        new_files = [f for f in arw_files if is_file_stable(f) and f.name not in processed]

        for arw in new_files:
            output_path = output_dir / f"{arw.stem}_corrected.dng"
            print(f"[watch] {arw.name}")
            try:
                correct_crosstalk(arw, output_path, M_inv)
                processed.add(arw.name)
                move_to_processed([arw], watch_dir)
            except Exception as e:
                print(f"  ERROR: {e} — file left in place for retry")
                traceback.print_exc()
            print()

        time.sleep(interval)


def main():
    """CLI entry point: parse arguments and run single-file or watch mode."""
    ap = argparse.ArgumentParser(description='Apply crosstalk correction to single-shot ARW.')
    ap.add_argument('--input', help='Input ARW file (single-shot, all LEDs on)')
    ap.add_argument('--output', help='Output DNG file path (single-file mode) or output folder (watch mode)')
    ap.add_argument('--watch', metavar='DIR', help='Watch folder for new ARWs and process automatically')
    ap.add_argument('--interval', type=float, default=5.0, help='Poll interval in seconds for --watch (default: 5)')
    ap.add_argument('--matrix', type=Path, metavar='FILE',
                    help='Crosstalk matrix CSV from trichrom-measure-crosstalk (default: built-in A7III+Scanlight values)')
    args = ap.parse_args()

    if not args.input and not args.watch:
        ap.error("one of --input or --watch is required")

    try:
        M_inv = _resolve_matrix(args)
    except ValueError as e:
        ap.error(str(e))

    if args.watch:
        watch_dir = Path(args.watch)
        output_dir = Path(args.output) if args.output else watch_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        run_watch_loop(watch_dir, output_dir, M_inv, interval=args.interval)
    else:
        input_path = Path(args.input)
        if not input_path.exists():
            print(f"ERROR: Input file not found: {input_path}")
            sys.exit(1)
        output_path = Path(args.output) if args.output else input_path.with_name(input_path.stem + '_corrected.dng')
        try:
            correct_crosstalk(input_path, output_path, M_inv)
        except Exception as e:
            print(f"ERROR: {e}")
            traceback.print_exc()
            sys.exit(1)


if __name__ == '__main__':
    main()
