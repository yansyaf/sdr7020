#!/usr/bin/env python3
"""Plot a band-occupancy chart from survey.jsonl (written by band_survey.py).

Usage: plot_survey.py captures/survey/survey.jsonl [out.png]
"""
import json
import sys

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def main():
    path = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else "band_occupancy.png"
    rows = [json.loads(l) for l in open(path) if l.strip()]
    rows = [r for r in rows if "psd_median" in r]
    rows.sort(key=lambda r: r["freq"])
    f = np.array([r["freq"] / 1e6 for r in rows])
    snr = np.array([r["psd_peak"] - r["psd_median"] for r in rows])
    labels = [r.get("label", "") for r in rows]

    fig, ax = plt.subplots(figsize=(12.5, 6), facecolor="#0d1117")
    ax.set_facecolor("#131a23")
    ax.vlines(f, 0, snr, color="#38d6ff", lw=2)
    ax.plot(f, snr, "o", ms=5, color="#38d6ff")
    hot = snr > max(15, np.percentile(snr, 80))
    ax.plot(f[hot], snr[hot], "o", ms=7, color="#ffd166")
    ax.set_xscale("log")
    ax.set_xlabel("frequency (MHz, log scale)", color="#c9d4e0")
    ax.set_ylabel("mean power above the quietest band (dB)", color="#c9d4e0")
    ax.set_title("PlutoSky 7020 — band occupancy, 2 MHz windows at 71 dB gain\n"
                 "no antenna fitted (SMA dust caps), so this is leakage only",
                 color="#eaf2fb", fontsize=11)
    ax.grid(color="#243040", lw=0.5, axis="y")
    ax.tick_params(colors="#8fa3b8")
    ax.set_ylim(0, max(snr) * 1.35 + 1)
    seen = {}
    order = np.argsort(snr)[::-1][:5]
    for i in order:
        lab = labels[i] or ""
        ax.annotate("%s\n%.0f MHz" % (lab, f[i]) if lab else "%.0f MHz" % f[i],
                    (f[i], snr[i]), textcoords="offset points", xytext=(0, 9),
                    ha="center", fontsize=8, color="#ffd166")
    fig.savefig(out, dpi=130, facecolor=fig.get_facecolor(), bbox_inches="tight")
    print("wrote", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
