#!/usr/bin/env python3
"""3-axis scan rig for planar antenna measurements, driven alongside the SDR.

Moves three motors, waits for the mechanics to settle, captures a measurement at
every point and writes a co-registered dataset. Motion goes through a GRBL or
Marlin controller over serial (an Arduino/ESP32 + 3 stepper drivers is the cheap,
proven option); the SDR is driven directly.

    # shake it out with no hardware at all
    scripts/scan_rig.py raster --mock --nx 9 --ny 9 --out /tmp/scan_mock

    # real rig: 9 x 9 points over 400 x 400 mm, 25 mm pitch
    scripts/scan_rig.py raster --port /dev/ttyUSB0 --nx 9 --ny 9 --pitch 50 \
        --freq 2400e6 --out captures/horn_2g4

    # polar cut at 5 deg steps, rotation on the R axis
    scripts/scan_rig.py polar --port /dev/ttyUSB0 --start -180 --stop 180 --step 5 \
        --freq 2400e6 --out captures/pattern_az

Why move-stop-measure instead of measuring on the fly: a stepper plus a 6 GHz
receiver in the same room means vibration and EMI. Stopping for a dwell gives a
clean capture and a position you can trust. The dwell is the price of a dataset
that is actually co-registered with position.

Axes
----
    X, Y   planar position (mm)      R   rotation (deg, on the Z/rotary axis)

GRBL/Marlin are 3-axis machines, so X, Y and Z are used: `--r-axis z` (default)
maps the rotation to the controller's Z. Remember to scale steps/mm accordingly.

Position feedback
-----------------
Home first (`--home`), and use limit switches. Without homing, every scan starts
from wherever the machine was left, and the pattern is rotated/shifted by an
unknown amount. If the rig has no switches, at least run `G92 X0 Y0 Z0` at a
known registration mark.
"""
import argparse
import csv
import json
import math
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))


# --------------------------------------------------------------------------
# motion backends
# --------------------------------------------------------------------------
class GrblMotion:
    """GRBL/Marlin over USB serial. Speaks the subset of g-code we need."""

    def __init__(self, port, baud=115200, r_axis="z", feed=3000):
        import serial
        self.s = serial.Serial(port, baud, timeout=0.2)
        self.r_axis = r_axis
        self.feed = feed
        time.sleep(2.0)                      # Arduino reset on open
        self.s.reset_input_buffer()
        self._send("G21")                    # millimetres
        self._send("G90")                    # absolute
        time.sleep(0.2)

    def _send(self, line):
        self.s.write((line + "\n").encode())
        deadline = time.time() + 30
        buf = b""
        while time.time() < deadline:
            buf += self.s.read(256)
            if b"ok" in buf or b"error" in buf:
                return b"ok" in buf
            if b"ALARM" in buf:
                raise RuntimeError("controller alarm: %s" % buf.decode("utf-8", "replace"))
        raise TimeoutError("no reply to %r (buf=%r)" % (line, buf[:120]))

    def home(self):
        return self._send("$H" if self.r_axis else "G28")

    def move(self, x=None, y=None, r=None):
        parts = []
        if x is not None:
            parts.append("X%.3f" % x)
        if y is not None:
            parts.append("Y%.3f" % y)
        if r is not None:
            parts.append("%s%.3f" % (self.r_axis.upper(), r))
        if not parts:
            return True
        return self._send("G1 %s F%d" % (" ".join(parts), self.feed))

    def dwell(self, seconds):
        return self._send("G4 P%.2f" % seconds)

    def close(self):
        self.s.close()


class MockMotion:
    """No hardware: records the commanded position so the pipeline can be tested."""

    def __init__(self, *a, **kw):
        self.pos = {"x": 0.0, "y": 0.0, "r": 0.0}
        self.log = []

    def home(self):
        self.pos = {"x": 0.0, "y": 0.0, "r": 0.0}
        return True

    def move(self, x=None, y=None, r=None):
        if x is not None:
            self.pos["x"] = x
        if y is not None:
            self.pos["y"] = y
        if r is not None:
            self.pos["r"] = r
        self.log.append(dict(self.pos))
        return True

    def dwell(self, seconds):
        time.sleep(min(seconds, 0.01))       # never actually wait in a mock run
        return True

    def close(self):
        pass


