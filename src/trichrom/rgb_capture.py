#!/usr/bin/env python3
"""
RGB channel capture sequence for scanlight + Sony A7III via Capture One 16.

Usage:
    python3 rgb_capture.py [options]

Options:
    --port PORT         Serial port (default: auto-detect)
    --brightness N      LED brightness 0-255 (default: 255)
    --stabilize N       Seconds to wait after setting color (default: 0.1)
    --watch-dir DIR     Watch this directory for new ARW files instead of a fixed wait
    --output-dir DIR    Where to write combined DNGs (default: <watch-dir>/combined/)
    --capture-wait N    Max seconds to wait for file transfer, or fixed wait without --watch-dir (default: 5.0)
    --channels CRGB     Channels to shoot, any combo of R G B W (default: RGB)
    --calibrate         Shoot one triplet at full brightness and compute equalized per-channel brightness
    --loop              Wait for Enter between frames, Ctrl+C to quit
    --dry-run           Print actions without connecting to hardware
"""

import argparse
import os
import sys
import time
from pathlib import Path

from .scanner import (
    BAYER_INDEX,
    CLIP_THRESHOLD,
    OFF,
    PROBE_BRIGHTNESS,
    Scanlight,
    find_scanlight_port,
    sample_channel_median,
    trigger_capture,
    wait_for_new_file,
)

CHANNELS = {
    'R': (255,   0,   0,   0,   0),
    'G': (  0, 255,   0,   0,   0),
    'B': (  0,   0, 255,   0,   0),
    'W': (  0,   0,   0, 255,   0),
}

CHANNEL_NAMES = {
    'R': 'Red',
    'G': 'Green',
    'B': 'Blue',
    'W': 'White',
}


def shoot_and_sample(scanlight, args, ch, brightness):
    """Set a single channel, trigger capture, wait for ARW, return (filename, median)."""
    r, g, b, w, ir = CHANNELS[ch]
    scanlight.set_color(r, g, b, w, ir, brightness)
    time.sleep(args.stabilize)

    try:
        before = set(f for f in os.listdir(args.watch_dir) if f.upper().endswith('.ARW') and not f.startswith('.'))
    except FileNotFoundError:
        before = set()

    ok = trigger_capture(args.dry_run)
    filename = wait_for_new_file(args.watch_dir, before, timeout=args.capture_wait) if ok else None
    if filename is None:
        sys.exit(f"Calibration failed: no file appeared for {ch} channel at brightness {brightness}.")

    path = Path(args.watch_dir) / filename
    median = sample_channel_median(path, BAYER_INDEX[ch])
    return filename, median


