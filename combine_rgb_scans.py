#!/usr/bin/env python3
"""
combine_rgb_scans.py

Combines 3 sequential ARW exposures (R, G, B narrowband) into a single DNG
by extracting each channel's Bayer photosites and recombining them.

Usage:
    python combine_rgb_scans.py --input /path/to/folder --output /path/to/output

Input folder should contain triplets of ARW files with sequential numbering:
    DSC00001.ARW  -> Red exposure
    DSC00002.ARW  -> Green exposure
    DSC00003.ARW  -> Blue exposure
    DSC00004.ARW  -> Red exposure (next frame)
    ...

Output DNGs will be named after the first file in each triplet:
    DSC00001_combined.DNG
    DSC00004_combined.DNG
    ...
"""

import argparse
import shutil
import sys
import time
import traceback
from pathlib import Path

import numpy as np
import rawpy
import tifffile
import pyexiv2


def cm_to_flatrational(input_array):
    """Convert a numpy array to a flat array of RATIONAL pairs (numerator, denominator)."""
    retarray = np.ones(input_array.size * 2, dtype=np.int32)
    retarray[0::2] = (input_array.flatten() * 10000).astype(np.int32)
    retarray[1::2] = 10000
    return retarray


def compute_filmbase_neutrals(red_path, green_path, blue_path):
    """
    Compute per-channel median pixel values from a film base triplet.
    Returns (median_r, median_g, median_b) after black-level subtraction,
    sampled from the center 50% of the active sensor area.
    """
    results = []
    for path, ch_idx in [(red_path, 0), (green_path, 1), (blue_path, 2)]:
        with rawpy.imread(str(path)) as raw:
            pattern = raw.raw_pattern.copy()
            image = raw.raw_image.copy().astype(np.float32)
            sizes = raw.sizes
            black = raw.black_level_per_channel

        data, row, col = extract_bayer_channel(image, pattern, ch_idx)

        # Crop to active sensor area (in half-resolution photosite coords)
        top = sizes.top_margin // 2
        left = sizes.left_margin // 2
        h = sizes.height // 2
        w = sizes.width // 2
        data = data[top:top + h, left:left + w]

        # Sample a 175×175 camera-pixel region from the center (≈87×87 photosites).
        # Small fixed crop avoids sprocket holes and frame edges in a filmbase triplet.
        half = 87  # 175 camera px / 2 = ~87 photosites
        ch = h // 2
        cw = w // 2
        data = data[ch - half:ch + half, cw - half:cw + half]

        # Subtract black level for this channel.
        # black_level_per_channel is indexed by channel (0=R, 1=G, 2=B, 3=G2).
        data = data - black[ch_idx]

        results.append(float(np.median(data)))

    median_r, median_g, median_b = results
    print(f"  Film base medians (post-black): R={median_r:.1f}  G={median_g:.1f}  B={median_b:.1f}")
    return median_r, median_g, median_b


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


