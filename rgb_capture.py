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
import glob
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    sys.exit("pyserial not installed. Run: pip3 install pyserial")

BAUD_RATE = 115200
PACKET_START = 0xFE
PKT_H2D_SET_COLOR = 0x00

CAPTURE_ONE_APP = "Capture One"

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

BAYER_INDEX = {'R': 0, 'G': 1, 'B': 2}

OFF = (0, 0, 0, 0, 0)


def build_set_color_packet(r, g, b, w, ir, brightness):
    scale = brightness / 255
    values = [int(v * scale) for v in (r, g, b, w, ir)]
    data = bytes(values) + b'\x00'  # save_preset = 0
    return bytes([PACKET_START, PKT_H2D_SET_COLOR, len(data)]) + data


def find_scanlight_port():
    candidates = []
    for port in serial.tools.list_ports.comports():
        desc = (port.description or '').lower()
        mfr = (port.manufacturer or '').lower()
        if any(k in desc or k in mfr for k in ('pico', 'rp2', 'raspberry', 'cdc')):
            candidates.append(port.device)
    if not candidates:
        candidates = sorted(glob.glob('/dev/cu.usbmodem*'))
    if not candidates:
        sys.exit(
            "Could not auto-detect scanlight serial port.\n"
            "Connect the scanlight and retry, or pass --port /dev/cu.usbmodemXXXX"
        )
    if len(candidates) > 1:
        print(f"Multiple serial ports found: {candidates}")
        print(f"Using {candidates[0]} — pass --port to override.")
    return candidates[0]


def set_color(ser, r, g, b, w, ir, brightness, dry_run):
    packet = build_set_color_packet(r, g, b, w, ir, brightness)
    if dry_run:
        print(f"  [dry-run] serial write: {packet.hex()}")
    else:
        ser.write(packet)
        ser.flush()


def trigger_capture(dry_run):
    script = f'tell application "{CAPTURE_ONE_APP}" to capture'
    if dry_run:
        print(f"  [dry-run] osascript: {script}")
        return True
    result = subprocess.run(
        ['osascript', '-e', script],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"  WARNING: AppleScript error: {result.stderr.strip()}")
        return False
    return True


