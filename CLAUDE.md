# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup

```bash
pip install -e .
```

Dependencies: `rawpy`, `tifffile`, `pyexiv2`, `numpy`, `pyserial`, `scipy`. Python 3.14 (see `.tool-versions`). The optional `gphoto` extra (`pip install -e ".[gphoto]"`) adds python-gphoto2, needed only for `--camera-backend gphoto2`; it also needs `libgphoto2` on the system.

## Hardware

Sony A7R V, Sigma 105mm f/2.8 DG DN Macro (manual focus, f/8), JackW Big ScanLight
(narrowband RGB LED, RP2040) over USB serial, Negative Supply 35mm MK2 holder on a
Kaiser RS1 copy stand.

**Capture One owns the camera.** It holds the tether for live view and focus
magnification, writes the ARWs, and fires the shutter on our behalf via AppleScript.
Sony's PC Remote connection allows one controlling host at a time, so driving the
camera directly (gphoto2, CRSDK) generally means losing the live view that focusing
depends on. The ScanLight's own shutter trigger isn't usable on Sony bodies either.
`--camera-backend gphoto2` drives the camera directly as a fallback, but gphoto2's
Sony support is reverse-engineered per body and there's an unresolved capture failure
reported against the ILCE-7RM5 — prefer the Capture One path.

Trichromatic (3-shot): fire red, green, blue in sequence and merge the clean
per-channel exposures — no crosstalk to correct, since only one narrowband LED is
ever on at a time.

## Running the tool

One command, launched once per roll:

```bash
trichrom-scan --session-dir /path/to/session --watch-dir /path/to/captures \
    --film-stock "Portra 400" --roll-id roll01
```

`--output-dir` defaults to `--watch-dir` (merged TIFFs land next to the ARWs); pass
it explicitly to write merged TIFFs somewhere else. `--camera-backend` selects how the
shutter fires — `captureone` (default) or `gphoto2`.

A brand-new `--session-dir` (or `--recalibrate`) runs calibration first: ~5 minutes
of LED warm-up, per-channel flats (holder off, bare light), then a leader-based
two-pass channel balance + shutter-speed exposure targeting. Resuming an
already-calibrated session skips straight to the capture loop:

```bash
trichrom-scan --resume --watch-dir /path/to/captures
```

`--resume` (or an explicit `--session-dir` pointing at an existing session) reuses
the saved calibration and flats; if that calibration is more than a few hours old
you're asked whether to redo it rather than it being silently trusted. Per frame:
advance the film, check framing in Capture One's live view, press Enter (terminal
or footswitch). The three exposures fire in sequence, merge into a linear 16-bit
TIFF, and per-channel peaks print as a drift check. A JSON sidecar with calibration
numbers, shutter speed, raw peaks, and source filenames is written next to every
merged TIFF.

## Tests

```bash
python -m pip install -e ".[dev]"
python -m pytest
```

(`python -m` rather than the bare `pytest` script guarantees the tests run under the
same interpreter the dependencies were installed into.)

Tests cover the pure logic that fails *silently* — merge math, the ICC profile, leader
measurement, shutter selection, session state. No hardware, no ARWs, sub-second. The
camera backends and capture loop are deliberately untested: without a camera attached
there's nothing meaningful to assert. No linting is configured.

When adding tests, check they actually catch a regression — break the thing on purpose
and confirm the relevant test fails.

## Project structure

```
src/trichrom/
├── lib/
│   ├── scanner.py       # Scanlight serial control + ARW file watching
│   ├── captureone.py     # camera backend: shutter + settings via C1 AppleScript (default)
│   ├── gphoto.py         # camera backend: direct camera control via gphoto2
│   ├── shutter.py        # shutter-speed parsing/selection shared by both backends
│   ├── rawio.py          # rawpy read + Bayer plane extraction helpers
│   ├── leader.py         # film-base density measurement (variance-masked, percentile)
│   ├── flatfield.py       # per-channel flat-field build + apply
│   ├── icc.py             # minimal linear-ProPhoto ICC profile builder
│   ├── merge_tri.py       # merge pipeline -> linear TIFF + JSON sidecar
│   └── session_state.py   # session.json persistence, resume, staleness warning
└── scan.py               # calibrate (if needed) + capture + merge (trichrom-scan)
```

