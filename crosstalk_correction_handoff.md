# Handoff: Crosstalk Correction via DNG ColorMatrix1 (Option B)

## Background

Single-shot RGB film scanning workflow using a Jackw01 narrowband
Scanlight (RGB LED) + Sony A7III + Capture One. All three Scanlight
channels are on simultaneously during capture (NOT the 3-shot
trichromatic approach — that was tested and shelved due to physical
misregistration between sequential exposures).

**Problem:** single-shot captures look noticeably desaturated compared
to 3-shot trichromatic captures of the same/similar scenes, using the
same camera profile (Camera RGB, Linear Response) in Capture One. This
is attributed to **Bayer sensor crosstalk** — narrowband light still
leaks into the "wrong" Bayer photosites (e.g. red light registering some
signal in green/blue photosites), and that cross-contamination averages
channels toward grey, which manifests as desaturation. Earlier rough
measurements (from a different but related script) found crosstalk in
the 28-31% range across channel pairs.

**Goal:** measure the actual crosstalk for this camera + light
combination directly (no ColorChecker available/purchased), compute a
3x3 correction matrix, and apply it via the DNG `ColorMatrix1` tag so
Capture One (or any DNG reader) applies the correction automatically as
part of normal raw processing — i.e. without manually rewriting every
pixel value in a script.

This is "Option B" of two approaches discussed; "Option A" (directly
multiply every pixel by the inverse matrix and write corrected pixel
values into a new DNG) was set aside to try this simpler approach first.

---

## Step 1: Measure the crosstalk matrix (no ColorChecker needed)

Shoot three single-channel exposures of the **clear film rebate** (no
image content), one per Scanlight channel, with the other two channels
off:

- Red-only exposure
- Green-only exposure
- Blue-only exposure

For each exposure, read the **raw Bayer-level averages** (not Capture
One's WB-adjusted readout — use rawpy or equivalent to get true raw
sensor values, the same way RawDigger would show them) for all three
output positions: R, G, B.

This gives a 3x3 matrix where each column is one channel's "true"
contribution as it actually spreads across the R/G/B sensor outputs:

```
        Red-only   Green-only   Blue-only
R_out:   R_r         R_g          R_b
G_out:   G_r         G_g          G_b
B_out:   B_r         B_g          B_b
```

Normalize each column so the dominant value reads as 1.0 (i.e. divide
each column by its own diagonal entry), giving matrix M.

## Step 2: Invert the matrix

Compute M⁻¹ (use numpy.linalg.inv). This is the correction matrix that,
when applied to raw RGB values, undoes the measured crosstalk.

## Step 3: Write M⁻¹ into the DNG's ColorMatrix1 tag

Take a normally-captured single-shot ARW (all 3 Scanlight channels on,
calibrated per usual so the rebate sits at ~14000 raw value per
channel — see calibration notes below) and convert/repackage it as a DNG
with the computed M⁻¹ written into the `ColorMatrix1` EXIF/TIFF tag,
replacing whatever matrix would otherwise be there (the camera's default
sensor-to-XYZ matrix).

**Important context carried over from earlier work in this project:**
there is existing Python tooling (`combine_rgb_scans.py`) that already
extracts raw Bayer photosite data via `rawpy` and writes DNGs with custom
metadata (including custom `ColorMatrix1`-style tags) via `tifffile` +
`pyexiv2`. The DNG-writing approach (tags like `CFARepeatPatternDim`,
`CFAPattern`, `ColorMatrix1`, `CalibrationIlluminant1`, `BlackLevel`,
`WhiteLevel`, `AsShotNeutral`) used in that script is a good reference
for the tag-writing mechanics needed here — adapt rather than reinvent.

There were two earlier, separate, currently-broken scripts attempting
something similar for crosstalk correction specifically
(`compute_crosstalk.py`, `arw_to_corrected_dng.py`) — these had a known
bug where the output DNG's white balance metadata came out as Tint 100,
causing bad color in Capture One. That bug was never resolved; these
scripts may be worth reviewing for salvageable logic but should not be
trusted as-is.

## Step 4: Test in Capture One

Open the resulting DNG in Capture One with the normal single-shot
workflow (Camera RGB profile, Linear Response, Film Negative inversion).
Compare saturation/color accuracy against:
- The same frame processed without the custom ColorMatrix1 (baseline)
- A 3-shot trichromatic combine of a similar/same scene, if available,
  as a rough reference point for "what good separation looks like"

**Known risk flagged in advance:** writing a custom `ColorMatrix1` could
interact unpredictably with Capture One's own white balance / "As Shot"
handling, similar to problems already encountered with `AsShotNeutral`
metadata in the trichromatic workflow (where `AsShotNeutral = 1,1,1`
caused Capture One to compute a nonsensical Kelvin/Tint, e.g. 1442K /
-53 tint, even though underlying raw pixel data was correct). If
`ColorMatrix1` produces similarly broken results, fall back to "Option
A" (directly transform pixel values in the script, write a normal DNG,
skip relying on Capture One to apply the matrix via metadata).

---

## Relevant calibration context (for generating the test ARW/DNG)

- Light calibration target: film rebate MAX value (RawDigger, raw — not
  Capture One's WB-adjusted readout) should be ~14000 out of ~16383
  white level, per channel, with 0.0% OvExp. Previous tests at
  ~9300-10400 caused post-inversion clipping in shadow/dense areas.
- Scanlight warm-up required before calibrating (LEDs drift cold).
- In-camera WB: Daylight (fixed preset, NOT Auto/AWB — confirmed
  important to avoid camera baking in a hidden per-shot WB shift).
- Capture One profile: Camera RGB, Linear Response (no existing color
  matrix transform) — needed so the custom ColorMatrix1 is the only
  matrix being applied, not stacked on top of another one.
- Capture One inversion: Film Negative tool (correct for single-shot,
  since it's designed for the orange-mask single-exposure case).

## Open question to resolve during this work

Does Capture One actually respect a custom `ColorMatrix1` tag in a
third-party-generated DNG, or does it override/ignore it in favor of its
own camera-specific processing? This should be verified early — if
Capture One ignores the tag, Option B is a dead end and we should move
straight to Option A.