def combine_triplet(red_path, green_path, blue_path, output_path, filmbase_neutrals=None):
    """
    Extract R from red_path, G from green_path, B from blue_path,
    recombine into a single Bayer array, and write as DNG.
    """
    print(f"  Reading R: {red_path.name}")
    with rawpy.imread(str(red_path)) as raw_r:
        bayer_pattern = raw_r.raw_pattern.copy()
        bayer_data_r = raw_r.raw_image.copy().astype(np.uint16)
        sizes = raw_r.sizes

    print(f"  Reading G: {green_path.name}")
    with rawpy.imread(str(green_path)) as raw_g:
        bayer_data_g = raw_g.raw_image.copy().astype(np.uint16)

    print(f"  Reading B: {blue_path.name}")
    with rawpy.imread(str(blue_path)) as raw_b:
        bayer_data_b = raw_b.raw_image.copy().astype(np.uint16)
        # Pull metadata from blue (last) frame - same as Entropy512 approach
        WB_AsShot = raw_b.camera_whitebalance
        WhiteLevel = raw_b.white_level
        BlackLevel_perChannel = np.array(raw_b.black_level_per_channel, dtype=np.uint16)
        CM_XYZ2camRGB = raw_b.rgb_xyz_matrix

    # Extract each channel's photosites from the relevant exposure
    R_data, Rrow, Rcol = extract_bayer_channel(bayer_data_r, bayer_pattern, 0)  # R
    G_data, G0row, G0col = extract_bayer_channel(bayer_data_g, bayer_pattern, 1)  # G
    G2_data, G1row, G1col = extract_bayer_channel(bayer_data_g, bayer_pattern, 3)  # G2
    B_data, Brow, Bcol = extract_bayer_channel(bayer_data_b, bayer_pattern, 2)  # B

    print(f"  R  max={np.amax(R_data)}  min={np.amin(R_data)}")
    print(f"  G  max={np.amax(G_data)}  min={np.amin(G_data)}")
    print(f"  G2 max={np.amax(G2_data)} min={np.amin(G2_data)}")
    print(f"  B  max={np.amax(B_data)}  min={np.amin(B_data)}")

    # Start with the blue frame's raw data as the base array (has correct metadata)
    # then overwrite each channel's photosites with data from the correct exposure
    merged = bayer_data_b.copy()
    merged[Rrow::2, Rcol::2] = R_data
    merged[G0row::2, G0col::2] = G_data
    merged[G1row::2, G1col::2] = G2_data
    merged[Brow::2, Bcol::2] = B_data

    # Crop to active sensor area using margins from the raw file
    top = sizes.top_margin
    left = sizes.left_margin
    crop_height = sizes.height
    crop_width = sizes.width
    merged = merged[top:top + crop_height, left:left + crop_width]

    # Build blacklevel array shaped to match Bayer pattern
    bayer_pattern_copy = bayer_pattern.copy()
    blacklevel_array = np.array(BlackLevel_perChannel)[bayer_pattern_copy].astype(np.uint16)

    # RT crashes if G2 is stored as index 3 instead of 1 — remap it
    bayer_pattern_copy[bayer_pattern_copy == 3] = 1

    # Color matrix (3x3, drop the last row which is all zeros in rawpy)
    cmatrix = CM_XYZ2camRGB[:-1, :]

    # Preserved EXIF keys from the red (first) frame
    preserved_keys = [
        'Exif.Photo.LensModel',
        'Exif.Photo.FocalLengthIn35mmFilm',
        'Exif.Photo.FocalLength',
        'Exif.Photo.FNumber',
        'Exif.Photo.ExposureTime',
        'Exif.Image.Make',
        'Exif.Image.Model',
        'Exif.Image.Orientation',
        'Exif.Image.DateTime',
        'Exif.Sony2.SonyModelID',
        'Exif.Sony2.LensID',
        'Exif.Photo.ISOSpeedRatings',
    ]

    with pyexiv2.Image(str(red_path)) as exiv_file:
        exif_data = exiv_file.read_exif()
    preserved_data = {k: exif_data[k] for k in set(preserved_keys).intersection(exif_data.keys())}

    unique_cam_model = (
        preserved_data.get('Exif.Image.Make', 'Unknown') + ' ' +
        preserved_data.get('Exif.Image.Model', 'Unknown')
    )

    # Build DNG extra tags
    # DNG tag numeric codes: CFALayout=50711, BayerGreenSplit=50712,
    # DefaultCropOrigin=50719, DefaultCropSize=50720, ColorMatrix1=50721,
    # ColorMatrix2=50722, AsShotNeutral=50728, ActiveArea=50829,
    # CalibrationIlluminant1=50778, CalibrationIlluminant2=50779
    dng_extratags = []
    dng_extratags.append(('CFARepeatPatternDim', 'H', 2, bayer_pattern_copy.shape))
    dng_extratags.append(('CFAPattern', 'B', bayer_pattern_copy.size, bayer_pattern_copy.flatten()))
    dng_extratags.append((50711, 'H', 1, 1))  # CFALayout = Rectangular

    # ColorMatrix1 (Standard Light A) + ColorMatrix2 (D65) — we only have D65 from rawpy
    dng_extratags.append((50721, '2i', cmatrix.size, cm_to_flatrational(cmatrix)))  # ColorMatrix1
    dng_extratags.append((50778, 'H', 1, 17))   # CalibrationIlluminant1 = Standard Light A
    dng_extratags.append((50722, '2i', cmatrix.size, cm_to_flatrational(cmatrix)))  # ColorMatrix2
    dng_extratags.append((50779, 'H', 1, 21))   # CalibrationIlluminant2 = D65

    dng_extratags.append(('BlackLevelRepeatDim', 'H', 2, blacklevel_array.shape))
    dng_extratags.append(('BlackLevel', 'H', blacklevel_array.size, blacklevel_array.flatten()))
    dng_extratags.append(('WhiteLevel', 'H', 1, WhiteLevel))

    # Active area and crop tags
    dng_extratags.append((50829, 'H', 4, np.array([0, 0, crop_height, crop_width], dtype=np.uint16)))  # ActiveArea
    dng_extratags.append((50719, 'H', 2, np.array([0, 0], dtype=np.uint16)))        # DefaultCropOrigin
    dng_extratags.append((50720, 'H', 2, np.array([crop_width, crop_height], dtype=np.uint16)))  # DefaultCropSize
    dng_extratags.append((50733, 'H', 1, 500))  # BayerGreenSplit

    dng_extratags.append(('DNGVersion', 'B', 4, [1, 4, 0, 0]))
    dng_extratags.append(('DNGBackwardVersion', 'B', 4, [1, 4, 0, 0]))

    # AsShotNeutral: G-normalized reciprocals (G/R, 1, G/B).
    # If film base neutrals are provided, derive from those (correct for narrowband scanning).
    # Otherwise fall back to camera white balance (arbitrary, but better than nothing).
    if filmbase_neutrals is not None:
        med_r, med_g, med_b = filmbase_neutrals
        wb_r = med_r / med_g   # R_neutral / G_neutral  (< 1; raw processor inverts to get boost)
        wb_b = med_b / med_g   # B_neutral / G_neutral  (< 1)
    else:
        wb_r = WB_AsShot[1] / WB_AsShot[0]
        wb_b = WB_AsShot[1] / WB_AsShot[2]
    dng_extratags.append((50728, '2I', 3, np.array([  # AsShotNeutral
        int(wb_r * 10000), 10000,
        10000, 10000,
        int(wb_b * 10000), 10000
    ], dtype=np.uint32)))

    dng_extratags.append(('UniqueCameraModel', 's', len(unique_cam_model), unique_cam_model))

    print(f"  Writing: {output_path.name}")
    with tifffile.TiffWriter(str(output_path)) as dng:
        dng.write(
            merged.astype(np.uint16),
            photometric='CFA',
            compression=None,
            extratags=dng_extratags,
            subfiletype=0,
            rowsperstrip=1,
        )

    # Write preserved EXIF into the DNG
    with pyexiv2.Image(str(output_path)) as dng_exiv:
        dng_exiv.modify_exif(preserved_data)

    print(f"  Done -> {output_path}")


