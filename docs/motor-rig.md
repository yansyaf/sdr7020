# 3-axis mechanics for planar antenna measurement

Short answer to "can the FPGA GPIO drive my motor drivers": **yes electrically,
no practically.** The Zynq has the pins, but this board's external header does not
bring out enough of them, and FPGA pins are the wrong place to generate step
pulses anyway. Use the header for a trigger/sync line and let a small motion
controller own the motors.

## What the board actually gives you

From the vendor's functional diagram (the ext-extension header):

| header pin | what it is | usable for motion? |
|---|---|---|
| 5V IN/OUT, GND | power | supply for logic / small loads only |
| 3.3V OUT, 1.8V OUT | regulated rails | **not** motor power |
| 3.3V IO | 1 pin of LVCMOS33 | one signal, e.g. a trigger |
| 1.8V IO | 1 pin of LVCMOS18 | one signal |
| RFIC-XTAL VC IO | VCXO control voltage | leave alone |
| PTT OUT (5V) | push-to-talk / TX enable | **the best trigger output on the board** |
| RFIC SYNC IO | sync between boards | spare sync |

On the Linux side the Zynq GPIO controller is there and mostly idle:

```
gpiochip0: GPIOs 906-1023, parent: platform/e000a000.gpio, zynq_gpio
 gpio-906 (led0:yellow)  out lo
 gpio-952 (ulpi resetb)  out hi
 gpio-973 (reset)        out hi
```

118 lines, three in use. But *unused by Linux* is not the same as *available on
the header*: the rest are consumed by the USB PHY, SD, Ethernet, QSPI and the
RFIC control bus, or simply not routed to a connector. The header's two IO pins
are the realistic budget.

Three axes need, at minimum, step + dir each = **6 outputs**, plus 3 limit
switches and probably an enable. That is 10 pins. The header has two.

### Electrical limits, in case you are tempted

* `LVCMOS33` drives ~12 mA, `LVCMOS18` ~8 mA, and the absolute maximum for the
  bank is well below what a driver's opto input wants if you also expect it to be
  clean.
* Never wire an FPGA pin to a driver input directly. Buffer it: 74LVC245 for
  level, ULN2003/ULQ2003 for current, or a proper opto (TLP291, 6N137).
* Motor supply and logic ground must meet at exactly one point. A stepper driver
  dumping 2 A of chopped current through the same ground the ADC references is
  how you get a noise floor full of spurs.

## Recommended architecture

```
   PC ──USB──► PlutoSky 7020 ──RF──► DUT / probe
    │                  │
    │             PTT OUT (5V) ──► trigger in (optional, hardware-synced runs)
    │
    └──USB serial──► GRBL / Marlin (Arduino Uno + CNC shield, or ESP32)
                        └──► 3 × TMC2209 / DRV8825 ──► X, Y, R steppers
                                                     limit switches ×3
```

* **GRBL** (or Marlin) already solves acceleration, homing, limits and step
  timing in a microcontroller. You get all of it for ~$30 and it will not
  jitter when Linux decides to run a kernel thread.
* The Pluto keeps doing what it is good at: RF.
* `scripts/scan_rig.py` is the glue: it sends `G1 X.. Y.. Z..`, waits, captures,
  and writes one row per point.

The FPGA-GPIO path exists — you would add a step generator to
`firmware/hdl/ip/`, route it to EMIO, and rebuild the bitstream — but you still
cannot get 10 pins out of a 2-pin header, and you would be re-implementing GRBL
badly. Only worth it if you later build your own carrier board with a proper
expansion connector.

## Synchronisation

Two honest options:

* **Move–stop–measure** (what `scan_rig.py` does). Move, dwell 200–500 ms for the
  mechanics and the AD9361 AGC/DC loops to settle, capture, store. Slow (~0.5 s
  per point) and *correct*: every capture is tied to a stationary, known
  position.
* **Hardware trigger.** MCU pulls the header's PTT (or SYNC) pin when the axis is
  in position; the SDR captures on that edge. Faster, but needs a capture path
  that reacts to an external trigger — on Pluto that is an FPGA change (a
  triggered buffer start), i.e. real custom firmware work.

