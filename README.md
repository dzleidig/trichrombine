# trichrombine

RGB film scanning via Scanlight LED controller + Sony A7III + Capture One.

Includes tools for:
- **Single-shot RGB capture** with per-channel brightness calibration
- **Bayer crosstalk correction** to fix desaturation in single-shot captures
- **3-shot trichromatic capture** (legacy)
- **DNG combining** and metadata management

## Installation

```bash
pip install -e .
```

This installs four CLI commands: `trichrom-capture`, `trichrom-calibrate`, `trichrom-combine`, `trichrom-correct`.

## The crosstalk problem

Single-shot captures (all three LEDs on simultaneously) look noticeably desaturated compared to trichromatic captures of the same scene. The cause is **Bayer sensor crosstalk**: even narrowband LEDs have some spectral overlap with adjacent Bayer filter channels. Red light, for example, still registers a measurable signal in the green and blue photosites. That cross-contamination pulls all three channels toward each other, which manifests as reduced saturation.

The fix is to measure the exact crosstalk for this camera + light combination, invert the resulting 3×3 matrix, and multiply the raw pixel values by M⁻¹ before writing the DNG. This undoes the mixing and restores the color separation the sensor should have captured.

Crosstalk is measured by shooting three calibration exposures — one per LED channel, with the other two off — and reading the raw Bayer-level response in all three channel positions for each exposure. These nine values form the crosstalk matrix M; its inverse is the correction.

## Quick Start

### 1. Calibrate LED brightness (one-time per session)

```bash
trichrom-calibrate --watch-dir /path/to/captures --stabilize 0.5 --capture-wait 10
```

Finds the maximum safe brightness for each LED that won't clip the film rebate. Output:

```
=== Result ===
  --brightness-r 200
  --brightness-g 180
  --brightness-b 210
```

### 2. Capture frames

```bash
trichrom-capture \
  --watch-dir /path/to/captures \
  --brightness-r 200 --brightness-g 180 --brightness-b 210 \
  --loop
```

All three LEDs fire simultaneously. Press Enter for each frame; Ctrl+C to stop.

### 3. Apply crosstalk correction

Single file:
```bash
trichrom-correct --input SHOT.ARW --output corrected.DNG
```

Watch a folder and process automatically as new ARWs arrive:
```bash
trichrom-correct --watch /path/to/captures --output /path/to/dngs
```

Processed ARWs are moved to a `processed/` subfolder. If you want to re-measure the crosstalk matrix from your own calibration shots instead of using the built-in values:

```bash
trichrom-correct --input SHOT.ARW --output corrected.DNG \
    --red RED.ARW --green GREEN.ARW --blue BLUE.ARW
```

### 4. Combine triplets (legacy 3-shot mode)

```bash
trichrom-combine -i /path/to/arw_folder -o /path/to/output
```

For historical 3-shot RGB captures (R, G, B in sequence), extracts each channel's Bayer photosites and recombines into a single DNG.

## Repository Structure

```
trichrom/
├── src/trichrom/
│   ├── scanner.py              # Scanlight hardware abstraction
│   ├── rgb_capture.py          # Trichromatic capture script
│   ├── calibrate_rgb.py        # LED brightness calibration
│   ├── combine_rgb_scans.py    # DNG combining (legacy 3-shot)
│   └── correct_crosstalk.py    # Single-shot crosstalk correction
├── test_images/                # Calibration ARWs and histograms
├── pyproject.toml
└── README.md
```

## Dependencies

- **rawpy** — read Sony ARW raw files
- **numpy** — array operations
- **tifffile** — write DNG output
- **pyexiv2** — manage EXIF/IPTC metadata
- **pyserial** — serial communication with Scanlight
- **scipy** — interpolation for ICC CLUT baking

All installed automatically via `pip install -e .`

## Hardware

- Sony A7III (or compatible)
- Scanlight (Raspberry Pi Pico-based LED driver)
- USB serial connection to Scanlight
- Capture One 16+ with Capture One SDK enabled
