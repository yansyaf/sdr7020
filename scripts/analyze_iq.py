#!/usr/bin/python3
"""Analyse PlutoSky IQ captures: PSD + spectrogram + envelope.

Usage: analyze_iq.py <file.raw> <f0 Hz> <fs Hz> <out.png> [title] [--gain dB]
"""
import sys
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

EDGE_FRAC = 0.035   # ignore band-edge artefacts near +/- fs/2


def load(path):
    d = np.fromfile(path, dtype=np.int16)
    return d[0::2].astype(np.float32) + 1j * d[1::2].astype(np.float32)


def welch(iq, fs, nper=4096):
    nper = min(nper, len(iq))
    win = np.hanning(nper)
    nseg = max(1, len(iq) // nper)
    segs = iq[: nseg * nper].reshape(nseg, nper)
    P = np.abs(np.fft.fftshift(np.fft.fft(segs * win, axis=1), axes=1)) ** 2
    P = P.mean(axis=0) / (np.sum(win ** 2) * 32768.0 ** 2)
    f = np.fft.fftshift(np.fft.fftfreq(nper, 1 / fs))
    return f, 10 * np.log10(P + 1e-20)


def main():
    path, f0, fs, out = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), sys.argv[4]
    title = sys.argv[5] if len(sys.argv) > 5 and not sys.argv[5].startswith("--") else "PlutoSky 7020 AD9361"
    iq = load(path)
    n = len(iq)
    f, psd = welch(iq, fs)

    p_dbfs = 10 * np.log10(np.mean(np.abs(iq) ** 2) / 32768.0 ** 2)
    med = float(np.median(psd))
    edge = int(len(f) * EDGE_FRAC)
    inner = slice(edge, len(f) - edge)
    # kill DC bins
    dc = np.abs(f) < (fs / len(f) * 4)
    cand = psd.copy()
    cand[:edge] = -999
    cand[-edge:] = -999
    cand[dc] = -999
    pk = int(np.argmax(cand))

    print("samples %d (%.2f ms @ %.3f MSPS)" % (n, n / fs * 1e3, fs / 1e6))
    print("mean power %.1f dBFS   median PSD %.1f   peak %.1f dBFS @ %.4f MHz (snr %.1f dB)"
          % (p_dbfs, med, cand[pk], (f[pk] + f0) / 1e6, cand[pk] - med))

    # spectrogram
    nfft, hop = 256, 128
    nfr = max(1, (n - nfft) // hop)
    S = np.empty((nfr, nfft))
    w = np.hanning(nfft)
    for k in range(nfr):
        seg = iq[k * hop: k * hop + nfft] * w
        S[k] = np.abs(np.fft.fftshift(np.fft.fft(seg))) ** 2
    S = 10 * np.log10(S / (np.sum(w ** 2) * 32768.0 ** 2) + 1e-20)

    # envelope
    B = 32
    nb = n // B
    env = 10 * np.log10(np.abs(iq[: nb * B].reshape(nb, B)).mean(axis=1) / 32768.0 ** 2 + 1e-20)
    # mean(|x|^2) per block
    env = 10 * np.log10((np.abs(iq[: nb * B].reshape(nb, B)) ** 2).mean(axis=1) / 32768.0 ** 2 + 1e-20)

    fig = plt.figure(figsize=(13, 10), facecolor="#0d1117")
    gs = fig.add_gridspec(3, 1, hspace=0.38, height_ratios=[2.6, 2.6, 1.6])
    fc = "#131a23"

    ax1 = fig.add_subplot(gs[0])
    ax1.set_facecolor(fc)
    ax1.plot((f + f0) / 1e6, psd, lw=0.7, color="#38d6ff")
    ax1.axvspan((f[0] + f0) / 1e6, (f[edge] + f0) / 1e6, color="#3b2a2a", alpha=0.55)
    ax1.axvspan((f[-edge] + f0) / 1e6, (f[-1] + f0) / 1e6, color="#3b2a2a", alpha=0.55)
    ax1.plot((f[pk] + f0) / 1e6, cand[pk], "o", ms=6, color="#ffd166")
    ax1.annotate("%.4f MHz\n%.1f dBFS (SNR %.1f dB)" % ((f[pk] + f0) / 1e6, cand[pk], cand[pk] - med),
                 xy=((f[pk] + f0) / 1e6, cand[pk]), xytext=(12, -28), textcoords="offset points",
                 color="#ffd166", fontsize=9,
                 arrowprops=dict(arrowstyle="-", color="#ffd166", lw=0.8))
    ax1.set_ylabel("PSD (dBFS/Hz)", color="#c9d4e0")
    ax1.set_xlabel("RF frequency (MHz)", color="#c9d4e0")
    ax1.set_title("%s\nLO %.3f MHz | %.3f MSPS | %d samples | mean %.1f dBFS"
                  % (title, f0 / 1e6, fs / 1e6, n, p_dbfs), color="#eaf2fb", fontsize=11)
    ax1.grid(color="#243040", lw=0.5)
    ax1.set_ylim(med - 12, max(cand[pk] + 8, med + 20))
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

    fig.savefig(out, dpi=125, facecolor=fig.get_facecolor(), bbox_inches="tight")
    print("wrote", out)

    order = np.argsort(cand)[-6:][::-1]
    print("top bins (edge/DC excluded):")
    for i in order:
        print("   %.4f MHz  %.1f dBFS" % ((f[i] + f0) / 1e6, cand[i]))


if __name__ == "__main__":
    main()
