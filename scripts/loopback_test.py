#!/usr/bin/env python3
"""Cable loopback test: TX1 -> RX1 (or TX2 -> RX2) through a coax jumper.

This proves the whole RF chain in one measurement - TX LO, DAC, PA, the cable,
the RX balun, LNA, mixer, ADC, the FPGA datapath, DMA and the IIO buffer. If a
clean tone comes back, everything between the SMA connector and userspace works.

    ./loopback_test.py                       # auto: sweeps TX attenuation
    ./loopback_test.py --freq 915e6 --offset 250e3
    ./loopback_test.py --tx-gain -60 --no-sweep

Why the sweep: **TX1 has a PGA-102+ PA**. A jumper straight from TX to RX puts
the PA output into the receiver input. At full TX power that saturates the ADC at
best and can damage the RX front end at worst. Without an attenuator inline the
usable TX setting is somewhere around -60 dB or lower, and it depends on the
band, so this script finds it instead of guessing.

**Put a 20-30 dB SMA attenuator in the loop if you have one.** Then the TX can
run at a sane level and the numbers mean something.

Exit codes: 0 chain works, 1 no clean setting found, 2 no device.
"""
import argparse
import sys
import time

import numpy as np

FS = 2048.0            # 12-bit samples, left aligned in int16


def open_sdr(uri="usb:"):
    import adi
    last = None
    for attempt in range(5):
        try:
            return adi.Pluto(uri=uri)
        except Exception as e:
            last = e
            time.sleep(1.0)
    print("cannot open the board at %s: %s" % (uri, last), file=sys.stderr)
    print("  - is the USB data cable plugged into the board's USB (OTG) port?",
          file=sys.stderr)
    print("  - lsusb | grep 0456   and   iio_info -s", file=sys.stderr)
    sys.exit(2)


