"""
Shared hardware utilities: Scanlight serial control, Capture One triggering,
ARW file watching, and raw channel sampling.
"""

import glob
import os
import subprocess
import sys
import time

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    sys.exit("pyserial not installed. Run: pip install trichrom[dev]")

BAUD_RATE = 115200
PACKET_START = 0xFE
PKT_H2D_SET_COLOR = 0x00

CAPTURE_ONE_APP = "Capture One"

BAYER_INDEX = {'R': 0, 'G': 1, 'B': 2}
OFF = (0, 0, 0, 0, 0)
CLIP_THRESHOLD = 14000
PROBE_BRIGHTNESS = 50


def _build_set_color_packet(r, g, b, w, ir, brightness):
    scale = brightness / 255
    values = [int(v * scale) for v in (r, g, b, w, ir)]
    data = bytes(values) + b'\x00'
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


class Scanlight:
    def __init__(self, port, dry_run=False):
        self.dry_run = dry_run
        if dry_run:
            self._ser = None
        else:
            self._ser = serial.Serial(port, BAUD_RATE, timeout=1)
            time.sleep(0.5)

    def set_color(self, r, g, b, w, ir, brightness):
        packet = _build_set_color_packet(r, g, b, w, ir, brightness)
        if self.dry_run:
            print(f"  [dry-run] serial write: {packet.hex()}")
        else:
            self._ser.write(packet)
            self._ser.flush()

    def close(self):
        if self._ser:
            time.sleep(1.0)
            self._ser.close()
            self._ser = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.set_color(*OFF, 255)
        self.close()


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
            after = set(f for f in os.listdir(watch_dir) if f.upper().endswith('.ARW') and not f.startswith('.'))
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


def _load_channel_patch(path, ch_idx):
    """Extract the center 174x174 Bayer channel patch, black-level subtracted."""
    try:
        import rawpy
        import numpy as np
    except ImportError:
        sys.exit("rawpy and numpy are required. Run: pip install trichrom[dev]")

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
    channel_data = channel_data[h // 2 - half:h // 2 + half, w // 2 - half:w // 2 + half]
    return channel_data - black[ch_idx]


def sample_channel_median(path, ch_idx):
    """
    Trimmed median (5th–95th percentile) of the center 174x174 Bayer channel patch.
    Use for filmbase neutrals — robust to dust and bright defects.
    """
    import numpy as np
    data = _load_channel_patch(path, ch_idx)
    lo, hi = np.percentile(data, [5, 95])
    trimmed = data[(data >= lo) & (data <= hi)]
    return float(np.median(trimmed))


def sample_channel_max(path, ch_idx, percentile=99):
    """
    99th-percentile of the center 174x174 Bayer channel patch.
    Use for clipping detection — matches RawDigger MAX-with-0%-OvExp behavior.
    """
    import numpy as np
    data = _load_channel_patch(path, ch_idx)
    return float(np.percentile(data, percentile))
