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
from .lib.flatfield import FLAT_MAX, FLAT_TARGET, build_channel_flat, flat_level
from .lib.leader import measure_leader_level
from .lib.merge_tri import merge_triplet
from .lib.rawio import crop_half_res, extract_led_channel_plane, read_raw, verify_channel
from .lib.scanner import CHANNEL_BAYER_INDICES, Scanlight, find_scanlight_port, wait_for_new_file
from .lib.shutter import nearest_shutter_choice, shutter_str_to_seconds

CHANNELS = 'RGB'
# Which of the Scanlight's r/g/b slots each channel drives; the others stay at 0.
CHANNEL_SLOT = {'R': 0, 'G': 1, 'B': 2}
TARGET_LOW, TARGET_HIGH = 0.80, 0.90
SETTLE_SECONDS = 1.0
# How long each channel holds during warm-up. Roughly one capture's dwell, so the
# board sees the same on/off rhythm it will during the roll.
WARMUP_DWELL_SECONDS = 3.0
# How many times the flat probe may back off before giving up. Each attempt halves LED
# power, so four covers a 16x overshoot — well past anything the rig has shown.
MAX_FLAT_PROBES = 4
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


def light_channel(scanlight, ch, level):
    """Turn on exactly one narrowband channel; the other two go dark."""
    rgb = [0, 0, 0]
    rgb[CHANNEL_SLOT[ch]] = level
    scanlight.set_color(*rgb, 0, 0, 255)


def set_preview_light(scanlight, args):
    """Low white-equivalent level for eyeballing the frame while advancing film.
    Without this the light sits on whichever channel happened to fire last."""
    pb = args.preview_brightness
    scanlight.set_color(pb, pb, pb, 0, 0, 255)


def _shoot(scanlight, camera, args, ch, level):
    light_channel(scanlight, ch, level)
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


def _read_verified(path, ch, what):
    """read_raw for the calibration path, refusing a frame that wasn't lit by the LED
    we think it was.

    Fatal here where the capture loop only loses a frame: calibration sets the channel
    balance and shutter speed for the whole roll, so a mis-attributed frame doesn't
    spoil one image, it quietly mis-exposes every one that follows."""
    raw = read_raw(path)
    try:
        verify_channel(raw, ch, CHANNEL_BAYER_INDICES)
    except ValueError as exc:
        sys.exit(f"Calibration aborted on the {what} frame for channel {ch}: {exc}")
    return raw


