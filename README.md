# trichrombine

Trichromatic (3-shot) RGB film scanning for a Scanlight narrowband LED light
source: fire red, green, blue in sequence and merge the clean per-channel
exposures. No crosstalk to correct, since only one narrowband LED is ever on at a
time.

- [Background](#background)
- [Hardware](#hardware)
- [Installation](#installation)
- [Workflow](#workflow)

## Background

For information on the benefits of narrowband scanning over broad spectrum:
- https://jackw01.github.io/scanlight/
- https://medium.com/@alexi.maschas/color-negative-film-color-spaces-786e1d9903a4

Trichromatic (3-shot) scanning is the cleanest approach to narrowband scanning:
fire only the red LED and capture, then green, then blue, then merge the three
frames. Each frame captures one pure channel with no contamination from the
others — there's no LED spectral overlap to correct, since the other two LEDs are
off. The cost is speed: three exposures per frame instead of one.

## Hardware

- Sony A7R V, Sigma 105mm f/2.8 DG DN Macro (manual focus, f/8)
- JackW Big ScanLight (narrowband RGB LED, RP2040) over USB serial
- Negative Supply 35mm MK2 holder on a Kaiser RS1 copy stand
- Capture One (live view only) + `gphoto2` for tethered shutter triggering

Capture One stays tethered for live view and focus magnification only — focus is
set once per session (the lens is focus-by-wire) and never touched mid-roll. The
camera shutter is triggered directly via `gphoto2`, since the ScanLight's own
shutter trigger isn't usable on Sony bodies; Capture One imports whatever lands
regardless of what triggered it.

## Installation

```bash
pipx install git+https://github.com/dzleidig/trichrombine.git
```

This installs one CLI command: `trichrom-scan`.

## Workflow

Launch once per roll:

```bash
trichrom-scan --session-dir /path/to/session --watch-dir /path/to/captures \
    --film-stock "Portra 400" --roll-id roll01
```

Merged TIFFs land in `--watch-dir` by default, alongside the ARWs; pass
`--output-dir` to write them somewhere else instead.

### Calibration (automatic on a new session)

A brand-new `--session-dir` (or `--recalibrate` on an existing one) runs
calibration before anything else:

1. **LED warm-up** — ~5 minutes by default. LED output drifts as the light comes
   to temperature; calibrating cold and scanning warm means the numbers slide.
2. **Flat fields** — you're prompted to remove the film holder (bare light only).
   Several exposures per channel are averaged into a per-channel flat-field map,
   used to correct light falloff for the rest of the session.
3. **Leader positioning** — you're prompted to position the roll's own leader
   under the camera. Positioning can be loose: the leader-reading step masks out
   anything that isn't smooth film base, so dust, sprocket holes, and rough
   alignment don't throw it off.
4. **Channel balance** — one exposure per channel at a known starting power. LED
   output is roughly linear with drive current, so the correction is a direct
   ratio computation, not a search: the two stronger channels scale down to match
   the weakest.
5. **Exposure** — with channels balanced, shutter speed alone is adjusted until
   the leader lands at 80-90% of usable range (`white_level - black_level`).

The result — channel levels, shutter speed, flat-field paths — is written to
`session.json` inside `--session-dir`.

### Capture and merge

Once calibrated (or when resuming a session that already is), the tool drops into
the capture loop. Per frame: advance the film, check framing in Capture One's live
view, press Enter (terminal or footswitch). The three exposures fire in sequence
via `gphoto2` (trigger + wait-for-event, never a fixed sleep), merge into a linear
16-bit TIFF, and the per-channel peaks print as a drift check — a light-balance
problem shows up on frame one, not after a whole roll.

To resume a previous session instead of starting a new one:

```bash
trichrom-scan --resume --watch-dir /path/to/captures
```

`--resume` reuses the most recently calibrated session (or pass `--session-dir`
explicitly). If that calibration is more than a few hours old, you're asked
whether to redo it rather than it being silently trusted — LED drift or a physical
bump can make old numbers stale. Each merged TIFF gets a JSON sidecar with
calibration numbers, shutter speed, raw peaks, and source filenames.

Only the matching CFA plane is read from each exposure (red from the red-lit shot,
etc. — green averages the two green photosite positions), normalized by
`white_level - black_level`, divided by that channel's flat field, and stacked
into RGB. Unity white balance throughout, no lens/vignetting correction, and no
DCP or camera color matrix — the file stays in sensor space, tagged with a linear
ProPhoto-primaries ICC profile, and color characterization happens downstream in
Capture One on the merged TIFF. Inversion happens in Capture One's Levels while
the data is still linear; gamma encoding happens on export.
