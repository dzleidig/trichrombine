"""
Camera control through Capture One's AppleScript interface.

Capture One owns the camera outright here: it holds the tether for live view and
focus magnification, writes the ARWs into the capture folder, and fires the shutter
on our behalf. Sony's PC Remote connection allows one controlling host at a time, so
triggering through Capture One rather than alongside it is what keeps live view
available — driving the camera directly (gphoto2, CRSDK) means giving that up.

Capture One's capture command is asynchronous, so triggering only confirms that C1
accepted the command. The signal that actually matters for merging is the ARW landing
in the capture folder, which the caller waits for.
"""

import subprocess
import sys

from .shutter import STANDARD_CHOICES

APP = "Capture One"


def _osascript(script):
    """Run an AppleScript snippet and return its stripped stdout."""
    result = subprocess.run(['osascript', '-e', script], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"osascript failed: {script}")
    return result.stdout.strip()


def _tell(body):
    return f'tell application "{APP}" to {body}'


def _applescript_safe(value):
    """Guard against breaking out of the quoted AppleScript string literal."""
    if '"' in value or '\\' in value:
        raise ValueError(f"Unsafe shutter value: {value!r}")
    return value


def open_camera():
    """Confirm Capture One has a document open with a camera attached."""
    try:
        document = _osascript(_tell('name of current document'))
    except RuntimeError as e:
        sys.exit(f"Capture One is not ready: {e}\nOpen your session document and try again.")
    try:
        _osascript(_tell('shutter speed of camera of current document'))
    except RuntimeError:
        sys.exit(
            f"Capture One has no camera attached to '{document}'.\n"
            "Connect and tether the camera, then try again."
        )
    print(f"Capture One document: {document}")
    return APP


def close_camera(camera):
    """Capture One keeps owning the camera after we're done — nothing to release."""


def trigger_and_wait(camera, timeout_ms=8000, dry_run=False):
    """Fire the shutter via Capture One. timeout_ms is accepted for backend parity
    but unused: C1's capture returns immediately and the caller waits for the file."""
    script = _tell('capture')
    if dry_run:
        print(f"  [dry-run] osascript: {script}")
        return
    try:
        _osascript(script)
    except RuntimeError as e:
        sys.exit(f"Capture One refused the capture command: {e}")


def get_shutter_speed(camera):
    return _osascript(_tell('shutter speed of camera of current document'))


def get_shutter_choices(camera):
    """The shutter speeds Capture One reports for the attached camera, falling back
    to the standard ladder if this version doesn't expose the list."""
    try:
        raw = _osascript(_tell('available shutter speeds of camera of current document'))
    except RuntimeError:
        return list(STANDARD_CHOICES)
    choices = [c.strip() for c in raw.split(',') if c.strip()]
    return choices or list(STANDARD_CHOICES)


def set_shutter_speed(camera, value, dry_run=False):
    """
    Set the camera's shutter speed through Capture One.

    Whether C1 exposes this property as writable is undocumented and may vary by
    body, so a refused write falls back to asking the operator to dial it in. That
    only happens during calibration, never in the per-frame capture path.
    """
    script = _tell(f'set shutter speed of camera of current document to "{_applescript_safe(value)}"')
    if dry_run:
        print(f"  [dry-run] osascript: {script}")
        return
    try:
        _osascript(script)
    except RuntimeError as e:
        print(f"  Capture One would not set the shutter speed ({e}).")
        input(f"  Set the camera to {value} by hand, then press Enter...")
