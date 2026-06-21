#!/usr/bin/env python3
"""
Find maximum per-channel LED brightnesses for single-shot RGB scanning.

All three LEDs (R, G, B) fire simultaneously in a single exposure — this is
NOT the trichromatic (3-shot) method where R, G, B are captured sequentially.
The tool iterates real captures to find the highest independent brightness
for each LED such that no Bayer channel clips, accounting for LED crosstalk.

Usage:
    trichrom-calibrate --watch-dir DIR [options]

Options:
    --port PORT              Serial port (default: auto-detect)
    --watch-dir DIR          Watch this directory for new ARW files (required)
    --capture-wait N         Max seconds to wait for file transfer (default: 5.0)
    --stabilize N            Seconds to wait after setting color (default: 0.1)
    --clip-threshold N       Raw value considered clipping (default: 14000)
    --probe-brightness N     Initial LED brightness for calibration iterations (default: 50)
    --dry-run                Print actions without connecting to hardware
"""

import argparse
import os
import sys
import time
from pathlib import Path

from .lib.scanner import (
    BAYER_INDEX,
    CLIP_THRESHOLD,
    PROBE_BRIGHTNESS,
    Scanlight,
    find_scanlight_port,
    sample_channel_max,
    trigger_capture,
    wait_for_new_file,
)

TARGET_RATIO = 0.97


def shoot_all(scanlight, args, bv):
    """
    Fire R+G+B LEDs simultaneously at per-channel brightnesses bv,
    trigger one capture, and return (path, {ch: max_val}).

    bv is {R, G, B} with values 0-255. Each is passed directly as r/g/b
    to set_color() with brightness=255, giving independent per-channel control.
    """
    scanlight.set_color(bv['R'], bv['G'], bv['B'], 0, 0, 255)
    time.sleep(args.stabilize)

    try:
        before = set(
            f for f in os.listdir(args.watch_dir)
            if f.upper().endswith('.ARW') and not f.startswith('.')
        )
    except FileNotFoundError:
        before = set()

    trigger_capture(args.dry_run)
    filename = wait_for_new_file(args.watch_dir, before, timeout=args.capture_wait)
    if filename is None:
        sys.exit("Calibration failed: no file appeared.")

    path = Path(args.watch_dir) / filename
    maxes = {ch: sample_channel_max(path, BAYER_INDEX[ch]) for ch in 'RGB'}
    return path, maxes


def calibrate(scanlight, args):
    """
    Iteratively adjust per-channel brightnesses until all channels converge
    to within TARGET_RATIO..1.0 of CLIP_THRESHOLD. Each shot verifies the
    current state; only exit when the most recent shot shows all channels
    in acceptable range.

    If a channel maxes out at brightness 255 without reaching the target,
    prompts the user to adjust camera exposure and retries from scratch.

    Returns {ch: brightness}.
    """
    while True:
        bv = {'R': args.probe_brightness, 'G': args.probe_brightness, 'B': args.probe_brightness}
        clipped_at = {'R': set(), 'G': set(), 'B': set()}
        all_paths = []
        iteration = 0

        while True:
            iteration += 1
            print(f"--- Iteration {iteration}  R={bv['R']} G={bv['G']} B={bv['B']} ---")

            path, maxes = shoot_all(scanlight, args, bv)
            all_paths.append(path)

            for ch in 'RGB':
                print(f"  [{ch}] 99th-percentile={maxes[ch]:.1f}")

            new_bv = {}
            channel_done = {}
            clip = args.clip_threshold
            for ch in 'RGB':
                m = maxes[ch]
                if m > clip:
                    clipped_at[ch].add(bv[ch])
                    new_bv[ch] = max(1, round(bv[ch] * clip / m))
                    channel_done[ch] = False
                elif m >= clip * TARGET_RATIO or bv[ch] == 255:
                    new_bv[ch] = bv[ch]
                    channel_done[ch] = True
                else:
                    scaled = min(255, round(bv[ch] * clip / m))
                    if scaled in clipped_at[ch] or scaled == bv[ch]:
                        new_bv[ch] = bv[ch]
                        channel_done[ch] = True
                    else:
                        new_bv[ch] = scaled
                        channel_done[ch] = False

            print()
            if all(channel_done.values()):
                print("Converged — all channels in acceptable range on this shot.\n")
                break

            if new_bv == bv:
                print("No progress — accepting current brightnesses.\n")
                break

            bv = new_bv

        underexposed = [
            ch for ch in 'RGB'
            if bv[ch] == 255 and maxes[ch] < args.clip_threshold * TARGET_RATIO
        ]
        if underexposed:
            for ch in underexposed:
                print(
                    f"  [{ch}] reached max LED brightness (255) but only achieved "
                    f"{maxes[ch]:.0f} (need ≥{args.clip_threshold * TARGET_RATIO:.0f})"
                )
            print("\nExposure too low. Raise ISO, open aperture, or slow shutter speed.")
            print("Adjust camera settings, then press Enter to retry (Ctrl+C to quit)...")
            try:
                input()
            except KeyboardInterrupt:
                print("\nInterrupted.")
                sys.exit(0)
            print()
            for p in all_paths:
                if p.exists():
                    p.unlink()
            continue

        break

    final_path = all_paths[-1]
    deleted = 0
    for p in all_paths[:-1]:
        if p.exists():
            p.unlink()
            deleted += 1
    if deleted:
        print(f"Deleted {deleted} intermediate calibration frame(s).")

    ts = time.strftime('%Y%m%d_%H%M%S')
    calib_name = f"calib_{ts}_R{bv['R']}_G{bv['G']}_B{bv['B']}{final_path.suffix}"
    calib_path = final_path.parent / calib_name
    final_path.rename(calib_path)
    print(f"Kept: {calib_name}\n")

    return bv


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--port', help='Serial port (default: auto-detect)')
    parser.add_argument('--watch-dir', required=True, metavar='DIR',
                        help='Watch this directory for new ARW files')
    parser.add_argument('--capture-wait', type=float, default=5.0, metavar='N',
                        help='Max seconds to wait for file transfer (default: 5.0)')
    parser.add_argument('--stabilize', type=float, default=0.1, metavar='N',
                        help='Seconds to wait after setting color (default: 0.1)')
    parser.add_argument('--clip-threshold', type=int, default=CLIP_THRESHOLD, metavar='N',
                        help=f'Raw value considered clipping (default: {CLIP_THRESHOLD})')
    parser.add_argument('--probe-brightness', type=int, default=PROBE_BRIGHTNESS, metavar='N',
                        help=f'Initial LED brightness for calibration iterations (default: {PROBE_BRIGHTNESS})')
    parser.add_argument('--dry-run', action='store_true',
                        help='Print actions without connecting to hardware')
    args = parser.parse_args()

    if args.dry_run:
        scanlight = Scanlight(None, dry_run=True)
        print("=== DRY RUN — no hardware will be touched ===\n")
    else:
        port = args.port or find_scanlight_port()
        print(f"Connecting to scanlight on {port}...")
        scanlight = Scanlight(port, dry_run=False)
        print("Connected.\n")

    try:
        result = calibrate(scanlight, args)
        print("=== Result ===")
        for ch in 'RGB':
            print(f"  --brightness-{ch.lower()} {result[ch]}")
        print()
        print("Pass these flags to rgb_capture.py for a balanced single-shot capture.")
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        scanlight.close()


if __name__ == '__main__':
    main()
