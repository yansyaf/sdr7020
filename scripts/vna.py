#!/usr/bin/env python3
"""S11 / S21 measurements with a PlutoSDR-compatible board (PlutoSky 7020).

A scalar-and-vector network analyser mode: stepped CW, coherent (complex)
detection, and proper calibration, so the result is a real S-parameter, not a
"the level went up" guess.

    scripts/vna.py sweep --mode s21 --start 400 --stop 500 --step 2 --out captures/s21
    scripts/vna.py cal   --mode s21 --standard thru
    scripts/vna.py cal   --mode s11 --standard open   (then short, then load)
    scripts/vna.py cal   --mode s11 --solve           (builds the error model)

Wiring
------
S21 (transmission, gain/loss of a filter, cable, amplifier, antenna pair):

    TX1 ──── DUT ──── RX1        plus 20-30 dB of attenuation if the DUT has gain

S11 (reflection, return loss / antenna resonance):

    TX1 ── directional coupler or resistive bridge ── DUT
                    │ coupled (reflected) port ── RX1

    **S11 is physically meaningless without a directional device.** The AD9361
    cannot separate incident from reflected power on its own; a bridge/coupler
    is what makes the measurement. Any cheap 6 dB directional coupler or the
    classic 3-resistor bridge (50 ohm, see docs/vna.md) works.

Optional: put the coupler's *incident* port on RX2 and pass --ref-channel 1. The
ratio RX1/RX2 then cancels transmitter drift and cable movement, which is the
difference between a repeatable measurement and a pretty picture.

Calibration
-----------
S21: connect a short, known-good cable (a "thru") and run
     `cal --mode s21 --standard thru`. A perfect thru then reads 0 dB / 0 deg.
S11: measure OPEN, SHORT and LOAD at the reference plane, then `--solve`.
     That is the standard 1-port, 3-term error model:
         Gm = e00 + (e10e01 * G) / (1 - e11 * G)

Everything is stored as JSON next to the script (cal/ by default) and reused by
later sweeps. The frequency grid of a calibration must match the sweep grid.

Levels
------
TX runs at -30 dB by default: the board has a PGA-102+ PA on TX1, and a DUT that
reflects (open stub, mismatched antenna) sends power back. Raise --tx-gain only
for a well-matched DUT, and use an attenuator on any DUT that has gain.

Examples
--------
    scripts/vna.py sweep --mode s21 --start 2400 --stop 2500 --step 5
    scripts/vna.py sweep --mode s11 --start 88 --stop 108 --step 0.5 --ref-channel 1
    scripts/vna.py sweep --mode s21 --start 900 --stop 950 --out captures/amp_s21
"""
import argparse
import json
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
CAL_DIR = os.path.join(os.path.dirname(HERE), "cal")


# --------------------------------------------------------------------------
# signal generation / detection
# --------------------------------------------------------------------------
def make_tone(rate, offset, amp=0.3):
    """Cyclic-buffer tone with an integer number of cycles (no click at wrap)."""
    period = rate / offset
    cycles = max(1, int(round(16384 / period)))
    n = int(round(cycles * period))
    n -= n % 8                      # pluto wants a multiple of 8 samples
    t = np.arange(n) / rate
    return (amp * np.exp(2j * np.pi * offset * t)).astype(np.complex64)


def goertzel(iq, freq, rate):
    """Complex amplitude at `freq` (single-bin DFT with a Hann window)."""
    n = len(iq)
    w = np.hanning(n)
    ph = np.exp(-2j * np.pi * freq * np.arange(n) / rate)
    return np.sum(iq * w * ph) / np.sum(w)


