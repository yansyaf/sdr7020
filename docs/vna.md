# S11 / S21 measurements with the PlutoSky 7020

`scripts/vna.py` turns the board into a stepped-CW vector network analyser:
a CW tone out of TX1, coherent (complex) detection on RX1, and calibration, so
you get a real S-parameter with magnitude **and phase**, not just a level.

## What you need

| measurement | extra hardware | why |
|---|---|---|
| **S21** (gain, loss, filter shape) | none, just a cable | TX1 → DUT → RX1 |
| **S11** (return loss, resonance) | **directional coupler or resistive bridge** | the AD9361 cannot separate forward from reflected power |

S11 without a directional device is not a measurement — it is a number. Any
cheap 6 dB coupler works, or build the classic 3-resistor bridge:

```
               50R
   TX1 ──┬────/\/\/────┐
         │             │
         │          RX1 (coupled)
         │
        DUT
         │
        GND
```

A packaged RF bridge (e.g. a Mini-Circuits ZFDC-10-5, or the coupler from an old
Wiltron kit) is far better behaved than three resistors at UHF. Keep leads short.

### Level safety

TX defaults to **−30 dB** because TX1 drives a PGA-102+ PA. A mismatched antenna
reflects power back into it. For an active DUT, put 20–30 dB of attenuation in
front of RX1 — the AD9361 saturates at about −20 dBm.

## Procedure

### S21

```bash
# 1. reference: connect TX1 straight to RX1 through your best cable ("thru")
scripts/vna.py cal --mode s21 --standard thru --start 400 --stop 500 --step 2

# 2. insert the DUT and sweep the same grid
scripts/vna.py sweep --mode s21 --start 400 --stop 500 --step 2 --out captures/filter_s21
```

A perfect thru now reads 0 dB / 0°. Everything else is relative to it, including
the cable you used for the reference — so keep the reference cable short and do
not move it between calibration and measurement.

### S11

```bash
scripts/vna.py cal --mode s11 --standard open  --start 88 --stop 108 --step 0.5
scripts/vna.py cal --mode s11 --standard short --start 88 --stop 108 --step 0.5
scripts/vna.py cal --mode s11 --standard load  --start 88 --stop 108 --step 0.5
scripts/vna.py cal --mode s11 --solve
scripts/vna.py sweep --mode s11 --start 88 --stop 108 --step 0.5 --out captures/antenna_s11
```

OPEN, SHORT and LOAD are measured **at the reference plane** — the point where
the DUT will connect, including any pigtail you will use. Then `--solve` fits the
standard 1-port, 3-term error model

```
Gm = e00 + (e10·e01 · G) / (1 - e11 · G)
```

and later sweeps invert it, removing the directivity, tracking and source-match
errors of the bridge.

### Better repeatability: use RX2 as the incident monitor

If your coupler has a forward-coupled port, connect it to RX2 and add
`--ref-channel 1`. The ratio RX1/RX2 then cancels transmitter drift and cable
movement:

```bash
scripts/vna.py sweep --mode s11 --ref-channel 1 --start 88 --stop 108 --step 0.5
```

## Output

For `<out>` you get:

* `<out>.csv` — `freq_hz, mag_db, phase_deg, re, im`
* `<out>.s1p` / `<out>.s2p` — Touchstone (MA format), importable into any
  simulator or viewer
* `<out>.png` — magnitude and unwrapped phase

## Practical limits

* **Stepped CW**, not a chirp: one LO settle per point (~6 ms). 200 points ≈ 2 s.
  That is slower than a real VNA but it needs no synchronisation trickery.
* Tone offset is 250 kHz by default, well clear of the DC/1-f pedestal that the
  AD9361 shows at high gain. Do not set it below ~100 kHz.
* Dynamic range is roughly 60–70 dB with 16 k samples and 30 dB RX gain; raise
  `--nsamples` for more.
* Phase is only meaningful after a calibration — the two PLLs give an arbitrary
  but stable offset per frequency.
* The frequency grid of a calibration must match the sweep exactly; the script
  refuses a mismatch rather than silently interpolating.

## Verified

On the bench (no DUT): a 5-point sweep at 900–906 MHz runs in 0.2 s, and a
thru-calibrated repeat reads −0.19 dB / +0.42 dB — i.e. the calibration chain
and the error-model maths are correct. Absolute S11 accuracy still depends on
the coupler you attach; measure the three standards and check that the solved
model leaves the LOAD near −40 dB before you trust an antenna plot.