def is_file_stable(path, min_age_seconds=2.0):
    """Return True if the file hasn't been modified in the last min_age_seconds."""
    try:
        return time.time() - path.stat().st_mtime >= min_age_seconds
    except FileNotFoundError:
        return False


def move_to_processed(files, input_dir):
    """Move a list of files into <input_dir>/processed/, creating it if needed."""
    processed_dir = input_dir / 'processed'
    processed_dir.mkdir(exist_ok=True)
    for f in files:
        dest = processed_dir / f.name
        shutil.move(str(f), str(dest))
        print(f"  Moved {f.name} -> processed/")


def find_triplets(input_dir, min_age_seconds=2.0, quiet=False):
    """
    Find all ARW files in input_dir, sort them, and group into triplets.
    Returns list of (red_path, green_path, blue_path) tuples.
    Files that were modified within min_age_seconds are excluded (still being written).
    """
    arw_files = sorted(input_dir.glob('*.ARW'))
    if not arw_files:
        arw_files = sorted(input_dir.glob('*.arw'))

    arw_files = [f for f in arw_files if is_file_stable(f, min_age_seconds)]

    if not quiet and len(arw_files) % 3 != 0:
        print(f"WARNING: {len(arw_files)} ARW files found — not a multiple of 3.")
        print(f"         Will process {len(arw_files) // 3} complete triplets, ignoring remainder.")

    triplets = []
    for i in range(0, len(arw_files) - 2, 3):
        triplets.append((arw_files[i], arw_files[i + 1], arw_files[i + 2]))

    return triplets


