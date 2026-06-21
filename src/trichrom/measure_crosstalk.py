#!/usr/bin/env python3
"""
Measure the Bayer crosstalk matrix from single-LED calibration exposures.

Shoot three ARW frames — one LED at a time (red-only, green-only, blue-only) —
against a uniform backlit target. This tool reads the Bayer-level response of
all three channel positions in each exposure, builds the 3x3 crosstalk matrix M,
and saves it as a CSV for use with trichrom-correct --matrix.

Usage:
    trichrom-measure-crosstalk --red RED.ARW --green GREEN.ARW --blue BLUE.ARW --output crosstalk.csv
"""

import argparse
from pathlib import Path

import numpy as np
import rawpy

from .legacy.combine_rgb_scans import extract_bayer_channel


def measure_crosstalk(red_path, green_path, blue_path):
    """
    Measure crosstalk matrix from single-LED calibration exposures.

    Each input is an ARW from a single LED firing alone (red-only, green-only,
    blue-only). Extract each photosite's response to each LED and normalize
    so the diagonal (correct-channel response) is 1.0.

    Returns M where M[out, in] = fraction of LED 'in' that appears in output 'out'.
    """
    medians = []
    for path in [red_path, green_path, blue_path]:
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


def main():
    ap = argparse.ArgumentParser(description='Measure Bayer crosstalk matrix from single-LED calibration ARWs.')
    ap.add_argument('--red', type=Path, required=True, help='Red-only calibration ARW')
    ap.add_argument('--green', type=Path, required=True, help='Green-only calibration ARW')
    ap.add_argument('--blue', type=Path, required=True, help='Blue-only calibration ARW')
    ap.add_argument('--output', type=Path, required=True, help='Output CSV path for crosstalk matrix')
    args = ap.parse_args()

    print("Measuring crosstalk from calibration exposures...")
    M = measure_crosstalk(args.red, args.green, args.blue)

    print("Crosstalk matrix M (normalized):")
    for row in M:
        print("  " + "  ".join(f"{v:.4f}" for v in row))

    M_inv = np.linalg.inv(M)
    print("\nCorrection matrix M⁻¹:")
    for row in M_inv:
        print("  " + "  ".join(f"{v:+.6f}" for v in row))

    np.savetxt(args.output, M, delimiter=',', fmt='%.6f')
    print(f"\nSaved to {args.output}")


if __name__ == '__main__':
    main()
