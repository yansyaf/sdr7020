#!/usr/bin/python3
"""Sweep the PlutoSky receiver across a list of frequencies over the serial
console, capture a short IQ record at each, and store the PSD.

Writes one JSON line per frequency to survey.jsonl and a .npz with the PSD.

Usage: band_survey.py [--samples 16384] [--gain 71] [--outdir DIR] [--start MHz --stop MHz --step MHz]
"""
import argparse, json, os, subprocess, sys, time
import numpy as np

SDR = os.path.dirname(os.path.abspath(__file__))
CAP = os.path.join(SDR, "pluto_capture.py")

DEFAULT_BANDS = [
    # (start MHz, stop MHz, step MHz, label)
    (88, 108, 4, "FM broadcast"),
    (118, 137, 6, "airband"),
    (145, 147, 2, "VHF ham"),
    (430, 440, 5, "70cm ISM"),
    (470, 790, 20, "DVB-T"),
    (790, 862, 12, "LTE 800 / CDMA"),
    (880, 960, 8, "GSM 900 / LTE 900"),
    (1710, 1880, 20, "LTE 1800"),
    (1920, 2170, 25, "UMTS / LTE 2100"),
    (2300, 2400, 25, "LTE 2300"),
    (2400, 2500, 20, "WiFi 2.4"),
    (5150, 5850, 100, "WiFi 5"),
]


def psd_of(path, fs):
    d = np.fromfile(path, dtype=np.int16)
    iq = d[0::2].astype(np.float32) + 1j * d[1::2].astype(np.float32)
    n = len(iq)
    nper = min(2048, n)
    win = np.hanning(nper)
    nseg = max(1, n // nper)
    segs = iq[: nseg * nper].reshape(nseg, nper)
    P = np.abs(np.fft.fftshift(np.fft.fft(segs * win, axis=1), axes=1)) ** 2
    P = P.mean(axis=0) / (np.sum(win ** 2) * 2048.0 ** 2)
    db = 10 * np.log10(P + 1e-20)
    f = np.fft.fftshift(np.fft.fftfreq(nper, 1 / fs))
    # blank the DC / 1-f pedestal (about +/-300 kHz after gain) and the band edges:
    # those are the receiver's own artefacts and would otherwise dominate every window
    db[np.abs(f) < 300e3] = -999.0
    edge = int(nper * 0.035)
    db[:edge] = -999.0
    db[-edge:] = -999.0
    return f, db, np.mean(np.abs(iq) ** 2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", type=int, default=16384)
    ap.add_argument("--gain", type=int, default=71)
    ap.add_argument("--outdir", default=os.path.join(SDR, "survey"))
    ap.add_argument("--start", type=float)
    ap.add_argument("--stop", type=float)
    ap.add_argument("--step", type=float, default=10)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--backend", choices=["usb", "serial"], default="usb",
                    help="serial uses capture_serial.py through the console cable")
    a = ap.parse_args()
    os.makedirs(a.outdir, exist_ok=True)
    tool = os.path.join(SDR, "capture_serial.py" if a.backend == "serial" else "capture.py")

    if a.start is not None:
        plan = []
        f = a.start
        while f <= a.stop + 1e-6:
            plan.append((f * 1e6, "manual sweep"))
            f += a.step
    else:
        plan = []
        for s, e, st, lab in DEFAULT_BANDS:
            f = s
            while f <= e + 1e-6:
                plan.append((f * 1e6, lab))
                f += st

    jl = os.path.join(a.outdir, "survey.jsonl")
    done = set()
    if a.resume and os.path.exists(jl):
        for line in open(jl):
            try:
                done.add(round(json.loads(line)["freq"]))
            except Exception:
                pass

    print("planned captures: %d (already done: %d)" % (len(plan), len(done)), flush=True)
    for i, (f, lab) in enumerate(plan):
        if round(f) in done:
            continue
        base = os.path.join(a.outdir, "cap_%d" % round(f))
        raw = base + ".raw"
        if not (os.path.exists(raw) and os.path.getsize(raw) == a.samples * 4):
            ok = False
            for attempt in range(3):
                cmd = ["/usr/bin/python3", tool,
                       "--freq", str(int(f)), "--samples", str(a.samples),
                       "--gain", str(a.gain), "--rate", "2083333", "--bw", "2000000",
                       "--out", base]
                r = subprocess.run(cmd, capture_output=True, text=True)
                if r.returncode == 0 and os.path.exists(raw):
                    ok = True
                    break
                print("  retry %d for %.1f MHz: %s" % (attempt + 1, f / 1e6,
                                                       (r.stdout + r.stderr).strip().splitlines()[-1:]), flush=True)
                time.sleep(3)
            if not ok:
                with open(jl, "a") as fh:
                    fh.write(json.dumps({"freq": f, "label": lab, "error": "capture failed"}) + "\n")
                continue
        psd, ms = psd_of(raw, 2083333)
        np.savez_compressed(base + ".npz", psd=psd, freq=f, fs=2083333)
        peak = float(psd.max())
        med = float(np.median(psd))
        rec = {"freq": f, "label": lab, "psd_peak": peak, "psd_median": med,
               "meansq": float(ms), "snr": peak - med, "n": a.samples}
        with open(jl, "a") as fh:
            fh.write(json.dumps(rec) + "\n")
        print("[%d/%d] %8.2f MHz  %-16s peak %.1f dBFS  med %.1f  snr %.1f"
              % (i + 1, len(plan), f / 1e6, lab, peak, med, peak - med), flush=True)
    print("survey complete")


if __name__ == "__main__":
    main()
