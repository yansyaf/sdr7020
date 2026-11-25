#!/usr/bin/env python3
"""Plot a PlutoSky/PlutoSDR IQ capture.

Accepts either
  * <file>.npy   complex64 samples written by capture.py, or
  * <file>.raw   interleaved int16 I/Q written by capture_serial.py

Frequency / sample rate come from the .json sidecar next to the file, or from
--freq / --rate. Produces a PNG with PSD, spectrogram and envelope, and prints
the strongest real carriers (band-edge and DC artefacts excluded).

Examples
--------
    ./plot_iq.py captures/gsm900.npy
    ./plot_iq.py captures/cap_935M.raw --freq 935e6 --rate 2083333 --out 935.png
"""
import argparse
import json
import os
import sys

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

EDGE_FRAC = 0.035          # ignore +/- fs/2 artefacts (HB filter / aliasing)
DC_BINS = 4                # ignore DC / LO leakage


def load(path, freq=None, rate=None):
    base, ext = os.path.splitext(path)
    meta = {}
    if os.path.exists(base + ".json"):
        try:
            meta = json.load(open(base + ".json"))
        except Exception:
            meta = {}
    freq = freq if freq is not None else meta.get("freq")
    rate = rate if rate is not None else meta.get("rate")
    if ext == ".npy":
        iq = np.load(path).astype(np.complex64)
    else:
        d = np.fromfile(path, dtype=np.int16)
        iq = (d[0::2].astype(np.float32) + 1j * d[1::2].astype(np.float32)) / 2 ** 11
    if freq is None or rate is None:
        sys.exit("need --freq and --rate (no .json sidecar found)")
    return iq, float(freq), float(rate), meta


