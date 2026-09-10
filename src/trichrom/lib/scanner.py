"""
Shared hardware utilities: Scanlight serial control and ARW file watching.
Camera control lives in the backend modules (captureone, gphoto).
"""

import glob
import os
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

# Bayer channel indices contributing to each LED channel: green averages both
# green photosite positions (1=G, 3=G2 in RGGB) for full-resolution-matched planes.
CHANNEL_BAYER_INDICES = {'R': (0,), 'G': (1, 3), 'B': (2,)}
OFF = (0, 0, 0, 0, 0)


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