## Architecture

**`scan.py`** — `main()` decides whether calibration runs: a brand-new session dir,
`--recalibrate`, or a session with no recorded calibration all force it; a stale
(`>4h`) calibration on an otherwise-calibrated resumed session prompts
interactively. Calibration itself is two passes, not an iterative search: pass 1
(`balance_channels`) shoots one exposure per channel at a known starting power and
computes the balance ratio directly (LED output is roughly linear with drive
current) — the two stronger channels scale down to match the weakest, never up.
Pass 2 (`adjust_exposure`) holds channel balance fixed and adjusts shutter speed
alone, picking the nearest camera-supported shutter value by ratio each iteration,
capped at `--max-exposure-iterations`. Flats are captured first (`capture_flats`,
holder off, bare light, averaged over `--flat-shots` exposures per channel) since
flat-field correction must be applied before any leader reading is trusted. The
capture loop (`run_capture_loop`) then fires R/G/B per frame and merges via
`lib/merge_tri.py`. Hardware (Scanlight + camera) connects once and is shared
across both phases.

**`lib/leader.py`** — measures the film-base signal robust to loose leader
positioning: samples a central band, computes local variance via a single-pass
`uniform_filter` on values and values-squared, masks out the highest-variance
fraction (image content and frame edges, since base is smooth), Gaussian-blurs to
average out grain, then takes a high percentile (dust/hot pixels can't set the
target).

**`lib/flatfield.py`** — averages several per-channel exposures, extracts the
matching Bayer plane(s) (green averages G+G2), heavily smooths with
`uniform_filter` to kill flat-field grain without a polynomial model, and
normalizes so the brightest point is 1.0.

**`lib/merge_tri.py`** — for each of the three exposures, extracts only the
matching CFA plane (red from the red exposure, etc.; green averages G+G2),
normalizes by `(white_level - black_level)`, divides by that channel's flat, and
stacks into a linear `(h/2, w/2, 3)` TIFF — already effectively "demosaiced" since
each output pixel came from one cleanly-lit photosite, no interpolation needed.
Unity white balance throughout: the camera's per-channel as-shot WB guess is
recorded in metadata as documentation only, never applied. No lens/vignetting
correction and no DCP/camera color matrix — sensor space is preserved for
downstream color work in Capture One.

**`lib/icc.py`** — hand-built minimal ICC v4 matrix/TRC profile: ProPhoto RGB
primaries (D50 native white point, no chromatic adaptation needed), computed
RGB→XYZ matrix via the standard primaries+whitepoint derivation, and a `curv` TRC
tag with zero curve points (the ICC-spec encoding for an identity/linear
response).

**Camera backends** — `scan.py` reaches the camera through six functions
(`open_camera`, `close_camera`, `trigger_and_wait`, `get_shutter_speed`,
`get_shutter_choices`, `set_shutter_speed`) implemented by both `lib/captureone.py`
and `lib/gphoto.py`. `--camera-backend` picks one; `main()` resolves it once and
stashes the module on `args.cam`, which is already threaded through every function
that touches the camera, so call sites read `args.cam.trigger_and_wait(...)`.

**`lib/captureone.py`** (default) — drives C1 over `osascript`. `trigger_and_wait()`
runs `tell application "Capture One" to capture`; C1's capture is asynchronous, so it
only confirms the command was accepted and the caller's `wait_for_new_file()` remains
the signal that matters. `shutter speed of camera of current document` is confirmed
readable; whether C1 exposes it as *writable* is undocumented and may vary by body, so
`set_shutter_speed()` falls back to prompting the operator to dial it in if the write is
refused — a once-per-roll calibration step, never in the per-frame path.
`get_shutter_choices()` tries `available shutter speeds` and falls back to the standard
ladder in `lib/shutter.py`. Shutter values are checked for quote/backslash before being
interpolated into AppleScript.

