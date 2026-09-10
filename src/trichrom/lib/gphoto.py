"""
Camera control via gphoto2: tethered shutter trigger and shutter-speed config.

An alternative to the Capture One backend, for driving the camera directly. Note
that Sony's PC Remote connection allows one controlling host at a time, so this
generally cannot run alongside a Capture One tether — and gphoto2's Sony support is
reverse-engineered per body (there is an unresolved capture failure reported against
the ILCE-7RM5). Prefer the captureone backend unless it proves unworkable.

Fixed sleeps are avoided: they're either too slow (wasted time every frame) or too
fast (the classic source of truncated/corrupt captures).
"""

import sys
import time

try:
    import gphoto2 as gp
except ImportError:
    gp = None


def open_camera():
    if gp is None:
        sys.exit("python-gphoto2 not installed. Run: pip install trichrom[dev]")
    camera = gp.Camera()
    camera.init()
    return camera


def close_camera(camera):
    if camera is None:
        return
    try:
        camera.exit()
    except Exception:
        pass


def trigger_and_wait(camera, timeout_ms=8000, dry_run=False):
    """Trigger a capture and block until the camera confirms it wrote the file."""
    if dry_run:
        print("  [dry-run] gphoto2: trigger_capture")
        return
    camera.trigger_capture()
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
        event_type, _ = camera.wait_for_event(remaining_ms)
        if event_type in (gp.GP_EVENT_CAPTURE_COMPLETE, gp.GP_EVENT_FILE_ADDED):
            return
    sys.exit(f"Camera did not confirm capture within {timeout_ms}ms.")


def _shutter_widget(camera):
    config = camera.get_config()
    widget = config.get_child_by_name('shutterspeed')
    return config, widget


def get_shutter_choices(camera):
    """Return the shutter-speed strings the camera supports, in the camera's order."""
    _, widget = _shutter_widget(camera)
    return [widget.get_choice(i) for i in range(widget.count_choices())]


def get_shutter_speed(camera):
    _, widget = _shutter_widget(camera)
    return widget.get_value()


def set_shutter_speed(camera, value, dry_run=False):
    if dry_run:
        print(f"  [dry-run] gphoto2: set shutterspeed={value}")
        return
    config, widget = _shutter_widget(camera)
    widget.set_value(value)
    camera.set_config(config)
