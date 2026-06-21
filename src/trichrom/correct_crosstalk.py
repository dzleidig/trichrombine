#!/usr/bin/env python3

import argparse
import struct
import sys
import time
import traceback
from pathlib import Path

import numpy as np
import rawpy
import tifffile
import pyexiv2
from scipy.ndimage import map_coordinates

from .combine_rgb_scans import extract_bayer_channel, cm_to_flatrational, is_file_stable, move_to_processed

# Measured from test_images/{RED,GREEN,BLUE}.ARW for the Sony A7III + Scanlight.
# Each column is one channel's crosstalk into all three sensor outputs, normalized
# so the diagonal (correct-channel response) is 1.0.
_MEASURED_M = np.array([
    [1.0000, 0.1452, 0.0651],
    [0.1455, 1.0000, 0.4514],
    [0.0416, 0.2273, 1.0000],
], dtype=np.float64)

_DEFAULT_ICC_INPUT = Path(__file__).parent.parent.parent / "GenericDngFile-Neutral.icm"


def measure_crosstalk(red_path, green_path, blue_path):
    medians = []
    for path, ch_idx in [(red_path, 0), (green_path, 1), (blue_path, 2)]:
        with rawpy.imread(str(path)) as raw:
            pattern = raw.raw_pattern.copy()
            image = raw.raw_image.copy().astype(np.float32)
            sizes = raw.sizes
            black = raw.black_level_per_channel

        row_medians = []
        for out_idx in range(3):
            data, _, _ = extract_bayer_channel(image, pattern, out_idx)
            at = sizes.top_margin // 2
            al = sizes.left_margin // 2
            ah = sizes.height // 2
            aw = sizes.width // 2
            data = data[at:at + ah, al:al + aw]
            half = 200
            ch = ah // 2
            cw = aw // 2
            data = data[ch - half:ch + half, cw - half:cw + half]
            data = data.astype(np.float64) - black[out_idx]
            row_medians.append(float(np.median(data)))
        medians.append(row_medians)

    M_raw = np.array(medians, dtype=np.float64).T
    return M_raw / M_raw.diagonal()


def _add_matrix_args(ap):
    ap.add_argument('--red', type=Path, help='Red-only calibration ARW (for measuring crosstalk)')
    ap.add_argument('--green', type=Path, help='Green-only calibration ARW (for measuring crosstalk)')
    ap.add_argument('--blue', type=Path, help='Blue-only calibration ARW (for measuring crosstalk)')


def _resolve_matrix(args):
    if args.red or args.green or args.blue:
        if not (args.red and args.green and args.blue):
            raise ValueError("--red, --green, and --blue must all be provided together")
        print("Measuring crosstalk from calibration exposures...")
        M = measure_crosstalk(args.red, args.green, args.blue)
        print("Crosstalk matrix M (normalized):")
        for row in M:
            print("  " + "  ".join(f"{v:.4f}" for v in row))
    else:
        print("Using hardcoded crosstalk matrix from test_images measurements")
        M = _MEASURED_M

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
    ap = argparse.ArgumentParser(description='Apply crosstalk correction to single-shot ARW.')
    ap.add_argument('--input', help='Input ARW file (single-shot, all LEDs on)')
    ap.add_argument('--output', help='Output DNG file path (single-file mode) or output folder (watch mode)')
    ap.add_argument('--watch', metavar='DIR', help='Watch folder for new ARWs and process automatically')
    ap.add_argument('--interval', type=float, default=5.0, help='Poll interval in seconds for --watch (default: 5)')
    _add_matrix_args(ap)
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
        output_path = Path(args.output) if args.output else input_path.with_suffix('_corrected.dng')
        try:
            correct_crosstalk(input_path, output_path, M_inv)
        except Exception as e:
            print(f"ERROR: {e}")
            traceback.print_exc()
            sys.exit(1)


# ---------------------------------------------------------------------------
# Option B: bake correction into ICC CLUT (kept for reference)
# ---------------------------------------------------------------------------

