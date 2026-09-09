#!/usr/bin/env python3
"""
Trichromatic (3-shot) session calibration: sets Scanlight per-channel LED levels
and camera shutter speed for a roll.

Run once at the start of a session, before scanning, with ~5 minutes of LED
warm-up (output drifts as the light comes to temperature) and the roll's own
leader positioned under the camera (base density varies by stock and batch).

Two passes, not an iterative search:
  1. Channel balance — one exposure per channel at a known starting power. LED
     output is roughly linear with drive current, so the correction is computed
     directly from the ratios: the two stronger channels are scaled down to match
     the weakest. No search loop.
  2. Exposure — with channels balanced, shutter speed alone is adjusted until the
     peak lands on target (80-90% of usable range: white_level - black_level).

No exposure bracketing: calibration exists precisely to remove that uncertainty.

Usage:
    trichrom-tri-calibrate --session-dir DIR --watch-dir DIR --film-stock NAME --roll-id ID
"""

import argparse
import sys
import time
from pathlib import Path

import numpy as np

from .lib import gphoto, session_state
from .lib.flatfield import build_channel_flat
from .lib.leader import measure_leader_level
from .lib.rawio import crop_half_res, extract_led_channel_plane, read_raw
from .lib.scanner import CHANNEL_BAYER_INDICES, OFF, Scanlight, find_scanlight_port, wait_for_new_file

CHANNELS = 'RGB'
CHANNEL_COLOR = {'R': (255, 0, 0), 'G': (0, 255, 0), 'B': (0, 0, 255)}
TARGET_LOW, TARGET_HIGH = 0.80, 0.90


def _shoot(scanlight, camera, args, r, g, b, brightness):
    scanlight.set_color(r, g, b, 0, 0, brightness)
    time.sleep(args.stabilize)

    try:
        before = set(f for f in Path(args.watch_dir).iterdir() if f.suffix.upper() == '.ARW')
    except FileNotFoundError:
        before = set()

    gphoto.trigger_and_wait(camera, timeout_ms=args.shutter_timeout_ms, dry_run=args.dry_run)

    if args.dry_run:
        return None
    filename = wait_for_new_file(args.watch_dir, {p.name for p in before}, timeout=args.capture_wait)
    if filename is None:
        sys.exit("Calibration failed: no file appeared in --watch-dir.")
    return Path(args.watch_dir) / filename


def _measure_channel_level(path, ch, flats):
    raw = read_raw(path)
    plane = extract_led_channel_plane(raw['image'], raw['pattern'], CHANNEL_BAYER_INDICES[ch],
                                       raw['black_level_per_channel'])
    plane = crop_half_res(plane, raw['sizes'])
    if flats:
        plane = plane / flats[ch]
    level = measure_leader_level(plane)
    return level, raw