class VNA:
    def __init__(self, a):
        import adi
        self.a = a
        # libiio occasionally reports EBUSY/"No device found" for a second or two
        # after another process (or a previous run) released the USB gadget.
        last = None
        for attempt in range(6):
            try:
                self.sdr = adi.Pluto(uri=a.uri)
                break
            except Exception as e:
                last = e
                time.sleep(1.0)
        else:
            sys.exit("cannot open %s: %s" % (a.uri, last))
        self.sdr.sample_rate = int(a.rate)
        self.sdr.rx_rf_bandwidth = int(min(a.rate, max(2e6, a.rate * 0.8)))
        self.sdr.tx_rf_bandwidth = self.sdr.rx_rf_bandwidth
        self.sdr.gain_control_mode_chan0 = "manual"
        self.sdr.rx_hardwaregain_chan0 = int(a.rx_gain)
        self.sdr.tx_hardwaregain_chan0 = float(a.tx_gain)
        self.sdr.rx_buffer_size = int(a.nsamples)
        self.use_ref = a.ref_channel is not None
        if self.use_ref:
            self.sdr.rx_enabled_channels = [0, int(a.ref_channel)]
        self.tone = make_tone(a.rate, a.offset, a.amp)
        self.tx_on = False

    def start_tx(self):
        if self.tx_on:
            return
        try:
            self.sdr.tx_destroy_buffer()
        except Exception:
            pass
        self.sdr.tx_cyclic_buffer = True
        self.sdr.tx(self.tone * 2 ** 14)
        self.tx_on = True
        time.sleep(2 * len(self.tone) / self.a.rate + 0.01)

    def stop_tx(self):
        try:
            self.sdr.tx_destroy_buffer()
        except Exception:
            pass
        self.tx_on = False

    def point(self, freq):
        """Complex response at `freq` (Hz)."""
        self.sdr.tx_lo = int(freq)
        self.sdr.rx_lo = int(freq)
        time.sleep(self.a.settle)
        self.sdr.rx()                                  # flush a stale buffer
        d = self.sdr.rx()
        if self.use_ref:
            d = np.asarray(d)
            ch0, ch1 = (d[0], d[1]) if d.ndim == 2 else (d, d)
            r = goertzel(ch0, self.a.offset, self.a.rate)
            ref = goertzel(ch1, self.a.offset, self.a.rate)
            return r / ref if abs(ref) > 0 else 0j
        return goertzel(np.asarray(d), self.a.offset, self.a.rate)


def grid(a):
    f = []
    x = a.start
    while x <= a.stop + 1e-9:
        f.append(x * 1e6)
        x += a.step
    return np.array(f)


# --------------------------------------------------------------------------
# calibration helpers
# --------------------------------------------------------------------------
def cal_path(mode, what):
    return os.path.join(CAL_DIR, "%s_%s.json" % (mode, what))