# --------------------------------------------------------------------------
# measurement backends
# --------------------------------------------------------------------------
class SdrMeasure:
    """One capture per point; returns mean power and peak above the noise floor."""

    def __init__(self, freq, rate=2083333.0, gain=40, nsamples=65536,
                 uri="usb:", offset=None, save_iq=False):
        import adi
        self.sdr = adi.Pluto(uri=uri)
        self.sdr.sample_rate = int(rate)
        self.sdr.rx_rf_bandwidth = int(rate)
        self.sdr.gain_control_mode_chan0 = "manual"
        self.sdr.rx_hardwaregain_chan0 = int(gain)
        self.sdr.rx_buffer_size = int(nsamples)
        self.sdr.rx_lo = int(freq)
        self.rate = rate
        self.offset = offset
        self.save_iq = save_iq

    def measure(self, index, outdir):
        for _ in range(2):
            d = np.asarray(self.sdr.rx())
        d = d / 2 ** 11
        mean = float(10 * np.log10(np.mean(np.abs(d) ** 2) + 1e-20))
        if self.offset is not None:
            # narrow-band power at a CW offset (use with a transmitter on)
            n = len(d)
            w = np.hanning(n)
            ph = np.exp(-2j * np.pi * self.offset * np.arange(n) / self.rate)
            amp = np.sum(d * w * ph) / np.sum(w)
            band = 20 * np.log10(abs(amp) + 1e-12)
        else:
            psd = np.abs(np.fft.fftshift(np.fft.fft(d * np.hanning(len(d))))) ** 2
            psd /= np.sum(np.hanning(len(d)) ** 2)
            psd = 10 * np.log10(psd + 1e-20)
            band = float(psd.max() - np.median(psd))
        if self.save_iq:
            np.save(os.path.join(outdir, "iq_%06d.npy" % index), d.astype(np.complex64))
        return mean, band


class MockMeasure:
    """Synthetic pattern so the whole path can be exercised without a rig."""

    def measure(self, index, outdir):
        x = self._hook.get("x", 0.0)
        y = self._hook.get("y", 0.0)
        r = self._hook.get("r", 0.0)
        # a crude horn pattern + a little noise
        rr = math.hypot(x, y) / 150.0
        lobe = 20 * math.log10(abs(np.cos(np.radians(r)) * np.exp(-rr ** 2)) + 1e-3)
        noise = float(np.random.normal(0, 1.0))
        return -60 + lobe + noise, 0.0 + noise

    def attach(self, motion):
        self._hook = motion.pos


# --------------------------------------------------------------------------
# scan patterns
# --------------------------------------------------------------------------
def raster_points(nx, ny, pitch, serpentine=True):
    pts = []
    for j in range(ny):
        y = j * pitch
        xs = range(nx) if (j % 2 == 0 or not serpentine) else reversed(range(nx))
        for i in xs:
            pts.append((i * pitch, y))
    return pts