def run_watch_loop(input_dir, output_dir, interval, min_age_seconds, do_move, filmbase_neutrals=None):
    """Poll input_dir and process new ARW triplets as they arrive."""
    processed_set = set()

    while True:
        triplets = find_triplets(input_dir, min_age_seconds=min_age_seconds, quiet=True)
        new_triplets = [
            t for t in triplets
            if (t[0].name, t[1].name, t[2].name) not in processed_set
        ]

        for red, green, blue in new_triplets:
            ts = time.strftime('%Y%m%d_%H%M%S')
            output_name = f"{red.stem}_combined_{ts}.dng"
            output_path = output_dir / output_name
            print(f"[watch] {red.name} + {green.name} + {blue.name}")
            try:
                combine_triplet(red, green, blue, output_path, filmbase_neutrals=filmbase_neutrals)
                processed_set.add((red.name, green.name, blue.name))
                if do_move:
                    move_to_processed([red, green, blue], input_dir)
            except Exception as e:
                print(f"  ERROR: {e} — files left in place for retry")
                traceback.print_exc()
            print()

        time.sleep(interval)


def main():
    ap = argparse.ArgumentParser(description='Combine RGB narrowband ARW triplets into DNG files.')
    ap.add_argument('-i', '--input', required=True, help='Folder containing ARW triplets')
    ap.add_argument('-o', '--output', required=True, help='Folder for output DNG files')
    ap.add_argument('--watch', action='store_true',
                    help='Watch input folder continuously and process triplets as they arrive')
    ap.add_argument('--interval', type=float, default=5.0,
                    help='Poll interval in seconds for --watch mode (default: 5)')
    ap.add_argument('--move', action='store_true',
                    help='Move processed ARW files to <input>/processed/ after successful combine')
    ap.add_argument('--filmbase', metavar='PATH',
                    help='Directory containing a film base ARW triplet (R, G, B order). '
                         'When provided, AsShotNeutral is derived from film base channel '
                         'medians instead of camera white balance.')
    args = ap.parse_args()

    input_dir = Path(args.input)
    output_dir = Path(args.output)

    if not input_dir.exists():
        print(f"ERROR: Input folder not found: {input_dir}")
        sys.exit(1)

    output_dir.mkdir(parents=True, exist_ok=True)

    filmbase_neutrals = None
    if args.filmbase:
        filmbase_dir = Path(args.filmbase)
        if not filmbase_dir.exists():
            print(f"ERROR: Film base folder not found: {filmbase_dir}")
            sys.exit(1)
        fb_triplets = find_triplets(filmbase_dir, min_age_seconds=0)
        if not fb_triplets:
            print(f"ERROR: No complete ARW triplet found in film base folder: {filmbase_dir}")
            sys.exit(1)
        fb_red, fb_green, fb_blue = fb_triplets[0]
        print(f"Computing film base neutrals from: {fb_red.name}, {fb_green.name}, {fb_blue.name}")
        filmbase_neutrals = compute_filmbase_neutrals(fb_red, fb_green, fb_blue)
        print()

    if args.watch:
        print(f"Watching {input_dir} every {args.interval}s. Ctrl-C to stop.")
        if not args.move:
            print("NOTE: --move not set; processed files stay in place. "
                  "Processed triplets are tracked in memory only (resets on restart).")
        print()
        try:
            run_watch_loop(input_dir, output_dir, args.interval,
                           min_age_seconds=2.0, do_move=args.move,
                           filmbase_neutrals=filmbase_neutrals)
        except KeyboardInterrupt:
            print("\nWatch mode stopped.")
        return

    triplets = find_triplets(input_dir)
    if not triplets:
        print("No complete triplets found. Exiting.")
        sys.exit(1)

    print(f"Found {len(triplets)} triplets in {input_dir}")
    print()

    for i, (red, green, blue) in enumerate(triplets, 1):
        ts = time.strftime('%Y%m%d_%H%M%S')
        output_name = f"{red.stem}_combined_{ts}.dng"
        output_path = output_dir / output_name
        print(f"[{i}/{len(triplets)}] {red.stem} + {green.stem} + {blue.stem}")
        try:
            combine_triplet(red, green, blue, output_path, filmbase_neutrals=filmbase_neutrals)
            if args.move:
                move_to_processed([red, green, blue], input_dir)
        except Exception as e:
            print(f"  ERROR processing triplet: {e}")
            traceback.print_exc()
        print()

    print(f"Complete. {len(triplets)} DNG files written to {output_dir}")


if __name__ == '__main__':
    main()
