#!/usr/bin/env python3
"""Decide whether the energy in a spectrum is a real signal or a receiver artefact.

Method: capture twice, with the LO shifted by a few hundred kHz.

  * a real carrier sits at a fixed *absolute* frequency, so it moves inside the
    captured window when the LO moves;
  * DC offset, 1/f noise, LO leakage and baseband spurs are referenced to the
    LO, so they stay at the same *offset* from the LO.

The script compares the two PSDs under both alignments and reports which one
correlates better, then draws the evidence.

    ./verify_signal.py --freq 816e6 --backend serial
    ./verify_signal.py --freq 935e6 --shift 400e3 --samples 32768 --out images/verify_935.png
"""
import argparse
import os
import subprocess
import sys

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))


def psd(iq, fs, nper=2048):
    nper = min(nper, len(iq))
    w = np.hanning(nper)
    n = max(1, len(iq) // nper)
    P = np.abs(np.fft.fftshift(np.fft.fft(iq[: n * nper].reshape(n, nper) * w, axis=1), axes=1)) ** 2
    db = 10 * np.log10(P.mean(axis=0) / np.sum(w ** 2) + 1e-20)
    f = np.fft.fftshift(np.fft.fftfreq(nper, 1 / fs))
    return f, db


def capture(freq, out, args):
    tool = "capture_serial.py" if args.backend == "serial" else "capture.py"
    cmd = [sys.executable, os.path.join(HERE, tool),
           "--freq", str(int(freq)), "--samples", str(args.samples),
           "--rate", str(int(args.rate)), "--bw", str(int(args.rate)), "--gain", str(args.gain),
           "--out", out, "--no-plot"]
    r = subprocess.run([c for c in cmd if c != "--no-plot"], capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout, r.stderr)
        raise SystemExit("capture failed at %.3f MHz" % (freq / 1e6))
    return np.load(out + ".npy")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--freq", type=float, required=True)
    ap.add_argument("--shift", type=float, default=400e3, help="LO shift for the second capture")
    ap.add_argument("--rate", type=float, default=2083333)
    ap.add_argument("--gain", type=int, default=71)
    ap.add_argument("--samples", type=int, default=16384)
    ap.add_argument("--backend", choices=["usb", "serial"], default="usb")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    f0 = a.freq
    f1 = a.freq + a.shift
    os.makedirs("/tmp/sdr7020_verify", exist_ok=True)
    print("capture 1: LO %.4f MHz" % (f0 / 1e6))
    iq0 = capture(f0, "/tmp/sdr7020_verify/a", a)
    print("capture 2: LO %.4f MHz" % (f1 / 1e6))
    iq1 = capture(f1, "/tmp/sdr7020_verify/b", a)

    fo, d0 = psd(iq0, a.rate)
    ft, d1 = psd(iq1, a.rate)

    # common LO-relative offsets, |offset| <= 800 kHz
    offs = np.arange(-800e3, 800e3 + 1, a.rate / 2048)
    rel = np.array([d0[np.argmin(abs(fo - o))] for o in offs])
    rel2 = np.array([d1[np.argmin(abs(ft - o))] for o in offs])
    c_rel = np.corrcoef(rel, rel2)[0, 1]

    # absolute alignment: sample both at f0 + offs
    ab = np.array([d0[np.argmin(abs(fo - o))] for o in offs])
    ab2 = np.array([d1[np.argmin(abs(ft - (o + a.shift)))] for o in offs])
    c_abs = np.corrcoef(ab, ab2)[0, 1]

    print("\ncorrelation of the two spectra,  |offset| <= 800 kHz")
    print("  same offset from the LO : %+.3f" % c_rel)
    print("  same absolute frequency : %+.3f" % c_abs)
    if c_abs > c_rel + 0.15:
        verdict = ("REAL SIGNAL: features stay at a fixed absolute frequency when the "
                   "LO moves, so they come from outside the receiver.")
    elif c_rel > c_abs + 0.15:
        verdict = ("ARTEFACT: features follow the LO, so they are the receiver's own "
                   "DC offset / 1-f noise / baseband spurs, not radiated signals.")
    else:
        verdict = "INCONCLUSIVE: the two alignments are equally good (no strong features)."
    print("\n" + verdict)

    fig, ax = plt.subplots(figsize=(11, 5.5), facecolor="#0d1117")
    ax.set_facecolor("#131a23")
    ax.plot((fo + f0) / 1e6, d0, lw=0.8, color="#38d6ff", label="LO %.3f MHz" % (f0 / 1e6))
    ax.plot((ft + f1) / 1e6, d1, lw=0.8, color="#ff9f68", label="LO %.3f MHz" % (f1 / 1e6))
    for f, c in ((f0, "#38d6ff"), (f1, "#ff9f68")):
        ax.axvline(f / 1e6, color=c, ls=":", lw=0.9, alpha=0.7)
    ax.set_xlabel("RF frequency (MHz)", color="#c9d4e0")
    ax.set_ylabel("PSD (dBFS/Hz)", color="#c9d4e0")
    ax.set_title("%.1f MHz — two LO settings\n%s" % (f0 / 1e6, verdict),
                 color="#eaf2fb", fontsize=10)
    ax.grid(color="#243040", lw=0.5)
    ax.tick_params(colors="#8fa3b8")
    ax.legend(facecolor="#131a23", edgecolor="#243040", labelcolor="#c9d4e0", fontsize=9)
    out = a.out or os.path.join("/tmp", "verify_%.1fMHz.png" % (f0 / 1e6))
    fig.savefig(out, dpi=125, facecolor=fig.get_facecolor(), bbox_inches="tight")
    print("plot:", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
