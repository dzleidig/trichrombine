# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup

```bash
pip install -r requirements.txt
```

Dependencies: `rawpy`, `tifffile`, `pyexiv2`, `numpy`, `pyserial`, `scipy`. Python 3.14 (see `.tool-versions`).

## Running the scripts

**Batch combine ARW triplets into DNGs:**
```bash
python combine_rgb_scans.py -i /path/to/arw_folder -o /path/to/output
python combine_rgb_scans.py -i /path/to/arw_folder -o /path/to/output --watch  # tethered shooting
python combine_rgb_scans.py -i input/ -o output/ --lcc lcc.ARW --filmbase /path/to/filmbase_triplet/
```

**Live capture with scanlight hardware:**
```bash
python rgb_capture.py --watch-dir /path/to/capture --loop
python rgb_capture.py --watch-dir /path/to/capture --calibrate --loop  # auto-balance brightness
python rgb_capture.py --dry-run  # test without hardware
```

There are no tests or linting configured.

## Architecture

The project is two cooperating scripts:

**`combine_rgb_scans.py`** — the core processing engine. `combine_triplet()` is the central function: it reads three ARW files (R, G, B exposures), extracts the appropriate Bayer photosites from each (`extract_bayer_channel()`), merges them into a single Bayer array, and writes a valid DNG using `tifffile` with hand-crafted DNG TIFF tags. EXIF metadata is copied from the source files using `pyexiv2`. Two optional correction passes sit between extraction and merge:
- **Film base normalization** (`filmbase_neutrals`): scales all channels to a common Dmin median so the film base is neutral before combining — sets `AsShotNeutral` to `[1,1,1]` in the DNG.
- **LCC correction** (`lcc_path`): divides by a smoothed flat-field luminance map (`compute_lcc_map()`) to correct light falloff/vignetting.

**`rgb_capture.py`** — the hardware orchestration layer. It controls the scanlight LED via serial (custom binary packet protocol over `pyserial`) and triggers Capture One via AppleScript. It imports and calls `combine_triplet()` directly, so there is no subprocess boundary between capture and combining. Key flow:
- `run_calibration()` finds the maximum non-clipping brightness for each R/G/B LED channel by probing with real captures and iteratively converging on `CLIP_THRESHOLD` (13000).
- `capture_sequence()` sets each LED color, triggers capture, and watches a directory for the new ARW to land.
- `combine_frame()` wraps `combine_triplet()` and writes to an output directory.

**Data flow:**
```
ARW files (Sony ILCE-7M3) → rawpy (Bayer extraction) → channel merge → tifffile (DNG write) → pyexiv2 (EXIF inject)
```

The Bayer channel index mapping is: 0=R, 1=G, 2=B, 3=G2 (second green in RGGB). The G2 channel is always sourced from the green exposure alongside G. `rawpy`'s `rgb_xyz_matrix` provides the color matrix; black/white levels and active sensor area margins are read directly from the source files — no camera-specific hardcoding.

DNG tag IDs are used directly as integers where `tifffile` has no named alias (e.g. `50728` for `AsShotNeutral`, `50829` for `ActiveArea`).
