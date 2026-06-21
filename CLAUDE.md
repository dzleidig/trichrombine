# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup

```bash
pip install -e .
```

Dependencies: `rawpy`, `tifffile`, `pyexiv2`, `numpy`, `pyserial`, `scipy`. Python 3.14 (see `.tool-versions`).

## Running the tools

**Single-shot crosstalk correction (all LEDs on simultaneously):**
```bash
trichrom-correct --input SHOT.ARW --output corrected.DNG
trichrom-correct --watch /path/to/capture                  # auto-process new ARWs, move to processed/
trichrom-correct --watch /path/to/capture --output /path/to/dngs
trichrom-correct --input SHOT.ARW --output corrected.DNG \
    --red RED.ARW --green GREEN.ARW --blue BLUE.ARW        # re-measure crosstalk matrix
```

**Trichromatic capture — combine ARW triplets into DNGs:**
```bash
trichrom-combine -i /path/to/arw_folder -o /path/to/output
trichrom-combine -i /path/to/arw_folder -o /path/to/output --watch
trichrom-combine -i input/ -o output/ --lcc lcc.ARW --filmbase /path/to/filmbase_triplet/
```

**Live trichromatic capture with scanlight hardware:**
```bash
trichrom-capture --watch-dir /path/to/capture --loop
trichrom-capture --watch-dir /path/to/capture --calibrate --loop
trichrom-capture --dry-run
```

There are no tests or linting configured.

## Architecture

The project is a Python package (`src/trichrom/`) installed via `pyproject.toml` with four entry points.

**`correct_crosstalk.py`** — single-shot crosstalk correction. Reads one ARW (all three Scanlight LEDs on simultaneously), extracts R, G, G2, B Bayer channels, subtracts black level, applies the inverse crosstalk matrix M⁻¹ directly to pixel values, then writes a corrected DNG. Also contains the ICC CLUT baking approach (Option B, kept for reference) and the shared `measure_crosstalk()` / `_MEASURED_M` used by both workflows.

Key details:
- `correct_crosstalk()` stacks R/G/B signals as a `(3, N)` matrix and does a single `M_inv @ pixel_matrix` multiply. G2 receives the full row-1 correction using the neighboring R and B values.
- `run_watch_loop()` polls a directory, processes new stable ARWs, moves them to `processed/` on success.
- `_resolve_matrix()` / `_add_matrix_args()` are shared helpers used by both `main()` and `main_bake_icc()` to avoid duplicating the --red/--green/--blue arg setup.

**`combine_rgb_scans.py`** — trichromatic processing engine. `combine_triplet()` reads three ARW files (R, G, B exposures), extracts the appropriate Bayer photosites from each (`extract_bayer_channel()`), merges them into a single Bayer array, and writes a valid DNG. Two optional correction passes:
- **Film base normalization** (`filmbase_neutrals`): scales all channels to a common Dmin median so the film base is neutral — sets `AsShotNeutral` to `[1,1,1]`.
- **LCC correction** (`lcc_path`): divides by a smoothed flat-field luminance map (`compute_lcc_map()`) to correct light falloff/vignetting.

**`rgb_capture.py`** — hardware orchestration for trichromatic capture. Controls the Scanlight LED via serial (custom binary packet protocol over `pyserial`) and triggers Capture One via AppleScript. Calls `combine_triplet()` directly.

**`calibrate_rgb.py`** / **`scanner.py`** — LED calibration and low-level serial communication.

**Data flow:**
```
ARW files (Sony ILCE-7M3) → rawpy (Bayer extraction) → M⁻¹ correction or channel merge → tifffile (DNG write) → pyexiv2 (EXIF inject)
```

The Bayer channel index mapping is: 0=R, 1=G, 2=B, 3=G2 (second green in RGGB). `rawpy`'s `rgb_xyz_matrix` provides the color matrix; black/white levels and active sensor area margins are read directly from the source files — no camera-specific hardcoding.

DNG tag IDs are used directly as integers where `tifffile` has no named alias (e.g. `50728` for `AsShotNeutral`, `50829` for `ActiveArea`).