For a first working scanner, take the slow and correct option. Sampling density
matters far more than speed.

## Sampling density and mechanical accuracy

Near-field sampling wants **λ/4 or better** between points; far-field pattern
cuts want 1°–2°.

| frequency | λ | λ/2 | λ/4 (recommended) |
|---|---|---|---|
| 1 GHz | 300 mm | 150 mm | 75 mm |
| 2.4 GHz | 125 mm | 62 mm | 31 mm |
| 5.8 GHz | 52 mm | 26 mm | 13 mm |
| 10 GHz | 30 mm | 15 mm | 7.5 mm |

So at 2.4 GHz a 400 mm aperture needs ~13 × 13 points at λ/4 — a 9 × 9 grid at
50 mm pitch is a reasonable first scan; at 5.8 GHz the same aperture needs ~31 × 31.

Accuracy is dominated by **backlash and homing, not microstepping**. A NEMA-17 at
1.8°/step with 16 microsteps is 3200 steps/rev; even 1:1 that is 0.11°/step, and
a GT2 belt reduction of 3:1 gets you to 0.04°. Your real error will be the
0.2–0.5° of lash in a cheap rotary stage. Home every session, always approach a
target angle from the same direction, and log the commanded position (the CSV
does).

## EMI, power, grounding

Motor drivers are switching 20–40 kHz at amps, right next to a receiver that
resolves microvolts:

* separate 12–24 V supply for the motors, star-grounded at one point;
* ferrite clamp on each motor cable, twisted pair, keep them away from the SMA
  pigtails;
* aluminium or copper tape under the DUT, bonded to the SDR ground;
* never share a USB hub between the motion controller and the SDR if you can
  avoid it;
* if the noise floor jumps 10 dB when a motor is energised, stop and fix the
  wiring before you trust any pattern — `scripts/verify_signal.py` at a quiet
  frequency with motors on vs off is the quickest way to see it.

## Running a scan

```bash
# no hardware, checks the whole pipeline in under a second
./scripts/scan_rig.py raster --mock --nx 9 --ny 9 --pitch 25 --out /tmp/scan_mock

# planar raster, 2.4 GHz, 50 mm pitch, homing first
./scripts/scan_rig.py raster --port /dev/ttyUSB0 --home \
    --nx 9 --ny 9 --pitch 50 --freq 2400e6 --dwell 0.3 --out captures/horn_2g4

# azimuth cut at 2 deg steps on the rotary axis
./scripts/scan_rig.py polar --port /dev/ttyUSB0 --start -180 --stop 180 --step 2 \
    --freq 2400e6 --out captures/pattern_az

# transmission measurement instead of plain reception: a CW tone on TX1,
# measure it at the probe (needs --offset, e.g. 250 kHz, and a real DUT path)
./scripts/scan_rig.py raster --port /dev/ttyUSB0 --offset 250e3 --freq 2400e6 \
    --nx 9 --ny 9 --pitch 50 --out captures/transmission
```

Output per scan: `<out>.csv` with `index, x_mm, y_mm, r_deg, freq_hz, mean_dbfs,
peak_over_floor_db`, a JSON sidecar with the settings, and either a heatmap
(raster) or a polar plot (polar). `--save-iq` also stores every capture as
`.npy` if you want to post-process the raw data.

`--r-axis` maps the rotary axis: GRBL/Marlin are 3-axis machines, so rotation
usually rides on **Z** (`--r-axis z`, the default). Set the controller's
steps/mm for that axis as steps/degree.

## Checklist before the first real scan

1. Motors move with `G1 X10 F500` and the machine homes reliably.
2. Limit switches actually stop the machine (test them by hand, at low feed).
3. `--mock` run produces a CSV and a plot.
4. SDR captures at the intended frequency with the rig energised and idle.
5. Noise floor with motors energised vs disabled — should be identical.
6. Then run a coarse grid (9 × 9 at λ/2), look at the heatmap, and refine.