def welch(iq, fs, nper=4096):
    nper = min(nper, len(iq))
    win = np.hanning(nper)
    nseg = max(1, len(iq) // nper)
    segs = iq[: nseg * nper].reshape(nseg, nper)
    P = np.abs(np.fft.fftshift(np.fft.fft(segs * win, axis=1), axes=1)) ** 2
    P = P.mean(axis=0) / (np.sum(win ** 2))
    f = np.fft.fftshift(np.fft.fftfreq(nper, 1 / fs))
    return f, 10 * np.log10(P + 1e-20)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--freq", type=float)
    ap.add_argument("--rate", type=float)
    ap.add_argument("--out", default=None)
    ap.add_argument("--title", default="PlutoSky 7020 AD9361")
    a = ap.parse_args()

    iq, f0, fs, meta = load(a.file, a.freq, a.rate)
    n = len(iq)
    f, psd = welch(iq, fs)
    med = float(np.median(psd))
    p_dbfs = 10 * np.log10(np.mean(np.abs(iq) ** 2) + 1e-20)

    edge = int(len(f) * EDGE_FRAC)
    cand = psd.copy()
    cand[:edge] = -999
    cand[-edge:] = -999
    cand[np.abs(f) < fs / len(f) * DC_BINS] = -999
    pk = int(np.argmax(cand))
    if cand[pk] < -900:
        print("no candidate bins")
        return 1

    print("file      : %s" % a.file)
    print("samples   : %d  (%.2f ms @ %.3f MSPS)" % (n, n / fs * 1e3, fs / 1e6))
    print("LO        : %.4f MHz   gain %s dB" % (f0 / 1e6, meta.get("gain", "?")))
    print("mean power: %.1f dBFS   median PSD %.1f dBFS" % (p_dbfs, med))
    print("strongest : %.4f MHz  %.1f dBFS  (SNR %.1f dB)"
          % ((f[pk] + f0) / 1e6, cand[pk], cand[pk] - med))

    # spectrogram
    nfft, hop = 256, 128
    nfr = max(1, (n - nfft) // hop)
    w = np.hanning(nfft)
    S = np.empty((nfr, nfft))
    for k in range(nfr):
        S[k] = np.abs(np.fft.fftshift(np.fft.fft(iq[k * hop:k * hop + nfft] * w))) ** 2
    S = 10 * np.log10(S / np.sum(w ** 2) + 1e-20)

    B = 32
    nb = n // B
    env = 10 * np.log10((np.abs(iq[: nb * B].reshape(nb, B)) ** 2).mean(axis=1) + 1e-20)

    fc = "#131a23"
    fig = plt.figure(figsize=(13, 10), facecolor="#0d1117")
    gs = fig.add_gridspec(3, 1, hspace=0.38, height_ratios=[2.6, 2.6, 1.5])

    ax1 = fig.add_subplot(gs[0])
    ax1.set_facecolor(fc)
    ax1.plot((f + f0) / 1e6, psd, lw=0.7, color="#38d6ff")
    ax1.axvspan((f[0] + f0) / 1e6, (f[edge] + f0) / 1e6, color="#3b2a2a", alpha=0.5)
    ax1.axvspan((f[-edge] + f0) / 1e6, (f[-1] + f0) / 1e6, color="#3b2a2a", alpha=0.5)
    ax1.plot((f[pk] + f0) / 1e6, cand[pk], "o", ms=6, color="#ffd166")
    ax1.annotate("%.4f MHz  %.1f dBFS  (SNR %.1f dB)" % ((f[pk] + f0) / 1e6, cand[pk], cand[pk] - med),
                 xy=((f[pk] + f0) / 1e6, cand[pk]), xycoords="data",
                 xytext=(0.03, 0.90), textcoords="axes fraction",
                 color="#ffd166", fontsize=9,
                 arrowprops=dict(arrowstyle="->", color="#ffd166", lw=0.9, alpha=0.7))
    ax1.set_ylabel("PSD (dBFS/Hz)", color="#c9d4e0")
    ax1.set_xlabel("RF frequency (MHz)", color="#c9d4e0")
    ax1.set_title("%s\nLO %.3f MHz | %.3f MSPS | %d samples | mean %.1f dBFS"
                  % (a.title, f0 / 1e6, fs / 1e6, n, p_dbfs), color="#eaf2fb", fontsize=11)
    ax1.grid(color="#243040", lw=0.5)
    ax1.set_ylim(med - 14, max(cand[pk] + 8, med + 22))
    ax1.tick_params(colors="#8fa3b8")

    ax2 = fig.add_subplot(gs[1])
    ax2.set_facecolor(fc)
    ext = [(f[0] + f0) / 1e6, (f[-1] + f0) / 1e6, n / fs * 1e3, 0]
    vmax = np.percentile(S, 99.5)
    im = ax2.imshow(S, aspect="auto", cmap="turbo", extent=ext, vmin=vmax - 55, vmax=vmax)
    ax2.set_ylabel("Time (ms)", color="#c9d4e0")
    ax2.set_xlabel("RF frequency (MHz)", color="#c9d4e0")
    ax2.tick_params(colors="#8fa3b8")
    cb = fig.colorbar(im, ax=ax2, pad=0.01)
    cb.set_label("dBFS", color="#c9d4e0")
    cb.ax.tick_params(colors="#8fa3b8")

    ax3 = fig.add_subplot(gs[2])
    ax3.set_facecolor(fc)
    ax3.plot(np.arange(nb) * B / fs * 1e3, env, lw=0.5, color="#7ce38b")
    ax3.set_xlabel("Time (ms)", color="#c9d4e0")
    ax3.set_ylabel("power (dBFS)", color="#c9d4e0")
    ax3.grid(color="#243040", lw=0.5)
    ax3.tick_params(colors="#8fa3b8")

    out = a.out or (os.path.splitext(a.file)[0] + ".png")
    fig.savefig(out, dpi=125, facecolor=fig.get_facecolor(), bbox_inches="tight")
    print("plot      :", out)

    print("top bins (edge/DC excluded):")
    for i in np.argsort(cand)[-6:][::-1]:
        print("   %.4f MHz  %.1f dBFS" % ((f[i] + f0) / 1e6, cand[i]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