def _measure_channel_level(path, ch, flats):
    raw = _read_verified(path, ch, 'leader-level')
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
    Probe with bare light and pick the LED power that lands the flat near FLAT_TARGET
    of usable range. Returns (power, led_at_max) — the second half says the ratio wanted
    more than the 255 maximum and was clamped, which is what decides whether "turn the
    light up" is still advice the operator can act on.

    Flats are captured before the exposure pass has set a shutter speed — they have to
    be, since the leader readings that drive that pass are themselves flat-corrected. So
    LED power is the lever here, not shutter, and LED output is roughly linear with
    drive current, which makes the correction a direct ratio rather than a search.

    The loop exists only because that ratio needs an *unclipped* reading to be worth
    anything. A saturated frame reports 100% of usable range however far past full scale
    it truly is, so scaling from it always under-corrects — the first green probe on real
    hardware came back 99.9% clipped at power 180, reported 100%, and yielded power 126
    when ~92 was needed. Backing off and looking again costs one frame and is the only
    way to recover the real number.
    """
    power = args.flat_brightness
    for attempt in range(1, MAX_FLAT_PROBES + 1):
        path = _require_shot(scanlight, camera, args, ch, power, 'flat-field probe')
        if args.dry_run:
            return power, False

        raw = _read_verified(path, ch, 'flat-field probe')
        level = flat_level(raw['image'], raw['pattern'], CHANNEL_BAYER_INDICES[ch],
                           raw['black_level_per_channel'], raw['sizes'], raw['white_level'])
        if level <= 0:
            sys.exit(f"Flat probe for channel {ch} came back black — check the LED "
                     f"and that the film holder is off.")

        if level < FLAT_MAX:
            break

        # At or above the level a finished flat would be rejected at, the reading is
        # pinned near full scale and its ratio cannot be trusted. Halve rather than
        # scale by it: the true level may be any multiple of what was reported.
        if power <= 1:
            sys.exit(f"Flat probe for channel {ch} is saturated even at minimum LED "
                     f"power. Stop down the aperture or shorten the shutter.")
        print(f"  [{ch}] probe at power {power} reads {level:.0%} — saturated, "
              f"halving to {max(1, power // 2)} and re-probing")
        power = max(1, power // 2)
    else:
        sys.exit(f"Flat probe for channel {ch} still saturated after {MAX_FLAT_PROBES} "
                 f"attempts. Stop down the aperture or shorten the shutter.")

    wanted = round(power * FLAT_TARGET / level)
    chosen = max(1, min(255, wanted))
    print(f"  [{ch}] probe at power {power} read {level:.0%} of usable "
          f"range -> shooting flats at power {chosen}")
    if wanted > 255:
        # The LED is already at maximum and the channel is still short of target. The
        # flat is usable as long as build_channel_flat accepts it, but say so, because
        # the operator's only remaining levers are aperture and shutter.
        print(f"  [{ch}] NOTE: hitting {FLAT_TARGET:.0%} would need power {wanted}, above "
              f"the 255 maximum. The flat will land near {level * 255 / power:.0%} of "
              f"usable range — legal but dim. Open up or slow the shutter (--flat-shutter) "
              f"to do better.")
    return chosen, wanted > 255


def _flat_shutter_speed(args, camera):
    """The shutter the flats are actually being shot at, for the session record.

    Best effort on purpose: this is provenance, so a backend that won't report it
    costs a line in session.json, never the roll.
    """
    if args.dry_run:
        return None
    try:
        return args.cam.get_shutter_speed(camera)
    except Exception as e:
        print(f"  NOTE: could not read the shutter speed for the flats' record ({e}).")
        return None


def capture_flats(scanlight, camera, args, session_dir):
    """
    Average several exposures per channel with the holder off (bare light) to
    build per-channel flat-field maps. Same aperture and distance as scanning.

    Exposure is set per channel from a probe frame rather than assumed: a clipped
    flat under-corrects falloff across the whole roll and a dark one divides its
    read noise into every frame, and neither announces itself in the output.

    The shutter is left wherever the camera has it unless --flat-shutter says
    otherwise, and that is not a compromise: a flat is peak-normalized, so only its
    *shape* is ever applied, and shape is illumination falloff times lens vignetting —
    neither depends on shutter speed. The flats' shutter is therefore free to differ
    from the one the exposure pass later settles on. What it does decide is how well
    exposed the flat is, which is why --flat-shutter exists at all: when the LED clamps
    at 255 and the flat still comes back dim (red needs ~595 on the real rig), a slower
    shutter is the lever that remains. Returns (flat_paths, shutter_speed).
    """
    print("=== Flat fields ===")
    if args.flat_shutter:
        print(f"Setting shutter speed to {args.flat_shutter} for the flat pass.")
        args.cam.set_shutter_speed(camera, args.flat_shutter, dry_run=args.dry_run)
    # Actually wait. Said "before continuing" and then continued, so the flats were shot
    # through the holder — baking its vignetting and edges into the correction applied to
    # every frame of the roll, which looks like a plausible flat and is not one.
    print("Remove the film holder — bare light only — then press Enter...")
    if not args.dry_run:
        input()
    print()
    flats_dir = session_dir / 'flats'
    flats_dir.mkdir(parents=True, exist_ok=True)

    shutter_speed = _flat_shutter_speed(args, camera)
    if shutter_speed:
        print(f"Shooting flats at shutter speed {shutter_speed}.")

    flat_paths = {}
    for ch in CHANNELS:
        power, led_at_max = _probe_flat_power(scanlight, camera, args, ch)
        print(f"[{ch}] Shooting {args.flat_shots} flat exposures at power {power}...")
        raws, pattern, black, sizes, white_level = [], None, None, None, None
        for _ in range(args.flat_shots):
            path = _require_shot(scanlight, camera, args, ch, power, 'flat-field')
            if args.dry_run:
                continue
            raw = _read_verified(path, ch, 'flat-field')
            raws.append(raw['image'])
            pattern, black, sizes, white_level = (raw['pattern'], raw['black_level_per_channel'],
                                                  raw['sizes'], raw['white_level'])

        if args.dry_run:
            continue

        flat = build_channel_flat(raws, pattern, CHANNEL_BAYER_INDICES[ch], black, sizes,
                                  white_level, led_at_max=led_at_max)
        flat_path = flats_dir / f'{ch}.npy'
        np.save(flat_path, flat)
        flat_paths[ch] = flat_path
        print(f"[{ch}] Flat saved -> {flat_path}\n")

    return flat_paths, shutter_speed


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

        reported = args.cam.get_shutter_speed(camera)
        current = shutter_str_to_seconds(reported)
        if current is None or current <= 0:
            sys.exit(f"exposure-targeting: camera reports shutter speed {reported!r}, which "
                     "isn't a duration to scale from. Take it off Bulb and re-run calibration.")
        choices = args.cam.get_shutter_choices(camera)
        new_shutter = nearest_shutter_choice(choices, current * ratio)
        if new_shutter is None:
            sys.exit(f"exposure-targeting: no usable shutter speed among the {len(choices)} the "
                     f"camera offers ({', '.join(map(str, choices[:6]))}...). Cannot target "
                     "exposure; calibration aborted rather than scanning at the wrong one.")
        print(f"  Adjusting shutter -> {new_shutter}\n")
        args.cam.set_shutter_speed(camera, new_shutter, dry_run=args.dry_run)

    print(f"  Did not fully converge after {args.max_exposure_iterations} iterations — "
          f"accepting current shutter speed (discrete steps are coarse).\n")
    return args.cam.get_shutter_speed(camera), peaks


def warm_up(scanlight, args, dwell=WARMUP_DWELL_SECONDS):
    """
    Run the LEDs at the load they'll actually see while scanning, so calibration
    lands on the thermal steady state the roll will run at.

    Cycling one channel at a time is the substance of this, not a detail. Scanning
    only ever has a single narrowband LED on, so warming all three together — or
    any of them at full power — settles the board hotter than it will ever get in
    use, and calibrating against that overshoot means the light cools toward its
    real operating point over the roll. That is the same drift warm-up exists to
    prevent, just with the sign flipped. `--start-power` is the closest available
    stand-in for the scanning power, since it's what pass 1 is about to shoot at
    and the calibrated powers only go down from there.
    """
    print(f"Warming up LEDs for {args.warmup_seconds:.0f}s — cycling R/G/B at power "
          f"{args.start_power}, matching the load while scanning.")
    deadline = time.monotonic() + args.warmup_seconds
    next_report = time.monotonic() + 30

    while True:
        for ch in CHANNELS:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                print()
                return
            light_channel(scanlight, ch, args.start_power)
            if time.monotonic() >= next_report:
                print(f"  {remaining:.0f}s remaining...")
                next_report += 30
            time.sleep(min(dwell, remaining))


def run_calibration(scanlight, camera, args, state, session_dir):
    if not args.skip_warmup and not args.dry_run:
        warm_up(scanlight, args)

    if args.skip_flats and state.get('flats'):
        flat_paths = {ch: Path(p) for ch, p in state['flats'].items()}
    else:
        flat_paths, flat_shutter = capture_flats(scanlight, camera, args, session_dir)
        session_state.set_flats(state, flat_paths, flat_shutter)
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
        set_preview_light(scanlight, args)
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
            peaks = merge_triplet(captured['R'], captured['G'], captured['B'], flats, output_path,
                                  meta, full_resolution=args.resolution == 'full')
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

def build_parser():
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
    parser.add_argument('--resolution', choices=('full', 'half'), default='full',
                        help='full interpolates each channel from its measured sites to the '
                             'sensor\'s full pixel count; half emits one pixel per measured '
                             'photosite, interpolating nothing (default: full)')
    parser.add_argument('--recalibrate', action='store_true', help='Force recalibration even if already calibrated')
    parser.add_argument('--dry-run', action='store_true', help='Print actions without touching hardware')

    calib = parser.add_argument_group('calibration tuning', 'Defaults are usually fine.')
    calib.add_argument('--start-power', type=int, default=200, metavar='N',
                       help='LED power 0-255 for the warm-up cycle and the channel-balance '
                            'pass; balance only scales down from here (default: 200)')
    calib.add_argument('--flat-brightness', type=int, default=180, metavar='N',
                       help='Starting LED brightness 0-255 for the flat-field probe, which '
                            'then scales it to hit the target exposure (default: 180)')
    calib.add_argument('--flat-shots', type=int, default=6, metavar='N',
                       help='Exposures averaged per channel for flat fields (default: 6)')
    calib.add_argument('--flat-shutter', metavar='SPEED',
                       help='Shutter speed (e.g. 1/8) to set before the flat pass; flats are '
                            'shot before a scanning shutter exists, and only the flat\'s shape '
                            'is used, so it may differ from it. Use when the LED clamps at 255 '
                            'and the flat is still dim (default: leave the camera alone)')
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
    return parser


def main():
    parser = build_parser()
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
        set_preview_light(scanlight, args)
        scanlight.close()
        args.cam.close_camera(camera)


if __name__ == '__main__':
    main()