def polar_points(start, stop, step, radius=0.0):
    n = int(round((stop - start) / step))
    return [(radius * math.cos(math.radians(start + k * step)),
             radius * math.sin(math.radians(start + k * step)),
             start + k * step) for k in range(n + 1)]


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pattern", choices=["raster", "polar"])
    ap.add_argument("--port", default=None, help="GRBL/Marlin serial port")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--mock", action="store_true", help="no hardware: synthetic data")
    ap.add_argument("--r-axis", default="z", choices=["z", "a", "b"])
    ap.add_argument("--home", action="store_true", help="run the homing cycle first")
    ap.add_argument("--nx", type=int, default=9)
    ap.add_argument("--ny", type=int, default=9)
    ap.add_argument("--pitch", type=float, default=50.0, help="mm between points")
    ap.add_argument("--start", type=float, default=-180.0, help="polar: start angle (deg)")
    ap.add_argument("--stop", type=float, default=180.0)
    ap.add_argument("--step", type=float, default=5.0)
    ap.add_argument("--radius", type=float, default=0.0, help="polar: offset radius (mm)")
    ap.add_argument("--freq", type=float, default=2400e6)
    ap.add_argument("--rate", type=float, default=2083333.0)
    ap.add_argument("--gain", type=int, default=40)
    ap.add_argument("--nsamples", type=int, default=65536)
    ap.add_argument("--offset", type=float, default=None,
                    help="CW offset for a transmission measurement (e.g. 250e3)")
    ap.add_argument("--feed", type=float, default=3000, help="mm/min")
    ap.add_argument("--dwell", type=float, default=0.25, help="seconds to settle before capture")
    ap.add_argument("--save-iq", action="store_true")
    ap.add_argument("--out", required=True, help="output basename")
    ap.add_argument("--no-plot", action="store_true")
    a = ap.parse_args()

    if not a.mock and not a.port:
        sys.exit("need --port /dev/ttyUSB0 (or --mock to test the pipeline)")

    outdir = os.path.dirname(a.out) or "."
    os.makedirs(outdir, exist_ok=True)

    motion = MockMotion() if a.mock else GrblMotion(a.port, a.baud, a.r_axis, int(a.feed))
    if a.mock:
        measure = MockMeasure()
        measure.attach(motion)
    else:
        measure = SdrMeasure(a.freq, a.rate, a.gain, a.nsamples,
                             offset=a.offset, save_iq=a.save_iq)

    if a.home:
        print("homing...")
        motion.home()

    rows = []
    if a.pattern == "raster":
        pts = [(x, y, None) for x, y in raster_points(a.nx, a.ny, a.pitch)]
    else:
        pts = polar_points(a.start, a.stop, a.step, a.radius)

    print("%s scan: %d points, %.1f MHz, dwell %.2f s"
          % (a.pattern, len(pts), a.freq / 1e6, a.dwell))
    t0 = time.time()
    try:
        for k, p in enumerate(pts):
            if len(p) == 3:
                x, y, r = p
                motion.move(x=x, y=y, r=r)
            else:
                motion.move(x=p[0], y=p[1])
            motion.dwell(a.dwell)
            mean, band = measure.measure(k, outdir)
            pos = getattr(motion, "pos", {"x": p[0], "y": p[1], "r": p[2] if len(p) == 3 else 0.0})
            rows.append({"index": k, "x_mm": pos["x"], "y_mm": pos["y"], "r_deg": pos["r"],
                         "freq_hz": a.freq, "mean_dbfs": mean, "peak_over_floor_db": band})
            if (k + 1) % max(1, len(pts) // 10) == 0:
                print("  %d/%d  x=%.1f y=%.1f r=%.1f  %.1f dBFS"
                      % (k + 1, len(pts), pos["x"], pos["y"], pos["r"], mean), flush=True)
    except KeyboardInterrupt:
        print("\ninterrupted - keeping %d points" % len(rows))
    finally:
        motion.close()

    if not rows:
        sys.exit("no data")

    with open(a.out + ".csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    json.dump({"pattern": a.pattern, "freq": a.freq, "rate": a.rate, "gain": a.gain,
               "points": len(rows), "mock": a.mock,
               "when": time.strftime("%Y-%m-%dT%H:%M:%S")},
              open(a.out + ".json", "w"), indent=2)
    print("wrote %s.csv (%d points, %.1f s)" % (a.out, len(rows), time.time() - t0))

    if a.pattern == "raster" and not a.no_plot:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        xs = sorted({r["x_mm"] for r in rows})
        ys = sorted({r["y_mm"] for r in rows})
        grid = np.full((len(ys), len(xs)), np.nan)
        for r in rows:
            grid[ys.index(r["y_mm"]), xs.index(r["x_mm"])] = r["mean_dbfs"]
        fig, ax = plt.subplots(figsize=(7.5, 6), facecolor="#0d1117")
        ax.set_facecolor("#131a23")
        im = ax.imshow(grid, origin="lower", aspect="equal", cmap="turbo",
                       extent=[xs[0], xs[-1], ys[0], ys[-1]])
        ax.set_xlabel("X (mm)", color="#c9d4e0")
        ax.set_ylabel("Y (mm)", color="#c9d4e0")
        ax.set_title("planar scan at %.1f MHz" % (a.freq / 1e6), color="#eaf2fb")
        ax.tick_params(colors="#8fa3b8")
        cb = fig.colorbar(im, ax=ax, pad=0.02)
        cb.set_label("dBFS", color="#c9d4e0")
        cb.ax.tick_params(colors="#8fa3b8")
        fig.savefig(a.out + ".png", dpi=130, facecolor=fig.get_facecolor(), bbox_inches="tight")
        print("wrote %s.png" % a.out)
    elif a.pattern == "polar" and not a.no_plot:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        th = np.radians([r["r_deg"] for r in rows])
        mag = np.array([r["mean_dbfs"] for r in rows])
        mag = mag - mag.max()
        fig = plt.figure(figsize=(7, 7), facecolor="#0d1117")
        ax = fig.add_subplot(111, polar=True)
        ax.set_facecolor("#131a23")
        ax.plot(th, mag, color="#38d6ff", lw=1.2)
        ax.fill(th, mag, color="#38d6ff", alpha=0.18)
        ax.set_title("polar cut at %.1f MHz (normalised)" % (a.freq / 1e6),
                     color="#eaf2fb", pad=18)
        ax.tick_params(colors="#8fa3b8")
        ax.grid(color="#243040")
        fig.savefig(a.out + ".png", dpi=130, facecolor=fig.get_facecolor(), bbox_inches="tight")
        print("wrote %s.png" % a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