def run_calibration(scanlight, args):
    """
    Find the maximum non-clipping brightness for each channel independently.

    Phase 1 — single probe shot with all RGB LEDs on to estimate per-channel ratios.
    Phase 2 — for each channel, confirm and refine with real captures until the median
               lands within 3% of CLIP_THRESHOLD (from below). Each step is verified
               with an actual shot; no brightness is accepted without confirmation.

    Returns (brightnesses, filmbase_neutrals) where brightnesses are the confirmed
    per-channel LED values and filmbase_neutrals are the confirmed Dmin medians.
    """
    if not args.watch_dir:
        sys.exit("--calibrate requires --watch-dir to identify captured files.")

    print("=== Calibration probe ===\n")

    all_calibration_paths = []
    probe_medians = {}
    probe_bv = PROBE_BRIGHTNESS

    while True:
        # Single shot with all RGB LEDs on — reads all three Bayer channels at once.
        print(f"Probing all channels at brightness {probe_bv}...")
        scanlight.set_color(255, 255, 255, 0, 0, probe_bv)
        time.sleep(args.stabilize)
        try:
            before = set(f for f in os.listdir(args.watch_dir)
                         if f.upper().endswith('.ARW') and not f.startswith('.'))
        except FileNotFoundError:
            before = set()
        trigger_capture(args.dry_run)
        filename = wait_for_new_file(args.watch_dir, before, timeout=args.capture_wait)
        if filename is None:
            sys.exit("Calibration probe failed: no file appeared.")
        scanlight.set_color(*OFF, 255)

        probe_path = Path(args.watch_dir) / filename
        for ch in 'RGB':
            probe_medians[ch] = sample_channel_median(probe_path, BAYER_INDEX[ch])
            print(f"  [{ch}] median={probe_medians[ch]:.1f}")

        if any(m > CLIP_THRESHOLD for m in probe_medians.values()):
            probe_path.unlink()
            probe_bv = max(1, probe_bv // 2)
            print(f"Clipping at probe brightness — retrying at {probe_bv}...\n")
        else:
            all_calibration_paths.append(probe_path)
            print()
            break

    print()

    final_brightnesses = {}
    final_medians = {}
    final_paths = {}

    for ch in 'RGB':
        print(f"=== Maximizing {CHANNEL_NAMES[ch]} channel ===\n")
        brightness = min(255, round(probe_bv * CLIP_THRESHOLD / probe_medians[ch]))
        clipped_at = set()  # brightnesses confirmed to clip — never revisit

        while True:
            print(f"[{ch}] Shooting at brightness {brightness}...")
            filename, median = shoot_and_sample(scanlight, args, ch, brightness)
            path = Path(args.watch_dir) / filename
            all_calibration_paths.append(path)
            print(f"[{ch}] {filename}  median={median:.1f}")

            if median > CLIP_THRESHOLD:
                clipped_at.add(brightness)
                new_brightness = max(1, round(brightness * CLIP_THRESHOLD / median))
                print(f"[{ch}] Clipping — reducing {brightness} → {new_brightness}\n")
                brightness = new_brightness
            elif median < CLIP_THRESHOLD * 0.97 and brightness < 255:
                new_brightness = min(255, round(brightness * CLIP_THRESHOLD / median))
                if new_brightness in clipped_at or new_brightness == brightness:
                    # Would re-enter a clipping brightness or make no progress — accept current
                    print(f"[{ch}] Accepted: brightness={brightness}  median={median:.1f}\n")
                    final_brightnesses[ch] = brightness
                    final_medians[ch] = median
                    final_paths[ch] = path
                    break
                print(f"[{ch}] Headroom available — increasing {brightness} → {new_brightness}\n")
                brightness = new_brightness
            else:
                print(f"[{ch}] Accepted: brightness={brightness}  median={median:.1f}\n")
                final_brightnesses[ch] = brightness
                final_medians[ch] = median
                final_paths[ch] = path
                break

        scanlight.set_color(*OFF, 255)

    print("=== Calibration result ===")
    for ch in 'RGB':
        print(f"  {CHANNEL_NAMES[ch]}: brightness={final_brightnesses[ch]}  (Dmin median={final_medians[ch]:.1f})")
    print()

    keep = set(final_paths.values())
    deleted = 0
    for p in all_calibration_paths:
        if p not in keep and p.exists():
            p.unlink()
            deleted += 1
    if deleted:
        print(f"Deleted {deleted} intermediate calibration frame(s).\n")

    filmbase_neutrals = (final_medians['R'], final_medians['G'], final_medians['B'])
    return final_brightnesses, filmbase_neutrals


def capture_sequence(scanlight, args, brightnesses):
    """
    Shoot R, G, B in sequence. Returns a dict {ch: Path} of captured ARW files,
    or an empty dict if watch_dir is not set.
    """
    channels = args.channels.upper()
    captured = {}

    for ch in channels:
        name = CHANNEL_NAMES[ch]
        r, g, b, w, ir = CHANNELS[ch]
        bv = brightnesses.get(ch, args.brightness)
        print(f"[{ch}] Setting {name} channel (brightness {bv})...")
        scanlight.set_color(r, g, b, w, ir, bv)

        time.sleep(args.stabilize)

        if args.watch_dir and not args.dry_run:
            try:
                before = set(f for f in os.listdir(args.watch_dir) if f.upper().endswith('.ARW') and not f.startswith('.'))
            except FileNotFoundError:
                before = set()

        print(f"[{ch}] Triggering capture in Capture One...")
        ok = trigger_capture(args.dry_run)
        if not ok:
            print(f"[{ch}] Capture may have failed — continuing anyway.")

        if args.watch_dir and not args.dry_run:
            print(f"[{ch}] Waiting for file in {args.watch_dir}...")
            filename = wait_for_new_file(args.watch_dir, before, timeout=args.capture_wait)
            if filename:
                captured[ch] = Path(args.watch_dir) / filename
        else:
            print(f"[{ch}] Waiting {args.capture_wait}s for transfer...")
            time.sleep(args.capture_wait)
        print(f"[{ch}] Done.\n")

    print("All channels captured. Turning off LEDs.")
    scanlight.set_color(*OFF, 255)

    return captured


def combine_frame(captured, output_dir, frame_num, filmbase_neutrals=None, lcc_path=None):
    """Call combine_triplet on a captured R/G/B set and write DNG to output_dir."""
    from combine_rgb_scans import combine_triplet

    if not all(ch in captured for ch in 'RGB'):
        print(f"  WARNING: missing channels in captured set {list(captured.keys())} — skipping combine.")
        return

    red_path = captured['R']
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime('%Y%m%d_%H%M%S')
    output_path = output_dir / f"{red_path.stem}_combined_{ts}.dng"

    print(f"Combining frame {frame_num}...")
    try:
        combine_triplet(captured['R'], captured['G'], captured['B'], output_path,
                        filmbase_neutrals=filmbase_neutrals, lcc_path=lcc_path)
        print(f"  → {output_path}\n")
    except Exception as e:
        print(f"  ERROR combining frame: {e}\n")


def run_sequence(args):
    if args.dry_run:
        print("=== DRY RUN — no hardware will be touched ===\n")
        scanlight = Scanlight(None, dry_run=True)
    else:
        port = args.port or find_scanlight_port()
        print(f"Connecting to scanlight on {port}...")
        scanlight = Scanlight(port, dry_run=False)
        print("Connected.\n")

    channels = args.channels.upper()
    invalid = set(channels) - set(CHANNEL_NAMES)
    if invalid:
        sys.exit(f"Unknown channel(s): {invalid}. Use R, G, B, or W.")

    output_dir = Path(args.output_dir) if args.output_dir else (
        Path(args.watch_dir) / 'combined' if args.watch_dir else None
    )

    brightnesses = {
        'R': args.brightness_r if args.brightness_r is not None else args.brightness,
        'G': args.brightness_g if args.brightness_g is not None else args.brightness,
        'B': args.brightness_b if args.brightness_b is not None else args.brightness,
    }

    filmbase_neutrals = None
    lcc_path = Path(args.lcc) if args.lcc else None
    if lcc_path and not lcc_path.exists():
        sys.exit(f"LCC file not found: {lcc_path}")

    try:
        if args.calibrate:
            brightnesses, filmbase_neutrals = run_calibration(scanlight, args)
            scanlight.set_color(0, 0, 0, args.preview_brightness, 0, 255)

        if args.loop:
            frame = 1
            while True:
                input(f"Frame {frame} — press Enter to capture (Ctrl+C to quit)...")
                captured = capture_sequence(scanlight, args, brightnesses)
                if captured and output_dir:
                    combine_frame(captured, output_dir, frame, filmbase_neutrals, lcc_path)
                frame += 1
                # White preview light so the film is visible while advancing
                scanlight.set_color(0, 0, 0, args.preview_brightness, 0, 255)
        else:
            captured = capture_sequence(scanlight, args, brightnesses)
            if captured and output_dir:
                combine_frame(captured, output_dir, 1, filmbase_neutrals, lcc_path)

    except KeyboardInterrupt:
        print("\nInterrupted — turning off LEDs.")
        scanlight.set_color(*OFF, 255)
    finally:
        scanlight.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--port', help='Serial port (default: auto-detect)')
    parser.add_argument('--brightness', type=int, default=255, metavar='N',
                        help='LED brightness 0-255 for all channels (default: 255)')
    parser.add_argument('--brightness-r', type=int, metavar='N', help='Red channel brightness 0-255')
    parser.add_argument('--brightness-g', type=int, metavar='N', help='Green channel brightness 0-255')
    parser.add_argument('--brightness-b', type=int, metavar='N', help='Blue channel brightness 0-255')
    parser.add_argument('--stabilize', type=float, default=0.1, metavar='N',
                        help='Seconds to wait after setting color (default: 0.1)')
    parser.add_argument('--watch-dir', metavar='DIR',
                        help='Watch this directory for new ARW files instead of a fixed wait')
    parser.add_argument('--output-dir', metavar='DIR',
                        help='Where to write combined DNGs (default: <watch-dir>/combined/)')
    parser.add_argument('--capture-wait', type=float, default=5.0, metavar='N',
                        help='Max seconds to wait for file transfer, or fixed wait without --watch-dir (default: 5.0)')
    parser.add_argument('--channels', default='RGB',
                        help='Channels to shoot, any combo of R G B W (default: RGB)')
    parser.add_argument('--preview-brightness', type=int, default=32, metavar='N',
                        help='White LED brightness while advancing film in --loop mode (default: 32)')
    parser.add_argument('--calibrate', action='store_true',
                        help='Shoot one triplet at full brightness and compute equalized per-channel brightness')
    parser.add_argument('--loop', action='store_true',
                        help='Wait for Enter between frames, Ctrl+C to quit')
    parser.add_argument('--lcc', metavar='PATH',
                        help='Path to a flat-field ARW for light falloff correction (e.g. lcc.ARW)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Print actions without connecting to hardware')
    args = parser.parse_args()

    if not 0 <= args.brightness <= 255:
        sys.exit("--brightness must be 0-255")

    run_sequence(args)


if __name__ == '__main__':
    main()