def measure(sdr, freq, offset, tx_gain, rx_gain, nsamples):
    sdr.tx_hardwaregain_chan0 = float(tx_gain)
    sdr.rx_hardwaregain_chan0 = int(rx_gain)
    sdr.tx_lo = int(freq)
    sdr.rx_lo = int(freq)
    sdr.dds_single_tone(offset, 0.5, 0)
    time.sleep(0.05)
    for _ in range(2):
        sdr.rx()
    d = np.asarray(sdr.rx()) / FS
    n = len(d)
    w = np.hanning(n)
    P = np.abs(np.fft.fftshift(np.fft.fft(d * w))) ** 2 / np.sum(w ** 2)
    Pdb = 10 * np.log10(P + 1e-20)
    f = np.fft.fftshift(np.fft.fftfreq(n, 1 / sdr.sample_rate))
    band = np.abs(f - offset) < 5e3
    guard = (np.abs(f - offset) > 50e3) & (np.abs(f - offset) < 800e3)
    tone = float(Pdb[band].max())
    floor = float(np.median(Pdb[guard]))
    clip = float(np.mean(np.abs(d) >= 0.98))
    return {"mean": float(10 * np.log10(np.mean(np.abs(d) ** 2) + 1e-20)),
            "peak": float(np.max(np.abs(d)) * FS), "tone": tone, "floor": floor,
            "snr": tone - floor, "clip_frac": clip, "data": d}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--freq", type=float, default=915e6)
    ap.add_argument("--offset", type=float, default=250e3, help="tone offset from the LO")
    ap.add_argument("--rx-gain", type=int, default=0,
                    help="keep this low: the loopback signal is strong")
    ap.add_argument("--tx-gain", type=float, default=None, help="skip the sweep, use this")
    ap.add_argument("--no-sweep", action="store_true")
    ap.add_argument("--tx-channel", type=int, default=0)
    ap.add_argument("--rx-channel", type=int, default=0)
    ap.add_argument("--nsamples", type=int, default=1 << 15)
    ap.add_argument("--out", default="/tmp/loopback")
    ap.add_argument("--uri", default="usb:")
    a = ap.parse_args()

    sdr = open_sdr(a.uri)
    sdr.sample_rate = int(2083333)
    sdr.rx_rf_bandwidth = int(2083333)
    sdr.tx_rf_bandwidth = int(2083333)
    sdr.gain_control_mode_chan0 = "manual"
    sdr.rx_buffer_size = int(a.nsamples)
    try:
        sdr.tx_enabled_channels = [a.tx_channel]
        sdr.rx_enabled_channels = [a.rx_channel]
    except Exception:
        pass

    print("loopback %d -> %d, LO %.4f MHz, tone at %+.0f kHz"
          % (a.tx_channel, a.rx_channel, a.freq / 1e6, a.offset / 1e3))
    print("\n tx_gain   mean dBFS   peak|x|   tone dBFS   SNR vs floor   clipping")
    best = None
    if a.no_sweep or a.tx_gain is not None:
        gains = [a.tx_gain if a.tx_gain is not None else -60.0]
    else:
        gains = [-89.75, -80, -70, -60, -50, -40, -30]
    for g in gains:
        r = measure(sdr, a.freq, a.offset, g, a.rx_gain, a.nsamples)
        clipped = r["clip_frac"] > 0.001
        print("  %7.2f  %9.1f  %7.0f  %9.1f  %+9.1f dB      %s"
              % (g, r["mean"], r["peak"], r["tone"], r["snr"],
                 "YES (%.2f%% samples)" % (100 * r["clip_frac"]) if clipped else "no"))
        if not clipped and (best is None or r["snr"] > best[1]["snr"]):
            best = (g, r)

    if best is None:
        print("\nno clean setting: every TX level either clipped or found no tone.")
        print("  - is the jumper really TX1 -> RX1 (not TX2/RX2)? check the labels")
        print("  - an attenuator may be missing where one is needed, or the LO is off")
        sdr.dds_single_tone(0, 0, 0)
        return 1

    g, r = best
    print("\nusing tx_gain %.2f dB, rx_gain %d, SNR %+.1f dB" % (g, a.rx_gain, r["snr"]))

    # longer capture for the picture
    sdr.rx_buffer_size = 1 << 18
    r = measure(sdr, a.freq, a.offset, g, a.rx_gain, 1 << 18)
    sdr.dds_single_tone(0, 0, 0)

    n = len(r["data"])
    w = np.hanning(n)
    P = 10 * np.log10(np.abs(np.fft.fftshift(np.fft.fft(r["data"] * w))) ** 2 / np.sum(w ** 2) + 1e-20)
    f = np.fft.fftshift(np.fft.fftfreq(n, 1 / sdr.sample_rate)) + a.freq

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(11, 5), facecolor="#0d1117")
    ax.set_facecolor("#131a23")
    ax.plot(f / 1e6, P, lw=0.7, color="#38d6ff")
    ax.set_xlabel("RF frequency (MHz)", color="#c9d4e0")
    ax.set_ylabel("dBFS", color="#c9d4e0")
    ax.set_title("cable loopback TX%d -> RX%d at %.3f MHz | TX %.1f dB, RX %d dB | tone %.1f dBFS, SNR %.1f dB"
                 % (a.tx_channel, a.rx_channel, a.freq / 1e6, g, a.rx_gain, r["tone"], r["snr"]),
                 color="#eaf2fb", fontsize=9)
    ax.grid(color="#243040", lw=0.5)
    ax.tick_params(colors="#8fa3b8")
    fig.savefig(a.out + ".png", dpi=130, facecolor=fig.get_facecolor(), bbox_inches="tight")
    np.save(a.out + ".npy", r["data"].astype(np.complex64))
    print("wrote %s.png and .npy" % a.out)

    if r["snr"] > 40:
        print("\nPASS: the whole chain works - TX LO/DAC/PA, cable, RX LNA/mixer/ADC, DMA, IIO.")
    elif r["snr"] > 15:
        print("\nPARTIAL: a tone came back but weakly. Check the cable seating and the")
        print("         connectors; try another band before blaming the receiver.")
        return 1
    else:
        print("\nFAIL: no tone above the floor. Wrong SMA pair, a dead jumper, or the")
        print("      DDS is not reaching the TX path.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
