"""
Camera control via gphoto2: tethered shutter trigger and shutter-speed config.

Capture One stays tethered separately for live view and file import — this module
only fires the shutter and waits for the camera's own confirmation that the
exposure completed. Fixed sleeps are avoided: they're either too slow (wasted time
every frame) or too fast (the classic source of truncated/corrupt captures).
"""

import math
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


def shutter_str_to_seconds(s):
    """Parse a gphoto2 shutter-speed string ('1/125', '0.5', '2', 'bulb') to seconds."""
    s = s.strip()
    if s.lower() == 'bulb':
        return None
    if '/' in s:
        num, den = s.split('/', 1)
        return float(num) / float(den)
    return float(s)


def nearest_shutter_choice(choices, target_seconds):
    """Pick the choice whose duration is closest to target_seconds, on a log scale
    (shutter speeds are geometric, so ratio error is what matters, not absolute)."""
    best, best_dist = None, None
    for c in choices:
        secs = shutter_str_to_seconds(c)
        if secs is None or secs <= 0:
            continue
        dist = abs(math.log(secs) - math.log(target_seconds))
        if best_dist is None or dist < best_dist:
            best_dist, best = dist, c
    return best
