# trichrom

RGB film scanning via Scanlight LED controller + Sony A7III + Capture One.

Includes tools for:
- **Single-shot RGB capture** with per-channel brightness calibration (recommended)
- **3-shot trichromatic capture** (legacy)
- **DNG combining** and metadata management

## Installation

```bash
pip install -e .
```

This installs three CLI commands: `trichrom-capture`, `trichrom-calibrate`, `trichrom-combine`.

## Quick Start

### 1. Calibrate LED brightness (one-time setup per session)

```bash
trichrom-calibrate --watch-dir /path/to/captures --stabilize 0.5 --capture-wait 10
```

This measures each Bayer channel's 99th-percentile in the center frame and iterates to find the maximum safe brightness for each LED that doesn't clip. Output:

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

Frames are captured with all three LEDs on simultaneously. Press Enter for each frame; Ctrl+C to stop.

### 3. Combine triplets (legacy 3-shot mode)

```bash
trichrom-combine -i /path/to/arw_folder -o /path/to/output
```

For historical 3-shot RGB captures (R, G, B in sequence), extracts each channel's Bayer photosites and recombines into a single DNG.

## Documentation

- [Setup & Installation](docs/SETUP.md)
- [Workflow & Calibration Strategy](handoff.md)
- [Implementation Notes](CLAUDE.md)

## Repository Structure

```
trichrom/
├── src/trichrom/           # Package code
│   ├── __init__.py
│   ├── scanner.py          # Scanlight hardware abstraction
│   ├── rgb_capture.py      # Capture script
│   ├── calibrate_rgb.py    # LED brightness calibration
│   └── combine_rgb_scans.py # DNG combining (legacy)
├── docs/                    # Documentation
├── pyproject.toml          # Package metadata & entry points
└── README.md
```

## Dependencies

- **rawpy** — read Sony ARW raw files
- **numpy** — array operations
- **tifffile** — write DNG output
- **pyexiv2** — manage EXIF/IPTC metadata
- **pyserial** — serial communication with Scanlight
- **scipy** — color space transforms

All installed automatically via `pip install -e .`

## Hardware

- Sony A7III (or compatible)
- Scanlight (Raspberry Pi Pico-based LED driver)
- USB serial connection to Scanlight
- Capture One 16+ with Capture One SDK enabled