def _find_a2b0(data):
    tag_count = struct.unpack_from('>I', data, 128)[0]
    for i in range(tag_count):
        off = 132 + i * 12
        if data[off:off + 4] == b'A2B0':
            tag_offset = struct.unpack_from('>I', data, off + 4)[0]
            tag_size = struct.unpack_from('>I', data, off + 8)[0]
            return tag_offset, tag_size
    raise ValueError("No A2B0 tag found in ICC profile")


def _parse_mft2(lut_bytes):
    assert lut_bytes[0:4] == b'mft2', f"Expected mft2, got {lut_bytes[0:4]}"
    in_ch = lut_bytes[8]
    out_ch = lut_bytes[9]
    grid_pts = lut_bytes[10]
    in_entries = struct.unpack_from('>H', lut_bytes, 48)[0]

    in_table_start = 52
    in_table_bytes = in_ch * in_entries * 2
    clut_start = in_table_start + in_table_bytes
    clut_bytes_count = grid_pts ** in_ch * out_ch * 2
    out_entries = struct.unpack_from('>H', lut_bytes, 50)[0]
    out_table_start = clut_start + clut_bytes_count
    out_table_bytes = out_ch * out_entries * 2

    clut_raw = np.frombuffer(lut_bytes[clut_start:clut_start + clut_bytes_count], dtype='>u2').astype(np.float64) / 65535.0
    clut = clut_raw.reshape(grid_pts, grid_pts, grid_pts, out_ch)

    return {
        'header': lut_bytes[:in_table_start],
        'in_table': lut_bytes[in_table_start:clut_start],
        'clut': clut,
        'out_table': lut_bytes[out_table_start:out_table_start + out_table_bytes],
        'grid_pts': grid_pts,
        'out_ch': out_ch,
    }


def _bake_correction(clut, M_inv, grid_pts, out_ch):
    coords = np.linspace(0, 1, grid_pts)
    R, G, B = np.meshgrid(coords, coords, coords, indexing='ij')
    pts = np.stack([R, G, B], axis=-1)

    corrected_pts = np.clip(pts @ M_inv.T, 0, 1) * (grid_pts - 1)
    coords_for_map = corrected_pts.transpose(3, 0, 1, 2)

    new_clut = np.zeros_like(clut)
    for ch in range(out_ch):
        new_clut[..., ch] = map_coordinates(clut[..., ch], coords_for_map, order=1, mode='nearest')
    return new_clut


def bake_crosstalk_icc(input_path, output_path, M_inv):
    data = bytearray(input_path.read_bytes())
    tag_offset, tag_size = _find_a2b0(data)
    lut_bytes = bytes(data[tag_offset:tag_offset + tag_size])
    parsed = _parse_mft2(lut_bytes)

    print(f"CLUT grid: {parsed['grid_pts']}³, {parsed['out_ch']} output channels")
    print("Baking correction matrix into CLUT...")

    new_clut_bytes = (np.clip(_bake_correction(parsed['clut'], M_inv, parsed['grid_pts'], parsed['out_ch']), 0, 1) * 65535.0 + 0.5).astype(np.uint16).astype('>u2').tobytes()
    new_lut = parsed['header'] + bytes(parsed['in_table']) + new_clut_bytes + bytes(parsed['out_table'])
    assert len(new_lut) == tag_size, f"New A2B0 size {len(new_lut)} != original {tag_size}"

    data[tag_offset:tag_offset + tag_size] = new_lut

    old_name = b'GenericDngFile-Neutral\x00'
    new_name = b'GenericDngFile-Xtalked\x00'
    data = bytearray(bytes(data).replace(old_name, new_name))

    output_path.write_bytes(data)
    print(f"Written: {output_path}")


def main_bake_icc():
    ap = argparse.ArgumentParser(description='Bake crosstalk correction into ICC CLUT.')
    ap.add_argument('--input', type=Path, default=_DEFAULT_ICC_INPUT,
                    help='Source ICC profile (default: GenericDngFile-Neutral.icm)')
    ap.add_argument('--output', type=Path, required=True, help='Output ICC profile path')
    _add_matrix_args(ap)
    args = ap.parse_args()

    try:
        M_inv = _resolve_matrix(args)
    except ValueError as e:
        ap.error(str(e))

    bake_crosstalk_icc(args.input, args.output, M_inv)


if __name__ == '__main__':
    main()
