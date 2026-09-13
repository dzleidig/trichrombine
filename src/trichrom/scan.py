#!/usr/bin/env python3
"""
Trichromatic (3-shot) session scanning: calibrates once, then captures and merges.

Launch once per roll. A brand-new session (or --recalibrate) runs calibration
first — LED warm-up, per-channel flats, then the leader-based two-pass channel
balance + exposure targeting — before dropping into the capture loop. Resuming
an already-calibrated session skips straight to capturing, unless the saved
calibration looks stale, in which case you're asked whether to redo it.

Per frame: advance the film, check framing in Capture One's live view, press
Enter (terminal or footswitch). All three exposures fire in sequence, merge into
a linear 16-bit TIFF, and print per-channel peaks as a drift check.

The shutter is fired through Capture One by default, which keeps C1 the sole
owner of the camera — Sony's PC Remote allows one controlling host at a time, so
driving the camera directly would cost the live view that focusing depends on.
--camera-backend gphoto2 drives the camera directly instead.

No autofocus, ever — AF on a flat low-contrast negative is unreliable, and
firing between exposures would shift focus across channels. Focus is set once
per session in Capture One and never touched here.

No exposure bracketing: calibration exists precisely to remove that uncertainty.

Usage:
    trichrom-scan --session-dir /path/to/session --watch-dir DIR \
        --film-stock "Portra 400" --roll-id roll01
    trichrom-scan --resume --watch-dir DIR

--output-dir defaults to --watch-dir (merged TIFFs land next to the ARWs); pass
it explicitly to write merged TIFFs somewhere else.
"""

import argparse
import sys
import time
from pathlib import Path

import numpy as np

from .lib import captureone, gphoto, session_state
from .lib.flatfield import FLAT_TARGET, build_channel_flat, flat_level
from .lib.leader import measure_leader_level
from .lib.merge_tri import merge_triplet
from .lib.rawio import crop_half_res, extract_led_channel_plane, read_raw
from .lib.scanner import CHANNEL_BAYER_INDICES, OFF, Scanlight, find_scanlight_port, wait_for_new_file
from .lib.shutter import nearest_shutter_choice, shutter_str_to_seconds

CHANNELS = 'RGB'
# Which of the Scanlight's r/g/b slots each channel drives; the others stay at 0.
CHANNEL_SLOT = {'R': 0, 'G': 1, 'B': 2}
TARGET_LOW, TARGET_HIGH = 0.80, 0.90
SETTLE_SECONDS = 1.0
CAMERA_BACKENDS = {'captureone': captureone, 'gphoto2': gphoto}


# --------------------------------------------------------------------------
# Shared hardware helpers
# --------------------------------------------------------------------------

def wait_for_settle(path, timeout=10.0, min_age_seconds=SETTLE_SECONDS):
    """
    Wait for the ARW's size to stop changing before reading it — the classic
    source of intermittent corruption is reading a half-written raw.

    Returns False if it never settled. That must not be treated as success: a
    timeout here means the file is most likely still being written, which is
    exactly the case this guard exists to catch.
    """
    deadline = time.monotonic() + timeout
    last_size = -1
    while time.monotonic() < deadline:
        try:
            stat = path.stat()
        except FileNotFoundError:
            time.sleep(0.1)
            continue
        if stat.st_size == last_size and time.time() - stat.st_mtime >= min_age_seconds:
            return True
        last_size = stat.st_size
        time.sleep(0.2)
    return False


def _shoot(scanlight, camera, args, ch, level):
    rgb = [0, 0, 0]
    rgb[CHANNEL_SLOT[ch]] = level
    scanlight.set_color(*rgb, 0, 0, 255)
    time.sleep(args.stabilize)

    try:
        before = {p.name for p in Path(args.watch_dir).iterdir() if p.suffix.upper() == '.ARW'}
    except FileNotFoundError:
        before = set()

    args.cam.trigger_and_wait(camera, timeout_ms=args.shutter_timeout_ms, dry_run=args.dry_run)

    if args.dry_run:
        return None
    filename = wait_for_new_file(args.watch_dir, before, timeout=args.capture_wait)
    if filename is None:
        print(f"  WARNING: no file appeared for channel {ch}.")
        return None
    path = Path(args.watch_dir) / filename
    # wait_for_new_file returns as soon as the name appears, which for a tether
    # writing in place is before the ~100MB raw is finished. Settle here so every
    # consumer — calibration, flats, and the capture loop alike — reads a complete
    # file, and so only one new ARW exists before the next channel fires.
    if not wait_for_settle(path):
        print(f"  WARNING: {filename} was still being written after the settle timeout — "
              f"skipping it rather than reading a partial raw.")
        return None
    return path


