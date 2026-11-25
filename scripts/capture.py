#!/usr/bin/env python3
"""Capture IQ samples from a PlutoSDR-compatible board (PlutoSky 7020 / ADALM-PLUTO).

Preferred path: pyadi-iio (``pip install pyadi-iio``) over USB.
Fallback path:  the libiio command line tools (iio_attr / iio_readdev).

Examples
--------
    ./capture.py --find                       # list IIO contexts
    ./capture.py --freq 935e6 --samples 1e6 --out captures/gsm900
    ./capture.py --freq 100e6 --rate 5e6 --bw 4e6 --gain 60 --seconds 0.5
    ./capture.py --uri ip:192.168.2.1 --freq 2.437e9 --samples 2e6

The result is written as <out>.npy (complex64) plus <out>.json metadata, and a
picture <out>.png unless --no-plot is given.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time

import numpy as np

DEFAULT_URI = "usb:"
CHANNELS = "voltage0 voltage1"      # RX1 I/Q
DEV_RX = "cf-ad9361-lpc"
DEV_PHY = "ad9361-phy"


# --------------------------------------------------------------------------
# backends
# --------------------------------------------------------------------------
def _contexts_text():
    try:
        return subprocess.run(["iio_info", "-s"], capture_output=True, text=True,
                              timeout=20).stdout
    except Exception:
        return ""


def find_contexts():
    if shutil.which("iio_info"):
        out = subprocess.run(["iio_info", "-s"], capture_output=True, text=True).stdout
        print(out.strip() or "no IIO contexts found")
    try:
        import adi  # noqa: F401
        print("\npyadi-iio available: yes")
    except Exception:
        print("\npyadi-iio available: no (falling back to libiio CLI)")


def capture_pyadi(uri, freq, rate, bw, gain, nsamples, antenna):
    import adi
    sdr = adi.Pluto(uri=uri) if uri and uri not in ("usb:", "local:") else adi.Pluto()
    sdr.sample_rate = int(rate)
    sdr.tx_lo = sdr.rx_lo = int(freq)
    sdr.rx_rf_bandwidth = int(bw)
    sdr.gain_control_mode_chan0 = "manual"
    sdr.rx_hardwaregain_chan0 = int(gain)
    try:
        sdr.rx_enabled_channels = [0, 1]
    except Exception:
        pass
    try:
        sdr.rx_buffer_size = int(nsamples)
    except Exception as e:
        print("buffer size warning:", e)
    # flush a few buffers so the first samples are not stale
    for _ in range(2):
        sdr.rx()
    t0 = time.time()
    data = sdr.rx()
    dt = time.time() - t0
    data = np.asarray(data)
    if data.ndim == 2:                      # (2, N) -> complex
        data = data[0] + 1j * data[1]
    data = data.astype(np.complex64) / 2 ** 11
    print("pyadi-iio: %d samples in %.3f s (%.2f MSPS effective)"
          % (len(data), dt, len(data) / dt / 1e6))
    return data


def _cli(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 and "Invalid argument" not in r.stderr:
        sys.stderr.write("warn: %s -> %s\n" % (" ".join(cmd), r.stderr.strip()[:200]))
    return r


def capture_cli(uri, freq, rate, bw, gain, nsamples, antenna):
    u = ["-u", uri] if uri else []
    _cli(["iio_attr"] + u + ["-c", DEV_PHY, "voltage0", "gain_control_mode", "manual"])
    _cli(["iio_attr"] + u + ["-c", DEV_PHY, "voltage0", "hardwaregain", str(int(gain))])
    _cli(["iio_attr"] + u + ["-c", DEV_PHY, "voltage0", "sampling_frequency", str(int(rate))])
    _cli(["iio_attr"] + u + ["-c", DEV_PHY, "voltage0", "rf_bandwidth", str(int(bw))])
    _cli(["iio_attr"] + u + ["-c", DEV_PHY, "voltage0", "rf_port_select", antenna])
    _cli(["iio_attr"] + u + ["-c", DEV_PHY, "altvoltage0", "frequency", str(int(freq))])
    time.sleep(0.3)
    block = max(4096, min(262144, int(nsamples) // 4))
    out = subprocess.run(
        ["iio_readdev"] + u + ["-b", str(block), "-s", str(int(nsamples)), DEV_RX,
                               "voltage0", "voltage1"],
        capture_output=True)
    if len(out.stdout) < int(nsamples) * 4:
        raise RuntimeError("short read: %d bytes (wanted %d)"
                           % (len(out.stdout), int(nsamples) * 4))
    d = np.frombuffer(out.stdout[: int(nsamples) * 4], dtype=np.int16)
    iq = (d[0::2].astype(np.float32) + 1j * d[1::2].astype(np.float32)) / 2 ** 11
    print("libiio cli: %d samples" % len(iq))
    return iq.astype(np.complex64)


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--find", action="store_true", help="list IIO contexts and exit")
    ap.add_argument("--uri", default=DEFAULT_URI, help="IIO URI (usb:, ip:192.168.2.1, local:)")
    ap.add_argument("--freq", type=float, default=935e6, help="RX LO in Hz")
    ap.add_argument("--rate", type=float, default=2.0833333e6, help="sample rate in Hz")
    ap.add_argument("--bw", type=float, default=2e6, help="analog RF bandwidth in Hz")
    ap.add_argument("--gain", type=int, default=71, help="manual RX gain (-3..71 dB)")
    ap.add_argument("--samples", type=int, help="number of complex samples")
    ap.add_argument("--seconds", type=float, help="capture duration (alternative to --samples)")
    ap.add_argument("--antenna", default="A_BALANCED", help="rf_port_select value")
    ap.add_argument("--out", default=None, help="output basename (no extension)")
    ap.add_argument("--no-plot", action="store_true")
    a = ap.parse_args()

    if a.find:
        find_contexts()
        return 0

    nsamples = int(a.samples) if a.samples else int(a.seconds * a.rate) if a.seconds else 262144
    out = a.out or os.path.join("captures", "cap_%.3fMHz" % (a.freq / 1e6))
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)

    if a.uri.startswith("usb:") and not re.search(r"\[usb:", _contexts_text()):
        print("error: no PlutoSDR found on USB.\n"
              "  * the cable must be in the board's USB (OTG data) port, not DEBUG\n"
              "  * use a data cable, not a charge-only one\n"
              "  * run scripts/check_setup.sh to see what the host detects",
              file=sys.stderr)
        return 2

    try:
        iq = capture_pyadi(a.uri, a.freq, a.rate, a.bw, a.gain, nsamples, a.antenna)
        backend = "pyadi-iio"
    except Exception as e:
        if os.environ.get("PLUTO_VERBOSE"):
            print("pyadi-iio unavailable (%s), using libiio CLI" % e)
        try:
            iq = capture_cli(a.uri, a.freq, a.rate, a.bw, a.gain, nsamples, a.antenna)
        except RuntimeError as err:
            print("error: capture failed (%s)\n"
                  "  try scripts/check_setup.sh, or the serial fallback:\n"
                  "  scripts/capture_serial.py --freq %.1f --samples %d"
                  % (err, a.freq, nsamples), file=sys.stderr)
            return 3
        backend = "libiio-cli"

    np.save(out + ".npy", iq)
    meta = {"freq": a.freq, "rate": a.rate, "bw": a.bw, "gain": a.gain,
            "samples": len(iq), "backend": backend, "uri": a.uri,
            "antenna": a.antenna, "when": time.strftime("%Y-%m-%dT%H:%M:%S")}
    json.dump(meta, open(out + ".json", "w"), indent=2)
    p = 10 * np.log10(np.mean(np.abs(iq) ** 2) + 1e-20)
    print("saved %s.npy  (%d samples, mean %.1f dBFS)" % (out, len(iq), p))

    if not a.no_plot:
        here = os.path.dirname(os.path.abspath(__file__))
        subprocess.run([sys.executable, os.path.join(here, "plot_iq.py"), out + ".npy"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