def save_cal(mode, what, freqs, data, extra=None):
    os.makedirs(CAL_DIR, exist_ok=True)
    obj = {"mode": mode, "what": what, "freq": list(map(float, freqs)),
           "data": [[d.real, d.imag] for d in data], "when": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if extra:
        obj.update(extra)
    json.dump(obj, open(cal_path(mode, what), "w"), indent=1)
    print("saved", cal_path(mode, what))


def load_cal(mode, what):
    p = cal_path(mode, what)
    if not os.path.exists(p):
        return None
    o = json.load(open(p))
    return np.array(o["freq"]), np.array([complex(r, i) for r, i in o["data"]])


def solve_sol(a):
    """1-port 3-term error model from open/short/load measurements."""
    need = {}
    for s in ("open", "short", "load"):
        c = load_cal("s11", s)
        if c is None:
            sys.exit("missing s11 %s calibration - measure open, short and load first" % s)
        need[s] = c
    f = need["open"][0]
    mo, ms, ml = need["open"][1], need["short"][1], need["load"][1]
    e00 = ml                       # ideal load -> gamma 0
    u = mo - e00
    v = ms - e00
    e11 = (u - v) / (u + v)
    e10e01 = u * (1 - e11)
    save_cal("s11", "error_model", f, np.zeros_like(f),
             {"e00": [[z.real, z.imag] for z in e00],
              "e11": [[z.real, z.imag] for z in e11],
              "e10e01": [[z.real, z.imag] for z in e10e01]})
    print("error model built from %d points" % len(f))


def apply_sol(gm, path=os.path.join(CAL_DIR, "s11_error_model.json")):
    if not os.path.exists(path):
        sys.exit("no s11 error model - run 'cal --mode s11 --solve' first")
    o = json.load(open(path))
    e00 = np.array([complex(r, i) for r, i in o["e00"]])
    e11 = np.array([complex(r, i) for r, i in o["e11"]])
    b = np.array([complex(r, i) for r, i in o["e10e01"]])
    if len(e00) != len(gm):
        sys.exit("calibration grid does not match this sweep")
    return (gm - e00) / (b + e11 * (gm - e00))


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------
def write_touchstone(path, freqs, data, mode):
    ext = ".s1p" if mode == "s11" else ".s2p"
    with open(path + ext, "w") as fh:
        fh.write("! sdr7020 VNA %s  %s\n" % (mode.upper(), time.strftime("%Y-%m-%dT%H:%M:%S")))
        fh.write("# Hz S %s R 50\n" % ("DB" if False else "MA"))
        for f, g in zip(freqs, data):
            if mode == "s11":
                fh.write("%.0f %.6f %.4f\n" % (f, abs(g), np.degrees(np.angle(g))))
            else:
                # S21 in a 2-port file; S11/S12/S22 unknown -> 0
                fh.write("%.0f %.6f %.4f 0 0 0 0 %.6f %.4f\n"
                         % (f, abs(g), np.degrees(np.angle(g)), abs(g), np.degrees(np.angle(g))))
    print("wrote", path + ext)


def plot(path, freqs, data, title, mode):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 7), sharex=True, facecolor="#0d1117")
    for ax in (ax1, ax2):
        ax.set_facecolor("#131a23")
        ax.grid(color="#243040", lw=0.5)
        ax.tick_params(colors="#8fa3b8")
    mag = 20 * np.log10(np.abs(data) + 1e-12)
    ax1.plot(freqs / 1e6, mag, color="#38d6ff", lw=1.0)
    ax1.set_ylabel("|%s| (dB)" % mode.upper(), color="#c9d4e0")
    ax1.set_title(title, color="#eaf2fb")
    ax2.plot(freqs / 1e6, np.degrees(np.unwrap(np.angle(data))), color="#7ce38b", lw=1.0)
    ax2.set_ylabel("phase (deg)", color="#c9d4e0")
    ax2.set_xlabel("frequency (MHz)", color="#c9d4e0")
    fig.savefig(path, dpi=125, facecolor=fig.get_facecolor(), bbox_inches="tight")
    print("wrote", path)


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["sweep", "cal"])
    ap.add_argument("--mode", choices=["s11", "s21"], default="s21")
    ap.add_argument("--standard", choices=["thru", "open", "short", "load"], help="for cal")
    ap.add_argument("--solve", action="store_true", help="build the S11 error model")
    ap.add_argument("--start", type=float, default=900, help="MHz")
    ap.add_argument("--stop", type=float, default=950, help="MHz")
    ap.add_argument("--step", type=float, default=1, help="MHz")
    ap.add_argument("--rate", type=float, default=2083333)
    ap.add_argument("--offset", type=float, default=250e3, help="CW tone offset, away from DC")
    ap.add_argument("--nsamples", type=int, default=16384)
    ap.add_argument("--settle", type=float, default=0.006, help="seconds after retuning")
    ap.add_argument("--rx-gain", type=int, default=30)
    ap.add_argument("--tx-gain", type=float, default=-30.0)
    ap.add_argument("--amp", type=float, default=0.3)
    ap.add_argument("--ref-channel", type=int, default=None,
                    help="1 = use RX2 as incident monitor (needs a coupler)")
    ap.add_argument("--uri", default="usb:")
    ap.add_argument("--out", default=None, help="basename for csv/touchstone/png")
    ap.add_argument("--no-plot", action="store_true")
    a = ap.parse_args()

    if a.action == "cal" and a.mode == "s11" and a.solve:
        solve_sol(a)
        return 0
    if a.action == "cal" and not a.standard:
        sys.exit("cal needs --standard thru|open|short|load")

    freqs = grid(a)
    print("%s: %d points, %.3f-%.3f MHz, step %.3f MHz, CW offset %.0f kHz"
          % (a.mode.upper(), len(freqs), a.start, a.stop, a.step, a.offset / 1e3))

    v = VNA(a)
    v.start_tx()
    data = np.zeros(len(freqs), dtype=complex)
    t0 = time.time()
    try:
        for i, f in enumerate(freqs):
            data[i] = v.point(f)
            if (i + 1) % max(1, len(freqs) // 10) == 0:
                print("  %d/%d  %.1f MHz  raw %+6.1f dB"
                      % (i + 1, len(freqs), f / 1e6,
                         20 * np.log10(abs(data[i]) + 1e-12)), flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        v.stop_tx()
    print("sweep done in %.1f s" % (time.time() - t0))

    if a.action == "cal":
        save_cal(a.mode, a.standard, freqs, data)
        if a.standard == "thru":
            print("a perfect thru now reads 0 dB / 0 deg")
        return 0

    # ---- calibrated sweep -------------------------------------------------
    if a.mode == "s21":
        c = load_cal("s21", "thru")
        if c is None:
            print("WARNING: no thru calibration - this is raw response, not S21",
                  file=sys.stderr)
        else:
            if len(c[0]) != len(freqs):
                sys.exit("thru calibration has a different grid")
            data = data / c[1]
    else:
        data = apply_sol(data)

    out = a.out or os.path.join(os.path.dirname(HERE), "captures",
                                "%s_sweep" % a.mode)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out + ".csv", "w") as fh:
        fh.write("freq_hz,mag_db,phase_deg,re,im\n")
        for f, g in zip(freqs, data):
            fh.write("%.0f,%.4f,%.4f,%.8f,%.8f\n"
                     % (f, 20 * np.log10(abs(g) + 1e-12), np.degrees(np.angle(g)), g.real, g.imag))
    print("wrote", out + ".csv")
    write_touchstone(out, freqs, data, a.mode)
    if not a.no_plot:
        plot(out + ".png", freqs, data,
             "PlutoSky 7020 %s sweep" % a.mode.upper(), a.mode)
    return 0


if __name__ == "__main__":
    sys.exit(main())
