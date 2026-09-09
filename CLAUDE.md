# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup

```bash
pip install -e .
```

Core dependencies: `rawpy`, `tifffile`, `pyexiv2`, `numpy`, `pyserial`, `scipy`, `gphoto2` (python-gphoto2 — needs `libgphoto2` installed on the system). Python 3.14 (see `.tool-versions`).

## Hardware context

Two generations of hardware/workflow live in this repo:

- **Current: Sony A7R V + gphoto2, trichromatic (3-shot).** Camera is triggered directly via `gphoto2` (the ScanLight's own shutter trigger isn't usable on Sony bodies); Capture One stays tethered separately for live view only and imports whatever lands regardless of what triggered it. See `tri_calibrate.py` / `tri_capture.py` below.
- **Prior: Sony A7III + AppleScript/Capture One trigger, single-shot with crosstalk correction.** Still present and functional (`calibrate_rgb.py`, `correct_crosstalk.py`, `measure_crosstalk.py`, `legacy/`) — narrowband LED crosstalk figures measured on the A7III don't transfer to the A7R V (Sony changed red/yellow sensor response starting with that body), so this path isn't currently maintained against real hardware.

## Running the tools

### Trichromatic (3-shot) — current hardware

**Tool 1 — session calibration.** Run once per roll, after ~5 minutes of LED warm-up, with the roll's own leader positioned under the camera. Captures per-channel flats (holder off), balances R/G/B LED power from one exposure each (direct ratio computation, no search loop), then adjusts shutter speed alone until the leader lands at 80-90% of usable range (`white_level - black_level`). Writes channel levels + shutter speed into the session's `session.json`.
```bash
trichrom-tri-calibrate --session-dir /path/to/session --watch-dir /path/to/captures \
    --film-stock "Portra 400" --roll-id roll01
```

**Tool 2 — capture and merge.** Launch once per roll; per frame, advance the film and press Enter. Fires R, G, B in sequence via `gphoto2` (trigger + wait-for-event, never a fixed sleep), merges the matching CFA plane from each exposure into a linear 16-bit TIFF (ProPhoto-primaries ICC profile, unity white balance, no lens/DCP correction — color characterization happens downstream in Capture One), and prints per-channel peaks as a drift check.
```bash
trichrom-tri-capture --resume --watch-dir /path/to/captures --output-dir /path/to/tiffs
trichrom-tri-capture --session-dir /path/to/session --watch-dir /path/to/captures --output-dir /path/to/tiffs
```
`--resume` (or an explicit `--session-dir`) reuses the calibration and flats from Tool 1; a stale (`>4h` old) calibration prints a warning rather than failing silently. A JSON sidecar with calibration numbers, shutter speed, raw peaks, and source filenames is written next to every merged TIFF.

### Single-shot — prior A7III hardware

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

**Legacy 3-shot trichromatic tools (AppleScript-triggered, in `legacy/`):**
```bash
trichrom-combine -i /path/to/arw_folder -o /path/to/output
trichrom-capture --watch-dir /path/to/capture --loop
```

There are no tests or linting configured.

## Project structure

```
src/trichrom/
├── lib/
│   ├── scanner.py              # Scanlight serial control, channel sampling, file watching
│   ├── gphoto.py                # gphoto2 shutter trigger + shutter-speed control
│   ├── rawio.py                 # rawpy read + Bayer plane extraction helpers
│   ├── leader.py                # film-base density measurement (variance-masked, percentile)
│   ├── flatfield.py              # per-channel flat-field build + apply
│   ├── icc.py                    # minimal linear-ProPhoto ICC profile builder
│   ├── merge_tri.py              # 3-shot merge pipeline -> linear TIFF + JSON sidecar
│   └── session_state.py          # session.json persistence, resume, staleness warning
├── legacy/
│   ├── combine_rgb_scans.py    # 3-shot trichromatic DNG combining (AppleScript-triggered)
│   └── rgb_capture.py          # Hardware orchestration for 3-shot capture (AppleScript-triggered)
├── tri_calibrate.py             # Tool 1: session calibration (trichrom-tri-calibrate)
├── tri_capture.py               # Tool 2: capture + merge (trichrom-tri-capture)
├── calibrate_rgb.py             # LED brightness calibration (trichrom-calibrate)
├── correct_crosstalk.py         # Single-shot crosstalk correction (trichrom-correct)
└── measure_crosstalk.py         # Crosstalk matrix measurement (trichrom-measure-crosstalk)
```

## Architecture — trichromatic (3-shot)

**`tri_calibrate.py`** — two passes, not an iterative search. Pass 1 shoots one exposure per channel at a known starting power and computes the balance ratio directly (LED output is roughly linear with drive current): the two stronger channels scale down to match the weakest, never up. Pass 2 holds channel balance fixed and adjusts shutter speed alone, picking the nearest camera-supported shutter value by ratio each iteration, capped at `--max-exposure-iterations`. Flats are captured first (holder off, bare light, averaged over `--flat-shots` exposures per channel) since flat-field correction must be applied before any leader reading is trusted.

**`lib/leader.py`** — measures the film-base signal robust to loose leader positioning: samples a central band, computes local variance via a single-pass `uniform_filter` on values and values-squared, masks out the highest-variance fraction (image content and frame edges, since base is smooth), Gaussian-blurs to average out grain, then takes a high percentile (dust/hot pixels can't set the target).

**`lib/flatfield.py`** — averages several per-channel exposures, extracts the matching Bayer plane(s) (green averages G+G2), heavily smooths with `uniform_filter` to kill flat-field grain without a polynomial model, and normalizes so the brightest point is 1.0.

**`lib/merge_tri.py`** — for each of the three exposures, extracts only the matching CFA plane (red from the red exposure, etc.; green averages G+G2), normalizes by `(white_level - black_level)`, divides by that channel's flat, and stacks into a linear `(h/2, w/2, 3)` TIFF — already effectively "demosaiced" since each output pixel came from one cleanly-lit photosite, no interpolation needed. Unity white balance throughout: the camera's per-channel as-shot WB guess is recorded in metadata as documentation only, never applied. No lens/vignetting correction and no DCP/camera color matrix — sensor space is preserved for downstream color work in Capture One.

**`lib/icc.py`** — hand-built minimal ICC v4 matrix/TRC profile: ProPhoto RGB primaries (D50 native white point, no chromatic adaptation needed), computed RGB→XYZ matrix via the standard primaries+whitepoint derivation, and a `curv` TRC tag with zero curve points (the ICC-spec encoding for an identity/linear response).

**`lib/gphoto.py`** — `trigger_and_wait()` fires the shutter and blocks on `wait_for_event()` for `GP_EVENT_CAPTURE_COMPLETE`/`GP_EVENT_FILE_ADDED` rather than a fixed sleep (too-fast sleeps are the classic source of truncated captures). Shutter speed is read/set via the camera's `shutterspeed` config widget; `nearest_shutter_choice()` picks the closest available value to a target duration on a log scale, since shutter steps are geometric.

**`lib/session_state.py`** — `session.json` lives in the session folder so provenance travels with the roll if it's moved or archived. A separate small pointer file (`~/.trichrom/last_session`) records the most recently used session dir for `--resume`; losing it costs convenience; a `>4`-hour-old calibration triggers a recalibration warning rather than silently trusting stale numbers. Per-frame status is recorded so a mid-roll merge failure identifies exactly which frames need redoing.

## Architecture — single-shot (prior A7III hardware)

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