**`lib/gphoto.py`** — `trigger_and_wait()` fires the shutter and blocks on
`wait_for_event()` for `GP_EVENT_CAPTURE_COMPLETE`/`GP_EVENT_FILE_ADDED` rather
than a fixed sleep (too-fast sleeps are the classic source of truncated
captures). Shutter speed is read/set via the camera's `shutterspeed` config
widget. Its import is guarded, so the package works without python-gphoto2 installed.

**`lib/shutter.py`** — `nearest_shutter_choice()` picks the closest available value to a
target duration on a log scale, since shutter steps are geometric; `STANDARD_CHOICES` is
the 1/3-stop ladder used when a backend can't report the camera's own list.

**`lib/session_state.py`** — `session.json` lives in the session folder so
provenance travels with the roll if it's moved or archived. A separate small
pointer file (`~/.trichrom/last_session`) records the most recently used session
dir for `--resume`; losing it costs convenience. Per-frame status is recorded so a
mid-roll merge failure identifies exactly which frames need redoing.

**`lib/scanner.py`** — Scanlight serial protocol (custom binary packets over
pyserial) and ARW file watching. The Bayer channel index mapping is 0=R, 1=G, 2=B,
3=G2 (second green in RGGB); black/white levels and active sensor area margins are
read directly from source files — no camera-specific hardcoding.

Note that `wait_for_new_file()` returns as soon as a new ARW's name appears and
picks arbitrarily if more than one is present, so `_shoot()` in `scan.py` calls
`wait_for_settle()` before returning the path. That settle does double duty: it
guarantees the file is fully written before anything reads it (calibration, flats,
and merge all read through this one path), and by holding until the file is done it
keeps one trigger mapped to one file, which is what keeps filename→channel
attribution correct.

## Open items

The code paths that touch Capture One and the camera cannot be exercised without the
rig (macOS + C1 + tethered A7R V), so the following are unverified against real
hardware. The Capture One backend is written to the documented AppleScript surface
but has only been dry-run tested.

**Verify before the first real roll** (all quick checks at the rig):

1. **Trigger fires.** `tell application "Capture One" to capture` actually releases the
   shutter on the A7R V with C1 tethered. Known to work on an A7 III; unverified on
   this body. If it fails, see the fallback below.
2. **`capture` must not autofocus.** AF on a flat negative is unreliable and, worse,
   firing it between the three exposures shifts focus across channels — the spec's
   "no autofocus, ever." A community remote-trigger script initiates AF as an
   *explicit separate step*, which implies plain `capture` does not focus, but confirm
   the lens doesn't hunt on a bare `capture`.
3. **`capture` coexists with continuous live view.** We keep live view up for the whole
   roll (for focus magnification); a common community script instead opens live view,
   shoots, and closes it each time. Confirm capture works without closing live view.
4. **Is `shutter speed` writable?** Capture One → Scripts → Open Scripting Dictionary →
   `camera` class: `shutter speed (text)` means settable, `(text, r/o)` means read-only.
   If read-only, nothing breaks — `set_shutter_speed()` already degrades to prompting
   the operator during calibration — but you'll dial shutter speed by hand each pass-2
   iteration. Reading it is already confirmed to work.

**Fallback if the C1 trigger doesn't work:** `--camera-backend gphoto2` drives the
camera directly, but there's an open unresolved gphoto2 capture failure against the
ILCE-7RM5 (gphoto2 issue #676), so it may not work either. Next candidate is Sony's
**Camera Remote Command** (official CLI, macOS) before CrSDKPy (which needs a
self-built C++ bridge and is Windows-tested only).

**Fast-follow, only if warranted:** replace the poll-and-settle file detection with a
Capture One Background Script bound to `CO_CaptureDone(rawFilePath)`, which hands us
the captured file's path directly — eliminating the folder race and the settle
guesswork (see the `lib/scanner.py` note above). The mechanism is undocumented and it
is unverified whether it fires *after* the file is fully written, so keep the settle
check as a guard. Worth doing once the trigger path is proven, or sooner if
`wait_for_new_file` + settle proves flaky in practice.

**Minor code notes** (deliberately left as-is): `adjust_exposure` uses the R channel's
black level for all three channels' usable-range math — harmless while the A7R V
reports equal black levels across channels. A mid-roll merge failure prints the
exception message but not a traceback.
