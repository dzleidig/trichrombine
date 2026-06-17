# trichrom

Combines three sequential narrowband exposures (R, G, B) shot on a Sony ILCE-7M3 into a single DNG file by extracting each channel's Bayer photosites from the relevant exposure and recombining them into a valid CFA raw image.

## How it works

Each triplet of ARW files is treated as R, G, B exposures in order. The script:

1. Reads the raw Bayer data from each exposure
2. Extracts the appropriate photosites per channel (R pixels from the R exposure, G from the G exposure, B from the B exposure)
3. Recombines them into a single Bayer array
4. Writes a DNG with full metadata — color matrices, white balance, active area crop, CFA layout — compatible with Lightroom, Capture One, and RawTherapee

## Requirements

```
pip install -r requirements.txt
```

Dependencies: `rawpy`, `tifffile`, `pyexiv2`, `numpy`

## Usage

### One-shot batch

Process all complete triplets in a folder:

```bash
python combine_rgb_scans.py -i /path/to/arw_folder -o /path/to/output
```

### Watch mode

Poll a folder and process triplets as they arrive (useful when shooting tethered):

```bash
python combine_rgb_scans.py -i /path/to/arw_folder -o /path/to/output --watch
```

Optional flags:
- `--interval N` — poll interval in seconds (default: 5)
- `--move` — move processed ARW files into `<input>/processed/` after each successful combine

### File naming

Input files must be sorted alphanumerically, grouped in consecutive triplets:

```
trichromatic2105.ARW  → Red
trichromatic2106.ARW  → Green
trichromatic2107.ARW  → Blue
trichromatic2108.ARW  → Red (next frame)
...
```

Output DNGs are named after the first file in each triplet:

```
trichromatic2105_combined.dng
trichromatic2108_combined.dng
```

## Camera support

Currently hardcoded for the **Sony ILCE-7M3** (active sensor area 6000×4000). The Bayer pattern, color matrices, black/white levels, and white balance are read from the source ARW files via `rawpy` and `pyexiv2`.