def _require_shot(scanlight, camera, args, ch, level, what):
    """_shoot for the calibration path, where a missed frame is fatal — better a
    clear message than a None deref several frames deep in rawpy."""
    path = _shoot(scanlight, camera, args, ch, level)
    if path is None and not args.dry_run:
        sys.exit(
            f"Calibration aborted: no usable {what} frame for channel {ch}.\n"
            f"Check the tether and that captures are landing in {args.watch_dir}.")
    return path


def _measure_channel_level(path, ch, flats):
    raw = read_raw(path)
    plane = extract_led_channel_plane(raw['image'], raw['pattern'], CHANNEL_BAYER_INDICES[ch],
                                       raw['black_level_per_channel'])
    plane = crop_half_res(plane, raw['sizes'])
    if flats:
        plane = plane / flats[ch]
    return measure_leader_level(plane), raw


# --------------------------------------------------------------------------
# Calibration
# --------------------------------------------------------------------------

def _probe_flat_power(scanlight, camera, args, ch):
    """
    Shoot one bare-light probe and scale LED power so the flat lands near
    FLAT_TARGET of usable range.

    Flats are captured before the exposure pass has set a shutter speed — they
    have to be, since the leader readings that drive that pass are themselves
    flat-corrected. So LED power is the lever here, not shutter. Direct ratio,
    no search loop: LED output is roughly linear with drive current.
    """
    path = _require_shot(scanlight, camera, args, ch, args.flat_brightness, 'flat-field probe')
    if args.dry_run:
        return args.flat_brightness

    raw = read_raw(path)
    level = flat_level(raw['image'], raw['pattern'], CHANNEL_BAYER_INDICES[ch],
                       raw['black_level_per_channel'], raw['sizes'], raw['white_level'])
    if level <= 0:
        sys.exit(f"Flat probe for channel {ch} came back black — check the LED "
                 f"and that the film holder is off.")

    power = max(1, min(255, round(args.flat_brightness * FLAT_TARGET / level)))
    print(f"  [{ch}] probe at power {args.flat_brightness} read {level:.0%} of usable "
          f"range -> shooting flats at power {power}")
    return power


def capture_flats(scanlight, camera, args, session_dir):
    """
    Average several exposures per channel with the holder off (bare light) to
    build per-channel flat-field maps. Same aperture and distance as scanning.

    Exposure is set per channel from a probe frame rather than assumed: a clipped
    flat under-corrects falloff across the whole roll and a dark one divides its
    read noise into every frame, and neither announces itself in the output.
    """
    print("=== Flat fields ===")
    print("Remove the film holder — bare light only — before continuing.\n")
    flats_dir = session_dir / 'flats'
    flats_dir.mkdir(parents=True, exist_ok=True)

    flat_paths = {}
    for ch in CHANNELS:
        power = _probe_flat_power(scanlight, camera, args, ch)
        print(f"[{ch}] Shooting {args.flat_shots} flat exposures at power {power}...")
        raws, pattern, black, sizes, white_level = [], None, None, None, None
        for _ in range(args.flat_shots):
            path = _require_shot(scanlight, camera, args, ch, power, 'flat-field')
            if args.dry_run:
                continue
            raw = read_raw(path)
            raws.append(raw['image'])
            pattern, black, sizes, white_level = (raw['pattern'], raw['black_level_per_channel'],
                                                  raw['sizes'], raw['white_level'])

        if args.dry_run:
            continue

        flat = build_channel_flat(raws, pattern, CHANNEL_BAYER_INDICES[ch], black, sizes, white_level)
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
        path = _require_shot(scanlight, camera, args, ch, args.start_power, 'channel-balance')
        if args.dry_run:
            levels[ch] = 1.0
            continue
        level, _ = _measure_channel_level(path, ch, flats)
        levels[ch] = level
        print(f"  [{ch}] leader level = {level:.1f} at power {args.start_power}")

    weakest = min(levels.values())
    powers = {
        ch: args.start_power if levels[ch] == weakest
        else max(1, min(255, round(args.start_power * weakest / levels[ch])))
        for ch in CHANNELS
    }
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
    white_level = black0 = None

    for attempt in range(1, args.max_exposure_iterations + 1):
        peaks = {}
        for ch in CHANNELS:
            path = _require_shot(scanlight, camera, args, ch, powers[ch], 'exposure-targeting')
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
            return args.cam.get_shutter_speed(camera), peaks

        current = shutter_str_to_seconds(args.cam.get_shutter_speed(camera))
        choices = args.cam.get_shutter_choices(camera)
        new_shutter = nearest_shutter_choice(choices, current * ratio)
        print(f"  Adjusting shutter -> {new_shutter}\n")
        args.cam.set_shutter_speed(camera, new_shutter, dry_run=args.dry_run)

    print(f"  Did not fully converge after {args.max_exposure_iterations} iterations — "
          f"accepting current shutter speed (discrete steps are coarse).\n")
    return args.cam.get_shutter_speed(camera), peaks


