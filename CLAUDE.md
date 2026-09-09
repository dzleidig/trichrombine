# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup

```bash
pip install -e .
```

Dependencies: `rawpy`, `tifffile`, `pyexiv2`, `numpy`, `pyserial`, `scipy`, `gphoto2` (python-gphoto2 — needs `libgphoto2` installed on the system). Python 3.14 (see `.tool-versions`).

## Hardware

Sony A7R V, Sigma 105mm f/2.8 DG DN Macro (manual focus, f/8), JackW Big ScanLight
(narrowband RGB LED, RP2040) over USB serial, Negative Supply 35mm MK2 holder on a
Kaiser RS1 copy stand. Capture One stays tethered for live view only; the camera
shutter is triggered directly via `gphoto2` (the ScanLight's own shutter trigger
isn't usable on Sony bodies), and Capture One imports whatever lands regardless of
what triggered it.

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
it explicitly to write merged TIFFs somewhere else.

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

There are no tests or linting configured.

## Project structure

```
src/trichrom/
├── lib/
│   ├── scanner.py       # Scanlight serial control, channel sampling, file watching
│   ├── gphoto.py         # gphoto2 shutter trigger + shutter-speed control
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

**`lib/gphoto.py`** — `trigger_and_wait()` fires the shutter and blocks on
`wait_for_event()` for `GP_EVENT_CAPTURE_COMPLETE`/`GP_EVENT_FILE_ADDED` rather
than a fixed sleep (too-fast sleeps are the classic source of truncated
captures). Shutter speed is read/set via the camera's `shutterspeed` config
widget; `nearest_shutter_choice()` picks the closest available value to a target
duration on a log scale, since shutter steps are geometric.

**`lib/session_state.py`** — `session.json` lives in the session folder so
provenance travels with the roll if it's moved or archived. A separate small
pointer file (`~/.trichrom/last_session`) records the most recently used session
dir for `--resume`; losing it costs convenience. Per-frame status is recorded so a
mid-roll merge failure identifies exactly which frames need redoing.

**`lib/scanner.py`** — Scanlight serial protocol (custom binary packets over
pyserial), ARW file watching, and Bayer channel sampling utilities. The Bayer
channel index mapping is 0=R, 1=G, 2=B, 3=G2 (second green in RGGB); black/white
levels and active sensor area margins are read directly from source files — no
camera-specific hardcoding.