def capture_flats(scanlight, camera, args, session_dir):
    """
    Average several exposures per channel with the holder off (bare light) to
    build per-channel flat-field maps. Same aperture/distance as scanning, and
    within about a stop of the frame exposures — LED-only, camera does not need
    a settled shutter speed yet since flats are shot before pass 2 sets it.
    """
    print("=== Flat fields ===")
    print("Remove the film holder — bare light only — before continuing.\n")
    flats_dir = session_dir / 'flats'
    flats_dir.mkdir(parents=True, exist_ok=True)

    flat_paths = {}
    for ch in CHANNELS:
        print(f"[{ch}] Shooting {args.flat_shots} flat exposures at brightness {args.flat_brightness}...")
        r, g, b = CHANNEL_COLOR[ch]
        raws = []
        pattern = None
        black = None
        for i in range(args.flat_shots):
            path = _shoot(scanlight, camera, args, r * args.flat_brightness // 255,
                          g * args.flat_brightness // 255, b * args.flat_brightness // 255, 255)
            if args.dry_run:
                continue
            raw = read_raw(path)
            raws.append(raw['image'])
            pattern, black = raw['pattern'], raw['black_level_per_channel']

        if args.dry_run:
            continue

        flat = build_channel_flat(raws, pattern, CHANNEL_BAYER_INDICES[ch], black)
        flat_path = flats_dir / f'{ch}.npy'
        np.save(flat_path, flat)
        flat_paths[ch] = flat_path
        print(f"[{ch}] Flat saved -> {flat_path}\n")

    return flat_paths


def balance_channels(scanlight, camera, args, flats):
    """Pass 1: one shot per channel at a known power; scale the two stronger
    channels down to match the weakest. Direct computation, no search loop."""
    print("=== Channel balance ===\n")
    levels = {}
    for ch in CHANNELS:
        r, g, b = CHANNEL_COLOR[ch]
        path = _shoot(scanlight, camera, args, r * args.start_power // 255,
                      g * args.start_power // 255, b * args.start_power // 255, 255)
        if args.dry_run:
            levels[ch] = 1.0
            continue
        level, _ = _measure_channel_level(path, ch, flats)
        levels[ch] = level
        print(f"  [{ch}] leader level = {level:.1f} at power {args.start_power}")

    weakest = min(levels.values())
    powers = {}
    for ch in CHANNELS:
        powers[ch] = args.start_power if levels[ch] == weakest else max(
            1, min(255, round(args.start_power * weakest / levels[ch])))
    print()
    for ch in CHANNELS:
        print(f"  [{ch}] power {args.start_power} -> {powers[ch]}")
    print()
    return powers


def adjust_exposure(scanlight, camera, args, powers, flats):
    """Pass 2: with channels balanced, adjust shutter speed alone until the
    peak lands within [TARGET_LOW, TARGET_HIGH] of usable range."""
    print("=== Exposure ===\n")
    target_fraction = (TARGET_LOW + TARGET_HIGH) / 2
    peaks = {}
    white_level = None
    black0 = None

    for attempt in range(1, args.max_exposure_iterations + 1):
        peaks = {}
        for ch in CHANNELS:
            r, g, b = CHANNEL_COLOR[ch]
            path = _shoot(scanlight, camera, args, r * powers[ch] // 255, g * powers[ch] // 255,
                          b * powers[ch] // 255, 255)
            if args.dry_run:
                peaks[ch] = target_fraction
                continue
            level, raw = _measure_channel_level(path, ch, flats)
            peaks[ch] = level
            white_level, black0 = raw['white_level'], raw['black_level_per_channel'][0]
            print(f"  [{ch}] leader level = {level:.1f}")

        if args.dry_run:
            print("  [dry-run] skipping shutter adjustment.\n")
            return args.dry_run_shutter, peaks

        usable_range = white_level - black0
        target = target_fraction * usable_range
        reference = float(np.mean(list(peaks.values())))
        ratio = target / reference

        print(f"  usable range = {usable_range:.0f}, target = {target:.0f}, "
              f"reference = {reference:.1f}, ratio = {ratio:.3f}")

        if TARGET_LOW * usable_range <= reference <= TARGET_HIGH * usable_range:
            print(f"  Converged after {attempt} shutter iteration(s).\n")
            return gphoto.get_shutter_speed(camera), peaks

        current = gphoto.shutter_str_to_seconds(gphoto.get_shutter_speed(camera))
        choices = gphoto.get_shutter_choices(camera)
        new_shutter = gphoto.nearest_shutter_choice(choices, current * ratio)
        print(f"  Adjusting shutter -> {new_shutter}\n")
        gphoto.set_shutter_speed(camera, new_shutter, dry_run=args.dry_run)

    print(f"  Did not fully converge after {args.max_exposure_iterations} iterations — "
          f"accepting current shutter speed (discrete steps are coarse).\n")
    return gphoto.get_shutter_speed(camera), peaks


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--session-dir', required=True, metavar='DIR', help='Session folder for state and flats')
    parser.add_argument('--watch-dir', required=True, metavar='DIR', help='Capture One session capture folder')
    parser.add_argument('--film-stock', default='', help='Film stock name, recorded in session state')
    parser.add_argument('--roll-id', default='', help='Roll identifier, recorded in session state')
    parser.add_argument('--date', default=None, help='Session date (default: today)')
    parser.add_argument('--port', help='Scanlight serial port (default: auto-detect)')
    parser.add_argument('--start-power', type=int, default=200, metavar='N',
                        help='Starting LED power 0-255 for channel balance (default: 200)')
    parser.add_argument('--flat-brightness', type=int, default=180, metavar='N',
                        help='LED brightness 0-255 for flat-field shots (default: 180)')
    parser.add_argument('--flat-shots', type=int, default=6, metavar='N',
                        help='Exposures averaged per channel for flat fields (default: 6)')
    parser.add_argument('--skip-flats', action='store_true', help='Reuse existing flats from session dir')
    parser.add_argument('--stabilize', type=float, default=0.15, metavar='N',
                        help='LED settle + stand vibration delay before triggering (default: 0.15)')
    parser.add_argument('--capture-wait', type=float, default=8.0, metavar='N',
                        help='Max seconds to wait for the ARW to land in --watch-dir (default: 8.0)')
    parser.add_argument('--shutter-timeout-ms', type=int, default=8000, metavar='N',
                        help='Max ms to wait for gphoto2 capture confirmation (default: 8000)')
    parser.add_argument('--warmup-seconds', type=float, default=300, metavar='N',
                        help='LED warm-up wait before calibrating (default: 300)')
    parser.add_argument('--skip-warmup', action='store_true', help='Skip the LED warm-up wait')
    parser.add_argument('--max-exposure-iterations', type=int, default=4, metavar='N',
                        help='Cap on shutter-speed refinement iterations (default: 4)')
    parser.add_argument('--dry-run', action='store_true', help='Print actions without touching hardware')
    parser.add_argument('--dry-run-shutter', default='1/60', help='Fake shutter value to record in --dry-run')
    args = parser.parse_args()

    session_dir = Path(args.session_dir)
    if (session_dir / 'session.json').exists():
        state = session_state.load_session(session_dir)
    else:
        state = session_state.new_session(session_dir, args.film_stock, args.roll_id, args.date)

    if args.dry_run:
        scanlight = Scanlight(None, dry_run=True)
        camera = None
        print("=== DRY RUN — no hardware will be touched ===\n")
    else:
        port = args.port or find_scanlight_port()
        print(f"Connecting to Scanlight on {port}...")
        scanlight = Scanlight(port, dry_run=False)
        print("Connecting to camera via gphoto2...")
        camera = gphoto.open_camera()
        print("Connected.\n")

    try:
        if not args.skip_warmup and not args.dry_run:
            print(f"Warming up LEDs for {args.warmup_seconds:.0f}s...")
            time.sleep(args.warmup_seconds)
            print()

        if args.skip_flats and state.get('flats'):
            flat_paths = {ch: Path(p) for ch, p in state['flats'].items()}
        else:
            flat_paths = capture_flats(scanlight, camera, args, session_dir)
            if not args.dry_run:
                session_state.set_flats(state, flat_paths)
                session_state.save_session(session_dir, state)

        flats = None
        if not args.dry_run:
            flats = {ch: np.load(p) for ch, p in flat_paths.items()}

        print("Position the roll's own leader under the camera, then press Enter...")
        if not args.dry_run:
            input()

        powers = balance_channels(scanlight, camera, args, flats)
        shutter, peaks = adjust_exposure(scanlight, camera, args, powers, flats)

        print("=== Calibration result ===")
        for ch in CHANNELS:
            print(f"  {ch}: power={powers[ch]}  peak={peaks[ch]:.1f}")
        print(f"  shutter speed: {shutter}\n")

        if not args.dry_run:
            session_state.set_calibration(state, powers, shutter, peaks)
            session_state.save_session(session_dir, state)
            print(f"Saved calibration to {session_dir / 'session.json'}")

    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        scanlight.set_color(*OFF, 255)
        scanlight.close()
        gphoto.close_camera(camera)


if __name__ == '__main__':
    main()
