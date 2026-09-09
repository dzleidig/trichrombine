#!/usr/bin/env python3
"""
Trichromatic (3-shot) capture and merge.

Launch once per roll, using a session already calibrated by trichrom-tri-calibrate.
Per frame: advance the film, check framing in Capture One's live view, press Enter
(terminal or footswitch). The script fires all three exposures, merges them into a
linear TIFF, prints the channel peaks as a drift check, and waits for the next.

No autofocus, ever — AF on a flat low-contrast negative is unreliable, and firing
between exposures would shift focus across channels. Focus is set once per session
in Capture One and never touched here.

Usage:
    trichrom-tri-capture --resume --watch-dir DIR --output-dir DIR
    trichrom-tri-capture --session-dir DIR --watch-dir DIR --output-dir DIR
"""

import argparse
import sys
import time
from pathlib import Path

import numpy as np

from .lib import gphoto, session_state
from .lib.merge_tri import merge_triplet
from .lib.scanner import CHANNEL_BAYER_INDICES, OFF, Scanlight, find_scanlight_port, wait_for_new_file

CHANNELS = 'RGB'
CHANNEL_COLOR = {'R': (255, 0, 0), 'G': (0, 255, 0), 'B': (0, 0, 255)}
SETTLE_SECONDS = 1.0


def is_file_stable(path, min_age_seconds=SETTLE_SECONDS):
    try:
        return time.time() - path.stat().st_mtime >= min_age_seconds
    except FileNotFoundError:
        return False


def wait_for_settle(path, timeout=10.0):
    """Wait for the ARW's size to stop changing before reading it — the classic
    source of intermittent corruption is reading a half-written raw."""
    deadline = time.monotonic() + timeout
    last_size = -1
    while time.monotonic() < deadline:
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            time.sleep(0.1)
            continue
        if size == last_size and is_file_stable(path):
            return
        last_size = size
        time.sleep(0.2)