def run_calibration(scanlight, camera, args, state, session_dir):
    if not args.skip_warmup and not args.dry_run:
        print(f"Warming up LEDs for {args.warmup_seconds:.0f}s...")
        time.sleep(args.warmup_seconds)
        print()

    if args.skip_flats and state.get('flats'):
        flat_paths = {ch: Path(p) for ch, p in state['flats'].items()}
    else:
        flat_paths = capture_flats(scanlight, camera, args, session_dir)
        session_state.set_flats(state, flat_paths)
        if not args.dry_run:
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

    session_state.set_calibration(state, powers, shutter, peaks)
    if not args.dry_run:
        session_state.save_session(session_dir, state)
        print(f"Saved calibration to {session_dir / 'session.json'}\n")

    return flats


# --------------------------------------------------------------------------
# Capture and merge
# --------------------------------------------------------------------------

def capture_frame(scanlight, camera, args, levels):
    """Fire R, G, B in sequence and return {ch: Path} once all three have landed
    and settled on disk (settling happens in _shoot), or a partial dict if a
    channel failed."""
    captured = {}
    for ch in CHANNELS:
        path = _shoot(scanlight, camera, args, ch, levels[ch])
        if path is not None:
            captured[ch] = path
    return captured


def run_capture_loop(scanlight, camera, args, state, flats, levels):
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


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--session-dir', metavar='DIR', help='Session folder (default: --resume the last one)')
    parser.add_argument('--resume', action='store_true', help='Resume the most recently used session')
    parser.add_argument('--watch-dir', required=True, metavar='DIR', help='Capture One session capture folder')
    parser.add_argument('--output-dir', metavar='DIR',
                        help='Where merged TIFFs are written (default: --watch-dir)')
    parser.add_argument('--film-stock', default='', help='Film stock name, recorded in session state')
    parser.add_argument('--roll-id', default='', help='Roll identifier, recorded in session state')
    parser.add_argument('--date', default=None, help='Session date (default: today)')
    parser.add_argument('--port', help='Scanlight serial port (default: auto-detect)')
    parser.add_argument('--camera-backend', choices=sorted(CAMERA_BACKENDS), default='captureone',
                        help='How the shutter is fired (default: captureone)')
    parser.add_argument('--recalibrate', action='store_true', help='Force recalibration even if already calibrated')
    parser.add_argument('--dry-run', action='store_true', help='Print actions without touching hardware')

    calib = parser.add_argument_group('calibration tuning', 'Defaults are usually fine.')
    calib.add_argument('--start-power', type=int, default=200, metavar='N',
                       help='Starting LED power 0-255 for channel balance (default: 200)')
    calib.add_argument('--flat-brightness', type=int, default=180, metavar='N',
                       help='Starting LED brightness 0-255 for the flat-field probe, which '
                            'then scales it to hit the target exposure (default: 180)')
    calib.add_argument('--flat-shots', type=int, default=6, metavar='N',
                       help='Exposures averaged per channel for flat fields (default: 6)')
    calib.add_argument('--skip-flats', action='store_true', help='Reuse existing flats from session dir')
    calib.add_argument('--warmup-seconds', type=float, default=300, metavar='N',
                       help='LED warm-up wait before calibrating (default: 300)')
    calib.add_argument('--skip-warmup', action='store_true', help='Skip the LED warm-up wait')
    calib.add_argument('--max-exposure-iterations', type=int, default=4, metavar='N',
                       help='Cap on shutter-speed refinement iterations (default: 4)')

    timing = parser.add_argument_group('capture timing', 'Defaults are usually fine.')
    timing.add_argument('--stabilize', type=float, default=0.15, metavar='N',
                        help='LED settle + stand vibration delay before triggering (default: 0.15)')
    timing.add_argument('--capture-wait', type=float, default=8.0, metavar='N',
                        help='Max seconds to wait for the ARW to land in --watch-dir (default: 8.0)')
    timing.add_argument('--shutter-timeout-ms', type=int, default=8000, metavar='N',
                        help='Max ms to wait for capture confirmation, gphoto2 backend only (default: 8000)')
    timing.add_argument('--preview-brightness', type=int, default=32, metavar='N',
                        help='White-equivalent (R=G=B) LED level while advancing film (default: 32)')

    parser.add_argument('--dry-run-shutter', default='1/60', help=argparse.SUPPRESS)
    args = parser.parse_args()

    if not args.session_dir and not args.resume:
        parser.error("one of --session-dir or --resume is required")

    if not args.output_dir:
        args.output_dir = args.watch_dir

    args.cam = CAMERA_BACKENDS[args.camera_backend]

    is_new_session = args.session_dir and not (Path(args.session_dir) / 'session.json').exists()
    if is_new_session:
        session_dir = Path(args.session_dir)
        state = session_state.new_session(session_dir, args.film_stock, args.roll_id, args.date)
    else:
        session_dir = session_state.resolve_resume_dir(args.session_dir)
        state = session_state.load_session(session_dir)
    args.session_dir = session_dir

    for field, value in (('film_stock', args.film_stock), ('roll_id', args.roll_id), ('date', args.date)):
        if value:
            state[field] = value
    args.roll_id = state['roll_id']

    needs_calibration = is_new_session or args.recalibrate or not state.get('calibration')
    if not needs_calibration:
        age = session_state.calibration_age_seconds(state)
        if age is not None and age > session_state.STALE_SECONDS:
            session_state.warn_if_stale(state)
            if args.dry_run:
                needs_calibration = False
            else:
                answer = input("Recalibrate now? [y/N] ").strip().lower()
                needs_calibration = answer == 'y'

    if args.dry_run:
        scanlight = Scanlight(None, dry_run=True)
        camera = None
        print("=== DRY RUN — no hardware will be touched ===\n")
    else:
        port = args.port or find_scanlight_port()
        print(f"Connecting to Scanlight on {port}...")
        scanlight = Scanlight(port, dry_run=False)
        print(f"Connecting to camera via {args.camera_backend}...")
        camera = args.cam.open_camera()
        print("Connected.\n")

    try:
        if needs_calibration:
            flats = run_calibration(scanlight, camera, args, state, session_dir)
        else:
            flats = None
            if state.get('flats') and not args.dry_run:
                flats = {ch: np.load(p) for ch, p in state['flats'].items()}
            if not args.dry_run:
                args.cam.set_shutter_speed(camera, state['calibration']['shutter_speed'])

        levels = state['calibration']['channel_levels']
        print(f"Session: {session_dir}  film={state['film_stock']}  roll={state['roll_id']}")
        print(f"Channel levels: {levels}  shutter: {state['calibration']['shutter_speed']}\n")

        run_capture_loop(scanlight, camera, args, state, flats, levels)
    except KeyboardInterrupt:
        print("\nInterrupted — turning off LEDs.")
        failed = session_state.failed_frames(state)
        if failed:
            print(f"{len(failed)} frame(s) need redoing: {[f['frame'] for f in failed]}")
    finally:
        pb = args.preview_brightness
        scanlight.set_color(pb, pb, pb, 0, 0, 255)
        scanlight.close()
        args.cam.close_camera(camera)


if __name__ == '__main__':
    main()