def wait_for_new_file(watch_dir, before, timeout):
    """Wait for a new ARW file to appear in watch_dir. Returns the filename or None on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            after = set(f for f in os.listdir(watch_dir) if f.upper().endswith('.ARW') and not f.startswith('.') and not f.startswith('.'))
        except FileNotFoundError:
            time.sleep(0.1)
            continue
        new = after - before
        if new:
            name = next(iter(new))
            print(f"  File landed: {name}")
            return name
        time.sleep(0.1)
    print(f"  WARNING: no new file after {timeout}s, continuing anyway.")
    return None


def sample_channel_median(path, ch_idx):
    """
    Read an ARW and return the median pixel value for the given Bayer channel index
    sampled from the center 175x175 camera-pixel region, after black level subtraction.
    """
    try:
        import rawpy
        import numpy as np
    except ImportError:
        sys.exit("rawpy and numpy are required for --calibrate. Run: pip3 install rawpy numpy")

    with rawpy.imread(str(path)) as raw:
        pattern = raw.raw_pattern.copy()
        image = raw.raw_image.copy().astype(np.float32)
        sizes = raw.sizes
        black = raw.black_level_per_channel

    positions = (pattern == ch_idx).nonzero()
    row, col = int(positions[0][0]), int(positions[1][0])
    channel_data = image[row::2, col::2]

    top = sizes.top_margin // 2
    left = sizes.left_margin // 2
    h = sizes.height // 2
    w = sizes.width // 2
    channel_data = channel_data[top:top + h, left:left + w]

    half = 87
    ch_center = h // 2
    cw_center = w // 2
    channel_data = channel_data[ch_center - half:ch_center + half, cw_center - half:cw_center + half]

    channel_data = channel_data - black[ch_idx]
    return float(np.median(channel_data))


CLIP_THRESHOLD = 13000


def shoot_and_sample(ser, args, ch, brightness):
    """Set a single channel, trigger capture, wait for ARW, return (filename, median)."""
    r, g, b, w, ir = CHANNELS[ch]
    set_color(ser, r, g, b, w, ir, brightness, args.dry_run)
    time.sleep(args.stabilize)

    try:
        before = set(f for f in os.listdir(args.watch_dir) if f.upper().endswith('.ARW') and not f.startswith('.'))
    except FileNotFoundError:
        before = set()

    trigger_capture(args.dry_run)
    filename = wait_for_new_file(args.watch_dir, before, timeout=args.capture_wait)
    if filename is None:
        sys.exit(f"Calibration failed: no file appeared for {ch} channel at brightness {brightness}.")

    path = Path(args.watch_dir) / filename
    median = sample_channel_median(path, BAYER_INDEX[ch])
    return filename, median


PROBE_BRIGHTNESS = 20


def run_calibration(ser, args):
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
        set_color(ser, 255, 255, 255, 0, 0, probe_bv, args.dry_run)
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
        set_color(ser, *OFF, 255, args.dry_run)

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
            filename, median = shoot_and_sample(ser, args, ch, brightness)
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

        set_color(ser, *OFF, 255, args.dry_run)

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


def capture_sequence(ser, args, brightnesses):
    """
    Shoot R, G, B in sequence. Returns a dict {ch: Path} of captured ARW files,
    or an empty dict if watch_dir is not set.
    """
    dry_run = args.dry_run
    channels = args.channels.upper()
    captured = {}

    for ch in channels:
        name = CHANNEL_NAMES[ch]
        r, g, b, w, ir = CHANNELS[ch]
        bv = brightnesses.get(ch, args.brightness)
        print(f"[{ch}] Setting {name} channel (brightness {bv})...")
        set_color(ser, r, g, b, w, ir, bv, dry_run)

        time.sleep(args.stabilize)

        if args.watch_dir and not dry_run:
            try:
                before = set(f for f in os.listdir(args.watch_dir) if f.upper().endswith('.ARW') and not f.startswith('.'))
            except FileNotFoundError:
                before = set()

        print(f"[{ch}] Triggering capture in Capture One...")
        ok = trigger_capture(dry_run)
        if not ok:
            print(f"[{ch}] Capture may have failed — continuing anyway.")

        if args.watch_dir and not dry_run:
            print(f"[{ch}] Waiting for file in {args.watch_dir}...")
            filename = wait_for_new_file(args.watch_dir, before, timeout=args.capture_wait)
            if filename:
                captured[ch] = Path(args.watch_dir) / filename
        else:
            print(f"[{ch}] Waiting {args.capture_wait}s for transfer...")
            time.sleep(args.capture_wait)
        print(f"[{ch}] Done.\n")

    print("All channels captured. Turning off LEDs.")
    set_color(ser, *OFF, 255, dry_run)

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
    dry_run = args.dry_run

    if dry_run:
        ser = None
        print("=== DRY RUN — no hardware will be touched ===\n")
    else:
        port = args.port or find_scanlight_port()
        print(f"Connecting to scanlight on {port}...")
        ser = serial.Serial(port, BAUD_RATE, timeout=1)
        time.sleep(0.5)
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
            brightnesses, filmbase_neutrals = run_calibration(ser, args)
            set_color(ser, 0, 0, 0, args.preview_brightness, 0, 255, dry_run)

        if args.loop:
            frame = 1
            while True:
                input(f"Frame {frame} — press Enter to capture (Ctrl+C to quit)...")
                captured = capture_sequence(ser, args, brightnesses)
                if captured and output_dir:
                    combine_frame(captured, output_dir, frame, filmbase_neutrals, lcc_path)
                frame += 1
                # White preview light so the film is visible while advancing
                set_color(ser, 0, 0, 0, args.preview_brightness, 0, 255, dry_run)
        else:
            captured = capture_sequence(ser, args, brightnesses)
            if captured and output_dir:
                combine_frame(captured, output_dir, 1, filmbase_neutrals, lcc_path)

    except KeyboardInterrupt:
        print("\nInterrupted — turning off LEDs.")
        if not dry_run:
            set_color(ser, *OFF, 255, dry_run)
    finally:
        if ser:
            time.sleep(1.0)
            ser.close()


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
