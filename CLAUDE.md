# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup

```bash
pip install -e .
```

Core dependencies: `rawpy`, `tifffile`, `pyexiv2`, `numpy`, `pyserial`. Optional: `scipy` (legacy 3-shot combining). Python 3.14 (see `.tool-versions`).

## Running the tools

**Measure crosstalk matrix from single-LED calibration ARWs (one-time):**
```bash
trichrom-measure-crosstalk \
    --red RED.ARW --green GREEN.ARW --blue BLUE.ARW \
    --output crosstalk.csv
```

**LED brightness calibration (per scanning session):**
```bash
trichrom-calibrate --watch-dir /path/to/captures
trichrom-calibrate --watch-dir /path/to/captures --clip-threshold 14000 --probe-brightness 50
```

**Single-shot crosstalk correction:**
```bash
trichrom-correct --input SHOT.ARW --output corrected.DNG --matrix crosstalk.csv
trichrom-correct --watch /path/to/captures --output /path/to/dngs --matrix crosstalk.csv
```
If `--matrix` is omitted, falls back to the hardcoded Sony A7III + Scanlight matrix. A pre-measured matrix is at `test_images/crosstalk.csv`.

**Legacy 3-shot trichromatic tools (in `legacy/`):**
```bash
trichrom-combine -i /path/to/arw_folder -o /path/to/output
trichrom-capture --watch-dir /path/to/capture --loop
```

There are no tests or linting configured.

## Project structure

```
src/trichrom/
├── lib/
│   └── scanner.py              # Scanlight serial control, channel sampling, file watching
├── legacy/
│   ├── combine_rgb_scans.py    # 3-shot trichromatic DNG combining
│   └── rgb_capture.py          # Hardware orchestration for 3-shot capture
├── calibrate_rgb.py            # LED brightness calibration (trichrom-calibrate)
├── correct_crosstalk.py        # Single-shot crosstalk correction (trichrom-correct)
└── measure_crosstalk.py        # Crosstalk matrix measurement (trichrom-measure-crosstalk)
```

## Architecture

**`measure_crosstalk.py`** — reads three single-LED ARWs, samples the center 400×400 Bayer patch for each channel position, computes median responses, and builds the 3×3 crosstalk matrix M normalized so the diagonal is 1.0. Saves as CSV.

**`correct_crosstalk.py`** — single-shot correction. Reads an ARW (all three LEDs on), extracts R, G, G2, B Bayer channels, subtracts black level, applies M⁻¹ directly to pixel values, writes a corrected DNG with preserved EXIF/color metadata.

Key details:
- `correct_crosstalk()` stacks R/G/B signals as a `(3, N)` matrix and does a single `M_inv @ pixel_matrix` multiply. G2 receives full row-1 correction using the neighboring R and B values.
- `run_watch_loop()` polls a directory, processes stable ARWs, moves them to `processed/` on success.
- `_resolve_matrix()` loads M from a CSV (`--matrix`) or falls back to `_FALLBACK_M`.

**`calibrate_rgb.py`** — iteratively adjusts R, G, B LED brightnesses until each channel is ETTR without clipping. If a channel maxes at brightness 255 without converging, prompts the user to adjust camera exposure and retries from scratch rather than exiting.

**`lib/scanner.py`** — Scanlight serial protocol (custom binary packets over pyserial), Capture One triggering via AppleScript, ARW file watching, and Bayer channel sampling utilities.

**Data flow:**
```
ARW files (Sony ILCE-7M3) → rawpy (Bayer extraction) → M⁻¹ correction → tifffile (DNG write) → pyexiv2 (EXIF inject)
```

The Bayer channel index mapping is: 0=R, 1=G, 2=B, 3=G2 (second green in RGGB). `rawpy`'s `rgb_xyz_matrix` provides the color matrix; black/white levels and active sensor area margins are read directly from source files — no camera-specific hardcoding.

DNG tag IDs are used directly as integers where `tifffile` has no named alias (e.g. `50728` for `AsShotNeutral`, `50829` for `ActiveArea`).