def shoot_channel(scanlight, camera, args, ch, level):
    r, g, b = CHANNEL_COLOR[ch]
    scanlight.set_color(r * level // 255, g * level // 255, b * level // 255, 0, 0, 255)
    time.sleep(args.stabilize)

    try:
        before = {p.name for p in Path(args.watch_dir).iterdir() if p.suffix.upper() == '.ARW'}
    except FileNotFoundError:
        before = set()

    gphoto.trigger_and_wait(camera, timeout_ms=args.shutter_timeout_ms, dry_run=args.dry_run)

    if args.dry_run:
        return None
    filename = wait_for_new_file(args.watch_dir, before, timeout=args.capture_wait)
    if filename is None:
        print(f"  WARNING: no file appeared for channel {ch} — frame will be marked failed.")
        return None
    return Path(args.watch_dir) / filename


def capture_frame(scanlight, camera, args, levels):
    """Fire R, G, B in sequence and return {ch: Path} once all three have landed
    and settled on disk, or a partial dict if a channel failed."""
    captured = {}
    for ch in CHANNELS:
        path = shoot_channel(scanlight, camera, args, ch, levels[ch])
        if path is None:
            continue
        wait_for_settle(path)
        captured[ch] = path
    return captured


def run_loop(scanlight, camera, args, state, flats, levels):
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    frame = len(state['frames']) + 1

    while True:
        try:
            input(f"Frame {frame} — press Enter to capture (Ctrl+C to quit)...")
        except EOFError:
            break

        captured = capture_frame(scanlight, camera, args, levels)
        if len(captured) != 3:
            print(f"  Frame {frame}: incomplete capture ({sorted(captured)}) — skipping merge.\n")
            session_state.record_frame(state, frame, {ch: str(p) for ch, p in captured.items()}, None, 'incomplete')
            session_state.save_session(args.session_dir, state)
            frame += 1
            continue

        output_path = output_dir / f"{args.roll_id or 'roll'}_{frame:04d}.tiff"
        meta = {
            'film_stock': state['film_stock'],
            'roll_id': state['roll_id'],
            'date': state['date'],
            'frame': frame,
            'channel_levels': levels,
            'shutter_speed': state['calibration']['shutter_speed'],
        }
        try:
            peaks = merge_triplet(captured['R'], captured['G'], captured['B'], flats, output_path, meta)
            print(f"  Frame {frame} -> {output_path}")
            for ch in CHANNELS:
                print(f"    [{ch}] peak={peaks[ch]:.3f}")
            print()
            status = 'ok'
        except Exception as e:
            print(f"  ERROR merging frame {frame}: {e}\n")
            status = 'merge_failed'

        session_state.record_frame(
            state, frame, {ch: str(p) for ch, p in captured.items()}, output_path, status)
        session_state.save_session(args.session_dir, state)
        frame += 1


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--session-dir', metavar='DIR', help='Session folder (default: --resume the last one)')
    parser.add_argument('--resume', action='store_true', help='Resume the most recently used session')
    parser.add_argument('--watch-dir', required=True, metavar='DIR', help='Capture One session capture folder')
    parser.add_argument('--output-dir', required=True, metavar='DIR', help='Where merged TIFFs are written')
    parser.add_argument('--film-stock', help='Override film stock recorded in session state')
    parser.add_argument('--roll-id', help='Override roll identifier recorded in session state')
    parser.add_argument('--date', help='Override session date')
    parser.add_argument('--port', help='Scanlight serial port (default: auto-detect)')
    parser.add_argument('--stabilize', type=float, default=0.15, metavar='N',
                        help='LED settle + stand vibration delay before triggering (default: 0.15)')
    parser.add_argument('--capture-wait', type=float, default=8.0, metavar='N',
                        help='Max seconds to wait for the ARW to land in --watch-dir (default: 8.0)')
    parser.add_argument('--shutter-timeout-ms', type=int, default=8000, metavar='N',
                        help='Max ms to wait for gphoto2 capture confirmation (default: 8000)')
    parser.add_argument('--preview-brightness', type=int, default=32, metavar='N',
                        help='White-equivalent (R=G=B) LED level while advancing film (default: 32)')
    parser.add_argument('--dry-run', action='store_true', help='Print actions without touching hardware')
    args = parser.parse_args()

    if not args.session_dir and not args.resume:
        parser.error("one of --session-dir or --resume is required")
    args.session_dir = session_state.resolve_resume_dir(args.session_dir)

    state = session_state.load_session(args.session_dir)
    if not state.get('calibration'):
        sys.exit(f"No calibration recorded in {args.session_dir}. Run trichrom-tri-calibrate first.")
    session_state.warn_if_stale(state)

    for field, value in (('film_stock', args.film_stock), ('roll_id', args.roll_id), ('date', args.date)):
        if value:
            state[field] = value
    args.roll_id = state['roll_id']

    levels = state['calibration']['channel_levels']
    shutter = state['calibration']['shutter_speed']
    flats = None
    if state.get('flats') and not args.dry_run:
        flats = {ch: np.load(p) for ch, p in state['flats'].items()}

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
        gphoto.set_shutter_speed(camera, shutter, dry_run=args.dry_run)
        print(f"Connected. Shutter set to {shutter}.\n")

    print(f"Session: {args.session_dir}  film={state['film_stock']}  roll={state['roll_id']}")
    print(f"Channel levels: {levels}\n")

    try:
        run_loop(scanlight, camera, args, state, flats, levels)
    except KeyboardInterrupt:
        print("\nInterrupted — turning off LEDs.")
        failed = session_state.failed_frames(state)
        if failed:
            print(f"{len(failed)} frame(s) need redoing: {[f['frame'] for f in failed]}")
    finally:
        pb = args.preview_brightness
        scanlight.set_color(pb, pb, pb, 0, 0, 255)
        scanlight.close()
        gphoto.close_camera(camera)


if __name__ == '__main__':
    main()
